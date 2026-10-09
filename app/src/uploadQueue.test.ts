import { describe, expect, it, jest } from "@jest/globals";
import {
  MAX_IMAGE_BYTES,
  backoffMs,
  createUploadQueue,
  type ImagePart,
  type Outcome,
  type QueueItem,
  type QueueState,
  type Status,
  type Store,
  type Transport,
} from "./uploadQueue";

const T0 = Date.parse("2026-10-09T03:00:00.000Z");

type Entry = { capturedAt: string; status: Status; image: { size: number } | null };

function makeStore(entries: Record<string, Partial<Entry>>) {
  const data = new Map<string, Entry>();
  for (const [id, e] of Object.entries(entries)) {
    data.set(id, {
      capturedAt: e.capturedAt ?? "2026-10-09T01:00:00.000Z",
      status: e.status ?? { state: "pending", attempts: 0 },
      image: e.image === undefined ? { size: 100 } : e.image,
    });
  }
  const calls: string[] = [];
  const store: Store = {
    async list(): Promise<QueueItem[]> {
      return [...data.entries()].map(([id, e]) => ({
        id,
        capturedAt: e.capturedAt,
        status: e.status,
      }));
    },
    async readMetadataJson(id) {
      return `{"id":"${id}"}`;
    },
    async image(id) {
      calls.push(`image:${id}`);
      const e = data.get(id);
      if (!e || e.image === null) return null;
      return { part: new Blob(["x"]), size: e.image.size };
    },
    async setStatus(id, status) {
      calls.push(`setStatus:${id}:${status.state}`);
      const e = data.get(id);
      if (e) e.status = status;
    },
    async deleteImage(id) {
      calls.push(`deleteImage:${id}`);
      const e = data.get(id);
      if (e) e.image = null;
    },
  };
  return { store, data, calls };
}

function makeTransport(respond: (id: string) => Outcome | Promise<Outcome>) {
  const sent: string[] = [];
  const args: { baseUrl: string; token: string; image: ImagePart }[] = [];
  const transport: Transport = {
    async put(baseUrl, token, id, _metadataJson, image) {
      sent.push(id);
      args.push({ baseUrl, token, image });
      return respond(id);
    },
  };
  return { transport, sent, args };
}

const SETTINGS = { serverUrl: "http://server.test", inviteCode: "code" };

function setup(
  entries: Record<string, Partial<Entry>>,
  respond: (id: string) => Outcome | Promise<Outcome> = () => ({ kind: "ok" }),
  settings = SETTINGS,
) {
  const s = makeStore(entries);
  const t = makeTransport(respond);
  let nowMs = T0;
  const queue = createUploadQueue({
    store: s.store,
    transport: t.transport,
    getSettings: async () => settings,
    now: () => nowMs,
  });
  return { ...s, ...t, queue, setNow: (ms: number) => (nowMs = ms) };
}

describe("backoffMs", () => {
  it("30 秒から倍になり、30 分で頭打ち", () => {
    expect([1, 2, 3, 4].map(backoffMs)).toEqual([30_000, 60_000, 120_000, 240_000]);
    expect(backoffMs(6)).toBe(960_000);
    expect(backoffMs(7)).toBe(1_800_000);
    expect(backoffMs(8)).toBe(1_800_000);
    expect(backoffMs(1000)).toBe(1_800_000);
  });
});

