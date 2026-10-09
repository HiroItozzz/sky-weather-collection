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
  // 指定した操作を 1 回だけ失敗させる
  const failOnce = new Set<keyof Store>();
  function maybeFail(op: keyof Store): void {
    if (failOnce.delete(op)) throw new Error(`${op} に失敗`);
  }
  const store: Store = {
    async list(): Promise<QueueItem[]> {
      maybeFail("list");
      return [...data.entries()].map(([id, e]) => ({
        id,
        capturedAt: e.capturedAt,
        status: e.status,
      }));
    },
    async readMetadataJson(id) {
      maybeFail("readMetadataJson");
      return `{"id":"${id}"}`;
    },
    async image(id) {
      maybeFail("image");
      calls.push(`image:${id}`);
      const e = data.get(id);
      if (!e || e.image === null) return null;
      return { part: new Blob(["x"]), size: e.image.size };
    },
    async setStatus(id, status) {
      maybeFail("setStatus");
      calls.push(`setStatus:${id}:${status.state}`);
      const e = data.get(id);
      if (e) e.status = status;
    },
    async deleteImage(id) {
      maybeFail("deleteImage");
      calls.push(`deleteImage:${id}`);
      const e = data.get(id);
      if (e) e.image = null;
    },
  };
  return { store, data, calls, failOnce };
}

function makeTransport(respond: (id: string) => Outcome | Promise<Outcome>) {
  const sent: string[] = [];
  const args: { baseUrl: string; token: string; image: ImagePart; imageSize: number }[] = [];
  const transport: Transport = {
    async put(baseUrl, token, id, _metadataJson, image, imageSize) {
      sent.push(id);
      args.push({ baseUrl, token, image, imageSize });
      return respond(id);
    },
  };
  return { transport, sent, args };
}

function blocked(reason: "auth" | "config"): Outcome {
  return { kind: "blocked", reason, error: reason === "auth" ? "HTTP 401" : "HTTP 404" };
}

