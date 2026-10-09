/**
 * @jest-environment node
 */
// 本物のサーバーにつないで確かめるテスト。仕様は docs/m3-app-capture.md の 8.1 節。
// SKY_E2E_URL と SKY_E2E_TOKEN があるときだけ動く。`npm run test:e2e` で流す。
import { createHash, randomUUID } from "node:crypto";
import http from "node:http";
import { beforeAll, describe, expect, it } from "@jest/globals";
import { buildMetadata } from "./metadata";
import { createFetchTransport } from "./transport";
import { createUploadQueue, type QueueItem, type Status, type Store } from "./uploadQueue";

const SERVER_URL = process.env.SKY_E2E_URL ?? "";
const TOKEN = process.env.SKY_E2E_TOKEN ?? "";
const UNREACHABLE_URL = "http://127.0.0.1:9";

// 8x8 画素の小さな有効な JPEG
const JPEG_HEX =
  "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123251d283a333d3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffdb0043011112121815182f1a1a2f634238426363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363ffc00011080008000803012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f009e8a28af60f38fffd9";
const JPEG = new Uint8Array(Buffer.from(JPEG_HEX, "hex"));

/** 先頭の SOI の直後にコメント（COM）を入れて、画素は同じで中身のバイト列だけ違う JPEG にする。 */
function jpegWithComment(): Uint8Array {
  const comment = [0xff, 0xfe, 0x00, 0x04, 0x41, 0x42];
  return new Uint8Array([...JPEG.slice(0, 2), ...comment, ...JPEG.slice(2)]);
}

/**
 * node:http で作った fetch。jest-expo のプリセットが、グローバルの fetch を
 * Expo の実装（テストでは通信できない）に差し替えてしまうので、通信だけここで代わりに行う。
 * 送る本文の組み立て（FormData）と応答の分類は、本物の createFetchTransport がやる。
 */
const nodeFetch: typeof fetch = async (input, init) => {
  const url = new URL(String(input));
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  let payload = Buffer.alloc(0);
  if (init?.body) {
    const body = new Response(init.body as FormData);
    headers["content-type"] = body.headers.get("content-type") ?? "";
    payload = Buffer.from(await body.arrayBuffer());
  }
  headers["content-length"] = String(payload.length);

  return new Promise<Response>((resolve, reject) => {
    const req = http.request(
      url,
      { method: init?.method ?? "GET", headers },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () =>
          resolve(new Response(Buffer.concat(chunks), { status: res.statusCode ?? 0 })),
        );
        res.on("error", reject);
      },
    );
    req.on("error", reject);
    init?.signal?.addEventListener("abort", () => req.destroy(new Error("aborted")));
    req.end(payload);
  });
};

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function makeMetadataJson(id: string, bytes: Uint8Array, pressedAtMs: number): string {
  const metadata = buildMetadata({
    observationId: id,
    pressedAtMs,
    samples: [],
    location: {
      coords: {
        latitude: 35.6812,
        longitude: 139.7671,
        accuracy: 5,
        altitude: 10,
        altitudeAccuracy: 3,
      },
      timestamp: pressedAtMs - 1000,
    },
    headingAccuracy: 3,
    exif: { ExposureTime: 0.01, ISOSpeedRatings: 100, FNumber: 1.8, FocalLength: 4.2 },
    width: 8,
    height: 8,
    device: { platform: "android", os_version: "14", model: "e2e", app_version: "0.0.0" },
    imageSha256: sha256(bytes),
  });
  return JSON.stringify(metadata);
}

type Entry = {
  capturedAt: string;
  metadataJson: string;
  bytes: Uint8Array;
  hasImage: boolean;
  status: Status;
};