describe("createUploadQueue の 4.2 の表", () => {
  it("成功なら sent にしてから画像を消す", async () => {
    const q = setup({ a: {} });
    await q.queue.runNow();
    expect(q.calls).toEqual(["image:a", "setStatus:a:sent", "deleteImage:a"]);
    expect(q.data.get("a")?.status).toEqual({
      state: "sent",
      attempts: 0,
      last_attempt_at: new Date(T0).toISOString(),
      sent_at: new Date(T0).toISOString(),
    });
    expect(q.args[0]).toMatchObject({ baseUrl: "http://server.test", token: "code" });
    expect(q.queue.getState().pendingCount).toBe(0);
  });

  it("401 は pending のまま、キューが止まる", async () => {
    const q = setup({ a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } }, () => ({ kind: "auth" }));
    await q.queue.runNow();
    expect(q.sent).toEqual(["a"]);
    expect(q.data.get("a")?.status).toEqual({ state: "pending", attempts: 0 });
    expect(q.queue.getState()).toEqual({ pendingCount: 2, authBlocked: true, running: false });

    // 止まっている間は送らない
    await q.queue.runNow();
    await q.queue.runDue();
    expect(q.sent).toEqual(["a"]);
  });

  it("断られたら rejected にして、次の件へ進む", async () => {
    const q = setup(
      { a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } },
      (id) => (id === "a" ? { kind: "rejected", error: "HTTP 409: 重複" } : { kind: "ok" }),
    );
    await q.queue.runNow();
    expect(q.sent).toEqual(["a", "b"]);
    expect(q.data.get("a")?.status).toMatchObject({
      state: "rejected",
      last_error: "HTTP 409: 重複",
    });
    expect(q.data.get("b")?.status.state).toBe("sent");
    expect(q.queue.getState().pendingCount).toBe(0);

    // rejected は自動では送り直さない
    await q.queue.runNow();
    expect(q.sent).toEqual(["a", "b"]);
  });

  it("一時的な失敗は attempts を増やして待ち時間を決め、周を打ち切る", async () => {
    const q = setup(
      { a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } },
      () => ({ kind: "retry", error: "HTTP 503" }),
    );
    await q.queue.runNow();
    expect(q.sent).toEqual(["a"]);
    expect(q.data.get("a")?.status).toEqual({
      state: "pending",
      attempts: 1,
      last_attempt_at: new Date(T0).toISOString(),
      next_attempt_at: new Date(T0 + 30_000).toISOString(),
      last_error: "HTTP 503",
    });
    expect(q.data.get("b")?.status).toEqual({ state: "pending", attempts: 0 });
    expect(q.queue.getState()).toMatchObject({ pendingCount: 2, authBlocked: false });
  });

  it("失敗が続くと待ち時間が延びる", async () => {
    const q = setup({ a: {} }, () => ({ kind: "retry", error: "x" }));
    const waits: number[] = [];
    for (let i = 0; i < 3; i++) {
      await q.queue.runNow();
      const next = q.data.get("a")?.status.next_attempt_at ?? "";
      waits.push(Date.parse(next) - T0);
    }
    expect(waits).toEqual([30_000, 60_000, 120_000]);
    expect(q.data.get("a")?.status.attempts).toBe(3);
  });
});

