// 送信の段取り（再送キュー）。仕様は docs/m3-app-capture.md の 4 節と 6 節。
// 保存先と送信は引数で受け取るので、Expo に依存せずテストできる。
import type { Settings } from "./settings";
import type { ImagePart, Outcome, Transport } from "./transport";

export type { ImagePart, Outcome, Transport } from "./transport";

export type Status = {
  state: "pending" | "sent" | "rejected";
  /** 一時的に失敗した回数 */
  attempts: number;
  last_attempt_at?: string;
  next_attempt_at?: string;
  last_error?: string;
  sent_at?: string;
};

export type QueueItem = { id: string; capturedAt: string; status: Status };

export interface Store {
  /** metadata.json のそろったものだけ */
  list(): Promise<QueueItem[]>;
  readMetadataJson(id: string): Promise<string>;
  /** 画像がなければ null */
  image(id: string): Promise<{ part: ImagePart; size: number } | null>;
  setStatus(id: string, status: Status): Promise<void>;
  deleteImage(id: string): Promise<void>;
}

export type QueueState = {
  /** 状態が pending のものの件数 */
  pendingCount: number;
  /** キューを止めている理由。auth は 401、config は 403・404・405。止まっていなければ null */
  blockedReason: "auth" | "config" | null;
  /** 送信の周が走っているか */
  running: boolean;
};

export type UploadQueueDeps = {
  store: Store;
  transport: Transport;
  /** 未設定の項目は空文字列 */
  getSettings: () => Promise<Settings>;
  /** 現在時刻（UNIX ミリ秒） */
  now: () => number;
};

export interface UploadQueue {
  /** next_attempt_at を無視して、pending をすべて送る。 */
  runNow(): Promise<void>;
  /** next_attempt_at を過ぎた pending だけを送る。 */
  runDue(): Promise<void>;
  /** rejected を pending に戻す（すぐには送らない）。 */
  retryRejected(id: string): Promise<void>;
  /**
   * 設定を保存し直したあとに呼ぶ。設定の世代を1増やし、停止を解き、
   * 古い版が 403・404・405 で rejected にしたものを pending に戻して送る。
   */
  resumeAfterSettingsSaved(): Promise<void>;
  /** 未送信の件数を数え直す。 */
  refresh(): Promise<void>;
  getState(): QueueState;
  subscribe(listener: (state: QueueState) => void): () => void;
}

export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const BASE_DELAY_MS = 30_000;
const MAX_DELAY_MS = 30 * 60_000;
/** 古い版のアプリが rejected にしていた、設定の誤りによる応答 */
const LEGACY_CONFIG_ERROR_PREFIXES = ["HTTP 403", "HTTP 404", "HTTP 405"];

/** 一時的な失敗の待ち時間。attempts は失敗を数えたあとの回数（1 以上）。 */
export function backoffMs(attempts: number): number {
  // 指数が大きくなりすぎないよう、頭打ちになる手前で止める
  const exponent = Math.min(Math.max(attempts, 1) - 1, 20);
  return Math.min(BASE_DELAY_MS * 2 ** exponent, MAX_DELAY_MS);
}

