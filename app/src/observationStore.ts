// 端末内への保存（Paths.document/observations/{id}/）。仕様は docs/m3-app-capture.md の 3 節。
import * as Crypto from "expo-crypto";
import { Directory, File, Paths } from "expo-file-system";
import type { ExifInput } from "./exif";
import { buildMetadata, type DeviceInfo, type ObservationMetadata } from "./metadata";
import type { LocationInput, Sample } from "./record";
import type { QueueItem, Status, Store } from "./uploadQueue";

const IMAGE_NAME = "image.jpg";
const METADATA_NAME = "metadata.json";
const STATUS_NAME = "status.json";
/** metadata.json のないディレクトリを、保存の途中で落ちたものとみなすまでの時間 */
const STALE_MS = 60 * 60 * 1000;

function observationsDir(): Directory {
  return new Directory(Paths.document, "observations");
}

function observationDir(id: string): Directory {
  return new Directory(observationsDir(), id);
}

function fileOf(id: string, name: string): File {
  return new File(observationDir(id), name);
}

/** metadata.json を読む。なければ、または読めなければ null。 */
async function readMetadata(id: string): Promise<{ captured_at: string } | null> {
  const file = fileOf(id, METADATA_NAME);
  if (!file.exists) return null;
  try {
    const parsed: unknown = JSON.parse(await file.text());
    if (typeof parsed === "object" && parsed !== null && "captured_at" in parsed) {
      const capturedAt = (parsed as { captured_at: unknown }).captured_at;
      if (typeof capturedAt === "string") return { captured_at: capturedAt };
    }
  } catch {
    // 読めないものは、metadata.json がないものとして扱う
  }
  return null;
}

/** status.json を読む。なければ、または読めなければ pending とみなす。 */
async function readStatus(id: string): Promise<Status> {
  const pending: Status = { state: "pending", attempts: 0 };
  const file = fileOf(id, STATUS_NAME);
  if (!file.exists) return pending;
  try {
    const parsed: unknown = JSON.parse(await file.text());
    if (typeof parsed !== "object" || parsed === null) return pending;
    const status = parsed as Partial<Status>;
    if (status.state !== "pending" && status.state !== "sent" && status.state !== "rejected") {
      return pending;
    }
    return { ...status, state: status.state, attempts: Number(status.attempts) || 0 };
  } catch {
    return pending;
  }
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** observations/ の下のディレクトリを返す。まだなければ空。 */
function listObservationDirs(): Directory[] {
  const root = observationsDir();
  if (!root.exists) return [];
  return root.list().filter((entry): entry is Directory => entry instanceof Directory);
}

export const observationStore: Store = {
  async list(): Promise<QueueItem[]> {
    const items: QueueItem[] = [];
    for (const dir of listObservationDirs()) {
      const id = dir.name;
      const metadata = await readMetadata(id);
      if (metadata === null) continue;
      items.push({ id, capturedAt: metadata.captured_at, status: await readStatus(id) });
    }
    return items;
  },

  async readMetadataJson(id) {
    return fileOf(id, METADATA_NAME).text();
  },

  async image(id) {
    const file = fileOf(id, IMAGE_NAME);
    if (!file.exists) return null;
    return { part: { uri: file.uri, name: IMAGE_NAME, type: "image/jpeg" }, size: file.size };
  },

  async setStatus(id, status) {
    fileOf(id, STATUS_NAME).write(JSON.stringify(status));
  },

  async deleteImage(id) {
    const file = fileOf(id, IMAGE_NAME);
    if (file.exists) file.delete();
  },
};

export type SaveCaptureInput = {
  observationId: string;
  /** takePictureAsync の戻り値の uri（キャッシュの中のファイル） */
  cachedUri: string;
  pressedAtMs: number;
  samples: Sample[];
  location: LocationInput | null;
  headingAccuracy: number | null;
  exif: ExifInput | null | undefined;
  width: number | null | undefined;
  height: number | null | undefined;
  device: DeviceInfo;
};

/**
 * 撮った写真を保存する（3 節の保存の順番 1〜3）。
 * metadata.json を書き終えた時点で保存の完了とする。途中で失敗したら、作りかけのディレクトリを消す。
 */
export async function saveCapture(input: SaveCaptureInput): Promise<ObservationMetadata> {
  const dir = observationDir(input.observationId);
  try {
    dir.create({ intermediates: true, idempotent: true });

    // 1. キャッシュの画像を移す
    const image = new File(dir, IMAGE_NAME);
    await new File(input.cachedUri).move(image, { overwrite: true });

    // 2. SHA-256 を求める
    const digest = await Crypto.digest(Crypto.CryptoDigestAlgorithm.SHA256, await image.bytes());

    // 3. metadata.json を書く
    const metadata = buildMetadata({
      observationId: input.observationId,
      pressedAtMs: input.pressedAtMs,
      samples: input.samples,
      location: input.location,
      headingAccuracy: input.headingAccuracy,
      exif: input.exif,
      width: input.width,
      height: input.height,
      device: input.device,
      imageSha256: toHex(digest),
    });
    new File(dir, METADATA_NAME).write(JSON.stringify(metadata));
    return metadata;
  } catch (e) {
    try {
      if (dir.exists) dir.delete();
    } catch {
      // 後始末の失敗は、元のエラーを優先して無視する（残っても起動時の掃除で消える）
    }
    throw e;
  }
}

/**
 * 起動時の掃除。metadata.json のない（または読めない）ディレクトリのうち、
 * 1 時間以上更新されていないものを消す。1 時間以内のものは保存の最中かもしれないので触らない。
 */
export async function cleanupIncomplete(nowMs: number = Date.now()): Promise<void> {
  for (const dir of listObservationDirs()) {
    if ((await readMetadata(dir.name)) !== null) continue;
    const info = dir.info();
    // ファイルを足すと更新時刻が進むので、新しいほうを見る
    const times = [info.modificationTime, info.creationTime].filter(
      (t): t is number => typeof t === "number",
    );
    // 時刻がわからないものは、保存の最中かもしれないので消さない
    if (times.length === 0) continue;
    if (nowMs - Math.max(...times) >= STALE_MS) dir.delete();
  }
}