/** メモリの上の偽の Store。画像は Blob で返す。 */
function createMemoryStore() {
  const entries = new Map<string, Entry>();
  const store: Store = {
    async list(): Promise<QueueItem[]> {
      return [...entries.entries()].map(([id, e]) => ({
        id,
        capturedAt: e.capturedAt,
        status: e.status,
      }));
    },
    async readMetadataJson(id) {
      return entries.get(id)!.metadataJson;
    },
    async image(id) {
      const e = entries.get(id);
      if (!e || !e.hasImage) return null;
      return { part: new Blob([new Uint8Array(e.bytes)], { type: "image/jpeg" }), size: e.bytes.length };
    },
    async setStatus(id, status) {
      entries.get(id)!.status = status;
    },
    async deleteImage(id) {
      entries.get(id)!.hasImage = false;
    },
  };
  return {
    store,
    add(id: string, bytes: Uint8Array, pressedAtMs: number) {
      entries.set(id, {
        capturedAt: new Date(pressedAtMs).toISOString(),
        metadataJson: makeMetadataJson(id, bytes, pressedAtMs),
        bytes,
        hasImage: true,
        status: { state: "pending", attempts: 0 },
      });
    },
    /** 送信済みのものを、画像つきの pending に戻す。 */
    resetToPending(id: string) {
      const e = entries.get(id)!;
      e.hasImage = true;
      e.status = { state: "pending", attempts: 0 };
    },
    get(id: string): Entry {
      return entries.get(id)!;
    },
  };
}

const describeE2e = SERVER_URL !== "" && TOKEN !== "" ? describe : describe.skip;

describeE2e("サーバーとつないだ送信", () => {
  const memory = createMemoryStore();
  const settings = { serverUrl: SERVER_URL, inviteCode: TOKEN };
  const fixedNow = Date.now();
  const queue = createUploadQueue({
    store: memory.store,
    transport: createFetchTransport(nodeFetch),
    getSettings: async () => ({ ...settings }),
    now: () => fixedNow,
  });

  const firstId = randomUUID();

  async function getObservation(id: string, token = TOKEN) {
    return nodeFetch(`${SERVER_URL}/v1/observations/${id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }

  beforeAll(() => {
    settings.serverUrl = SERVER_URL;
    settings.inviteCode = TOKEN;
  });

  it("1. 1件送ると sent になり、サーバーに received_at ができる", async () => {
    memory.add(firstId, JPEG, fixedNow);
    await queue.runNow();
    expect(memory.get(firstId).status.state).toBe("sent");
    expect(memory.get(firstId).hasImage).toBe(false);

    const response = await getObservation(firstId);
    expect(response.status).toBe(200);
    const body = (await response.json()) as { observation_id: string; received_at: string };
    expect(body.observation_id).toBe(firstId);
    expect(typeof body.received_at).toBe("string");
  });

  it("2. 同じものを pending に戻して送り直しても sent になる", async () => {
    memory.resetToPending(firstId);
    await queue.runNow();
    expect(memory.get(firstId).status.state).toBe("sent");
  });

  it("3. 通じない URL では pending のまま待ち、あとで正しい URL にすると sent になる", async () => {
    const id = randomUUID();
    memory.add(id, JPEG, fixedNow + 1000);

    settings.serverUrl = UNREACHABLE_URL;
    await queue.runNow();
    const status = memory.get(id).status;
    expect(status.state).toBe("pending");
    expect(status.attempts).toBe(1);
    expect(status.next_attempt_at).toBe(new Date(fixedNow + 30_000).toISOString());

    settings.serverUrl = SERVER_URL;
    await queue.runNow();
    expect(memory.get(id).status.state).toBe("sent");
  });

  it("4. 違う招待コードでは pending のまま、キューが止まる", async () => {
    const id = randomUUID();
    memory.add(id, JPEG, fixedNow + 2000);

    settings.inviteCode = "wrong-" + TOKEN;
    await queue.runNow();
    expect(memory.get(id).status.state).toBe("pending");
    expect(memory.get(id).status.attempts).toBe(0);
    expect(queue.getState().authBlocked).toBe(true);

    // 招待コードを直せば再開して送られる
    settings.inviteCode = TOKEN;
    await queue.resumeAfterAuthFix();
    expect(queue.getState().authBlocked).toBe(false);
    expect(memory.get(id).status.state).toBe("sent");
  });

  it("5. 同じ ID で画像だけ違うものは rejected（409）になる", async () => {
    // 1. で送った ID に、中身のバイト列だけ違う画像を入れる
    memory.add(firstId, jpegWithComment(), fixedNow);
    await queue.runNow();
    const status = memory.get(firstId).status;
    expect(status.state).toBe("rejected");
    expect(status.last_error).toContain("409");
  });
});