function byCapturedAt(a: QueueItem, b: QueueItem): number {
  const diff = Date.parse(a.capturedAt) - Date.parse(b.capturedAt);
  if (diff !== 0 && !Number.isNaN(diff)) return diff;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

export function createUploadQueue(deps: UploadQueueDeps): UploadQueue {
  const { store, transport, getSettings, now } = deps;

  let state: QueueState = { pendingCount: 0, blockedReason: null, running: false };
  const listeners = new Set<(state: QueueState) => void>();

  function update(patch: Partial<QueueState>): void {
    const next = { ...state, ...patch };
    if (
      next.pendingCount === state.pendingCount &&
      next.blockedReason === state.blockedReason &&
      next.running === state.running
    ) {
      return;
    }
    state = next;
    for (const listener of [...listeners]) listener(state);
  }

  // 設定を保存し直すたびに1増える。古い設定で送った応答でキューを止め直さないために使う
  let settingsGeneration = 0;

  /** 1 周する。キューを止める応答か、通信の状態が悪いことを示す失敗が出たら、残りは送らずに打ち切る。 */
  async function pass(all: boolean): Promise<void> {
    const startGeneration = settingsGeneration;
    const items = await store.list();
    let pendingCount = items.filter((i) => i.status.state === "pending").length;
    update({ pendingCount });

    const settings = await getSettings();
    if (settings.serverUrl === "" || settings.inviteCode === "") return;

    const nowMs = now();
    const targets = items
      .filter((i) => i.status.state === "pending")
      .filter((i) => {
        if (all) return true;
        const next = i.status.next_attempt_at;
        return next === undefined || Date.parse(next) <= nowMs;
      })
      .sort(byCapturedAt);

    for (const item of targets) {
      const { id, status } = item;
      const attemptAt = new Date(now()).toISOString();

      const reject = async (error: string): Promise<void> => {
        await store.setStatus(id, {
          state: "rejected",
          attempts: status.attempts,
          last_attempt_at: attemptAt,
          last_error: error,
        });
        pendingCount -= 1;
        update({ pendingCount });
      };

      const image = await store.image(id);
      if (image === null) {
        await reject("画像が見つかりません");
        continue;
      }
      if (image.size > MAX_IMAGE_BYTES) {
        await reject("画像が10MBを超えています");
        continue;
      }

      let outcome: Outcome;
      try {
        const metadataJson = await store.readMetadataJson(id);
        outcome = await transport.put(
          settings.serverUrl,
          settings.inviteCode,
          id,
          metadataJson,
          image.part,
          image.size,
        );
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        outcome = { kind: "retry", error: `送信の準備に失敗しました: ${message}`, stopPass: false };
      }

      if (outcome.kind === "ok") {
        // 先に sent にしてから画像を消す（逆だと、落ちたときに画像のない pending が残る）
        await store.setStatus(id, {
          state: "sent",
          attempts: status.attempts,
          last_attempt_at: attemptAt,
          sent_at: attemptAt,
        });
        pendingCount -= 1;
        update({ pendingCount });
        await store.deleteImage(id);
      } else if (outcome.kind === "blocked") {
        // 送っている間に設定が保存し直されていたら、古い設定への応答なので止めずにこの周を終える
        if (settingsGeneration === startGeneration) update({ blockedReason: outcome.reason });
        return;
      } else if (outcome.kind === "rejected") {
        await reject(outcome.error);
      } else {
        const attempts = status.attempts + 1;
        await store.setStatus(id, {
          state: "pending",
          attempts,
          last_attempt_at: attemptAt,
          next_attempt_at: new Date(now() + backoffMs(attempts)).toISOString(),
          last_error: outcome.error,
        });
        if (outcome.stopPass) return;
      }
    }
  }

  async function refresh(): Promise<void> {
    const items = await store.list();
    update({ pendingCount: items.filter((i) => i.status.state === "pending").length });
  }

  let current: Promise<void> | null = null;
  // 走っている最中に呼ばれたときの、終わった後にもう 1 周する予約
  let rerun: { all: boolean } | null = null;

  async function loop(firstAll: boolean): Promise<void> {
    update({ running: true });
    try {
      let all = firstAll;
      for (;;) {
        if (state.blockedReason === null) await pass(all);
        if (rerun === null) break;
        all = rerun.all;
        rerun = null;
      }
    } finally {
      rerun = null;
      current = null;
      update({ running: false });
    }
  }

  function request(all: boolean): Promise<void> {
    if (current !== null) {
      rerun = { all: (rerun?.all ?? false) || all };
      return current;
    }
    current = loop(all);
    return current;
  }

  return {
    runNow: () => request(true),
    runDue: () => request(false),

    async retryRejected(id) {
      const items = await store.list();
      const item = items.find((i) => i.id === id);
      if (item === undefined || item.status.state !== "rejected") return;
      await store.setStatus(id, { state: "pending", attempts: 0 });
      await refresh();
    },

    async resumeAfterSettingsSaved() {
      settingsGeneration += 1;
      update({ blockedReason: null });
      const items = await store.list();
      for (const item of items) {
        const error = item.status.last_error ?? "";
        if (
          item.status.state === "rejected" &&
          LEGACY_CONFIG_ERROR_PREFIXES.some((prefix) => error.startsWith(prefix))
        ) {
          await store.setStatus(item.id, { state: "pending", attempts: 0 });
        }
      }
      await request(true);
    },

    refresh,

    getState: () => state,

    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