describe("createUploadQueue の段取り", () => {
  it("captured_at の昇順に送る", async () => {
    const q = setup({
      c: { capturedAt: "2026-10-09T03:00:00.000Z" },
      a: { capturedAt: "2026-10-09T01:00:00.000Z" },
      b: { capturedAt: "2026-10-09T02:00:00.000Z" },
    });
    await q.queue.runNow();
    expect(q.sent).toEqual(["a", "b", "c"]);
  });

  it("runDue は期限の来たものだけ、runNow は期限を無視する", async () => {
    const future = new Date(T0 + 60_000).toISOString();
    const past = new Date(T0 - 1000).toISOString();
    const q = setup({
      wait: { capturedAt: "2026-10-09T01:00:00.000Z", status: { state: "pending", attempts: 1, next_attempt_at: future } },
      due: { capturedAt: "2026-10-09T02:00:00.000Z", status: { state: "pending", attempts: 1, next_attempt_at: past } },
      fresh: { capturedAt: "2026-10-09T03:00:00.000Z" },
    });
    await q.queue.runDue();
    expect(q.sent).toEqual(["due", "fresh"]);

    q.setNow(T0 + 60_000);
    await q.queue.runDue();
    expect(q.sent).toEqual(["due", "fresh", "wait"]);
  });

  it("runNow は next_attempt_at を無視する", async () => {
    const future = new Date(T0 + 60_000).toISOString();
    const q = setup({
      a: { status: { state: "pending", attempts: 1, next_attempt_at: future } },
    });
    await q.queue.runNow();
    expect(q.sent).toEqual(["a"]);
  });

  it("同時に 2 つは走らせず、走っている最中の呼び出しは終わった後にもう 1 周する", async () => {
    let release: (o: Outcome) => void = () => {};
    let first = true;
    const q = setup({ a: {} }, () => {
      if (first) {
        first = false;
        return new Promise<Outcome>((resolve) => {
          release = resolve;
        });
      }
      return { kind: "ok" };
    });

    const p1 = q.queue.runNow();
    await new Promise((r) => setTimeout(r, 0));
    expect(q.queue.getState().running).toBe(true);
    const p2 = q.queue.runNow();
    const p3 = q.queue.runDue();
    expect(q.sent).toEqual(["a"]);

    // 1 周目は a が失敗する想定ではなく、通信のエラーで返す
    release({ kind: "retry", error: "x" });
    await Promise.all([p1, p2, p3]);
    // 2 周目（runNow 相当）で a をもう一度送って成功する。呼び出しが 3 つでも 2 周だけ
    expect(q.sent).toEqual(["a", "a"]);
    expect(q.data.get("a")?.status.state).toBe("sent");
    expect(q.queue.getState().running).toBe(false);
  });

  it("10MB を超える画像は送らずに rejected にする", async () => {
    const q = setup({
      big: { image: { size: MAX_IMAGE_BYTES + 1 } },
      ok: { capturedAt: "2026-10-09T02:00:00.000Z", image: { size: MAX_IMAGE_BYTES } },
    });
    await q.queue.runNow();
    expect(q.sent).toEqual(["ok"]);
    expect(q.data.get("big")?.status).toMatchObject({
      state: "rejected",
      last_error: "画像が10MBを超えています",
    });
  });

  it("画像のない pending は rejected にする", async () => {
    const q = setup({ a: { image: null } });
    await q.queue.runNow();
    expect(q.sent).toEqual([]);
    expect(q.data.get("a")?.status).toMatchObject({
      state: "rejected",
      last_error: "画像が見つかりません",
    });
  });

  it("URL か招待コードが未設定なら送らない", async () => {
    for (const settings of [
      { serverUrl: "", inviteCode: "code" },
      { serverUrl: "http://server.test", inviteCode: "" },
    ]) {
      const q = setup({ a: {} }, () => ({ kind: "ok" }), settings);
      await q.queue.runNow();
      expect(q.sent).toEqual([]);
      expect(q.data.get("a")?.status.state).toBe("pending");
      expect(q.queue.getState().pendingCount).toBe(1);
    }
  });

  it("retryRejected は rejected を pending に戻す", async () => {
    const q = setup({
      a: { status: { state: "rejected", attempts: 2, last_error: "HTTP 409" } },
      b: { status: { state: "sent", attempts: 0 } },
    });
    await q.queue.refresh();
    expect(q.queue.getState().pendingCount).toBe(0);
    await q.queue.retryRejected("a");
    await q.queue.retryRejected("b");
    expect(q.data.get("a")?.status).toEqual({ state: "pending", attempts: 0 });
    expect(q.data.get("b")?.status.state).toBe("sent");
    expect(q.queue.getState().pendingCount).toBe(1);
  });

  it("resumeAfterAuthFix は停止を解いて送り直す", async () => {
    let ok = false;
    const q = setup({ a: {} }, () => (ok ? { kind: "ok" } : { kind: "auth" }));
    await q.queue.runNow();
    expect(q.queue.getState().authBlocked).toBe(true);
    ok = true;
    await q.queue.resumeAfterAuthFix();
    expect(q.queue.getState()).toEqual({ pendingCount: 0, authBlocked: false, running: false });
    expect(q.data.get("a")?.status.state).toBe("sent");
  });

  it("状態の変化を購読できる", async () => {
    const q = setup({ a: {} });
    const seen: QueueState[] = [];
    const unsubscribe = q.queue.subscribe((s) => seen.push(s));
    await q.queue.runNow();
    expect(seen[0]).toEqual({ pendingCount: 0, authBlocked: false, running: true });
    expect(seen.map((s) => s.pendingCount)).toContain(1);
    expect(seen[seen.length - 1]).toEqual({ pendingCount: 0, authBlocked: false, running: false });
    unsubscribe();
    const count = seen.length;
    await q.queue.refresh();
    await q.queue.runNow();
    expect(seen.length).toBe(count);
  });
});