function retry(stopPass: boolean, error = "HTTP 503"): Outcome {
  return { kind: "retry", error, stopPass };
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

  it("画像の大きさを transport に渡す", async () => {
    const q = setup({ a: { image: { size: 1234 } } });
    await q.queue.runNow();
    expect(q.args[0].imageSize).toBe(1234);
  });

  it("401 は pending のまま、キューが止まる", async () => {
    const q = setup({ a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } }, () => blocked("auth"));
    await q.queue.runNow();
    expect(q.sent).toEqual(["a"]);
    expect(q.data.get("a")?.status).toEqual({ state: "pending", attempts: 0 });
    expect(q.queue.getState()).toEqual({ pendingCount: 2, blockedReason: "auth", running: false });

    // 止まっている間は送らない
    await q.queue.runNow();
    await q.queue.runDue();
    expect(q.sent).toEqual(["a"]);
  });

  it("403、404、405（設定の誤り）も pending のまま、キューが止まる", async () => {
    const q = setup({ a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } }, () => blocked("config"));
    await q.queue.runNow();
    expect(q.sent).toEqual(["a"]);
    expect(q.data.get("a")?.status).toEqual({ state: "pending", attempts: 0 });
    expect(q.queue.getState()).toEqual({ pendingCount: 2, blockedReason: "config", running: false });

    await q.queue.runNow();
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

  it("stopPass が true の一時的な失敗は attempts を増やして待ち時間を決め、周を打ち切る", async () => {
    const q = setup(
      { a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } },
      () => retry(true),
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
    expect(q.queue.getState()).toMatchObject({ pendingCount: 2, blockedReason: null });
  });

  it("stopPass が false の一時的な失敗は、その1件を待ちに回して次へ進む", async () => {
    const q = setup(
      { a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } },
      (id) => (id === "a" ? retry(false, "HTTP 500") : { kind: "ok" }),
    );
    await q.queue.runNow();
    expect(q.sent).toEqual(["a", "b"]);
    expect(q.data.get("a")?.status).toEqual({
      state: "pending",
      attempts: 1,
      last_attempt_at: new Date(T0).toISOString(),
      next_attempt_at: new Date(T0 + 30_000).toISOString(),
      last_error: "HTTP 500",
    });
    expect(q.data.get("b")?.status.state).toBe("sent");
    expect(q.queue.getState().pendingCount).toBe(1);
  });

  it("失敗が続くと待ち時間が延びる", async () => {
    const q = setup({ a: {} }, () => retry(true, "x"));
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
    release(retry(true, "x"));
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

  it("resumeAfterSettingsSaved は停止を解いて送り直す", async () => {
    for (const reason of ["auth", "config"] as const) {
      let ok = false;
      const q = setup({ a: {} }, () => (ok ? { kind: "ok" } : blocked(reason)));
      await q.queue.runNow();
      expect(q.queue.getState().blockedReason).toBe(reason);
      ok = true;
      await q.queue.resumeAfterSettingsSaved();
      expect(q.queue.getState()).toEqual({ pendingCount: 0, blockedReason: null, running: false });
      expect(q.data.get("a")?.status.state).toBe("sent");
    }
  });

  it("resumeAfterSettingsSaved は HTTP 403・404・405 で rejected になっていたものだけ pending に戻して送る", async () => {
    const q = setup({
      a: { capturedAt: "2026-10-09T01:00:00.000Z", status: { state: "rejected", attempts: 2, last_error: "HTTP 403: forbidden" } },
      b: { capturedAt: "2026-10-09T02:00:00.000Z", status: { state: "rejected", attempts: 1, last_error: "HTTP 404" } },
      c: { capturedAt: "2026-10-09T03:00:00.000Z", status: { state: "rejected", attempts: 0, last_error: "HTTP 405: x" } },
      d: { capturedAt: "2026-10-09T04:00:00.000Z", status: { state: "rejected", attempts: 0, last_error: "HTTP 409: 重複" } },
      e: { capturedAt: "2026-10-09T05:00:00.000Z", status: { state: "rejected", attempts: 0, last_error: "画像が見つかりません" } },
    });
    await q.queue.resumeAfterSettingsSaved();
    expect(q.sent).toEqual(["a", "b", "c"]);
    expect(q.data.get("a")?.status.state).toBe("sent");
    expect(q.data.get("a")?.status.attempts).toBe(0);
    expect(q.data.get("d")?.status.state).toBe("rejected");
    expect(q.data.get("e")?.status.state).toBe("rejected");
  });

  it("送っている間に設定が保存し直されたら、blocked の応答でも止めずにその周を終える", async () => {
    let release: (o: Outcome) => void = () => {};
    let first = true;
    const q = setup({ a: {}, b: { capturedAt: "2026-10-09T02:00:00.000Z" } }, () => {
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
    // 古い設定で送っている最中に、設定が保存し直される
    const p2 = q.queue.resumeAfterSettingsSaved();
    release(blocked("auth"));
    await Promise.all([p1, p2]);

    expect(q.queue.getState().blockedReason).toBeNull();
    // 止めずに終えたあと、予約されていた次の周で a も b も送れる
    expect(q.sent).toEqual(["a", "a", "b"]);
    expect(q.data.get("a")?.status.state).toBe("sent");
    expect(q.data.get("b")?.status.state).toBe("sent");
  });

  it("世代が変わっていなければ、blocked の応答で止める（保存の前に始まった周でも、後に始まった周は止まる）", async () => {
    const q = setup({ a: {} }, () => blocked("config"));
    await q.queue.resumeAfterSettingsSaved();
    expect(q.queue.getState().blockedReason).toBe("config");
  });

  describe("Store が例外を投げたとき", () => {
    const ops = ["list", "image", "setStatus", "readMetadataJson", "deleteImage"] as const;

    for (const op of ops) {
      it(`${op} が失敗しても running が false に戻り、次の runNow がまた走る`, async () => {
        const q = setup({ a: {} });
        q.failOnce.add(op);
        // readMetadataJson の失敗は retry として扱われ、例外にはならない
        const result = await q.queue.runNow().then(
          () => "resolved",
          () => "rejected",
        );
        expect(result).toBe(op === "readMetadataJson" ? "resolved" : "rejected");
        expect(q.queue.getState().running).toBe(false);

        // 失敗は 1 回だけなので、次は走って送れる
        await q.queue.runNow();
        expect(q.queue.getState().running).toBe(false);
        expect(q.data.get("a")?.status.state).toBe("sent");
      });
    }
  });

  it("状態の変化を購読できる", async () => {
    const q = setup({ a: {} });
    const seen: QueueState[] = [];
    const unsubscribe = q.queue.subscribe((s) => seen.push(s));
    await q.queue.runNow();
    expect(seen[0]).toEqual({ pendingCount: 0, blockedReason: null, running: true });
    expect(seen.map((s) => s.pendingCount)).toContain(1);
    expect(seen[seen.length - 1]).toEqual({ pendingCount: 0, blockedReason: null, running: false });
    unsubscribe();
    const count = seen.length;
    await q.queue.refresh();
    await q.queue.runNow();
    expect(seen.length).toBe(count);
  });
});
