import { afterEach, describe, expect, it, jest } from "@jest/globals";
import { classifyResponse, createFetchTransport, timeoutMs } from "./transport";

describe("classifyResponse", () => {
  it("200 と 201 は成功", () => {
    expect(classifyResponse(200, "")).toEqual({ kind: "ok" });
    expect(classifyResponse(201, "")).toEqual({ kind: "ok" });
  });

  it("401 は認証の失敗で、キューを止める", () => {
    expect(classifyResponse(401, "invalid token")).toEqual({
      kind: "blocked",
      reason: "auth",
      error: "HTTP 401: invalid token",
    });
  });

  it("403、404、405 は設定の誤りで、キューを止める", () => {
    for (const status of [403, 404, 405]) {
      expect(classifyResponse(status, "x")).toEqual({
        kind: "blocked",
        reason: "config",
        error: `HTTP ${status}: x`,
      });
    }
  });

  it("409、413、その他の 422、その他の 4xx は断られた", () => {
    for (const status of [400, 409, 413, 422, 451]) {
      expect(classifyResponse(status, "x")).toEqual({
        kind: "rejected",
        error: `HTTP ${status}: x`,
      });
    }
  });

  it("422 で captured_at がサーバー時刻より進んでいるものは、周を打ち切らない一時的な失敗", () => {
    const detail = "captured_at がサーバー時刻より未来です";
    expect(classifyResponse(422, detail)).toEqual({
      kind: "retry",
      error: `HTTP 422: ${detail}`,
      stopPass: false,
    });
  });

  it("422 の detail の判定は、短くする前の本文で行う", () => {
    const detail = `${"あ".repeat(300)}captured_at がサーバー時刻より未来です`;
    const r = classifyResponse(422, detail);
    expect(r.kind).toBe("retry");
    if (r.kind === "retry") expect(r.error).toBe(`HTTP 422: ${"あ".repeat(200)}`);
  });

  it("408、429、503 は周を打ち切る一時的な失敗", () => {
    for (const status of [408, 429, 503]) {
      expect(classifyResponse(status, "busy")).toEqual({
        kind: "retry",
        error: `HTTP ${status}: busy`,
        stopPass: true,
      });
    }
  });

  it("500、502、504 は周を打ち切らない一時的な失敗", () => {
    for (const status of [500, 502, 504]) {
      expect(classifyResponse(status, "busy")).toEqual({
        kind: "retry",
        error: `HTTP ${status}: busy`,
        stopPass: false,
      });
    }
  });

  it("detail が空なら状態コードだけ、長ければ 200 文字までにする", () => {
    expect(classifyResponse(409, "")).toEqual({ kind: "rejected", error: "HTTP 409" });
    const r = classifyResponse(422, "あ".repeat(500));
    expect(r.kind).toBe("rejected");
    if (r.kind === "rejected") expect(r.error).toBe(`HTTP 422: ${"あ".repeat(200)}`);
  });

  it("想定外の状態コードは送り直す", () => {
    expect(classifyResponse(204, "")).toEqual({ kind: "retry", error: "HTTP 204", stopPass: false });
    expect(classifyResponse(302, "")).toEqual({ kind: "retry", error: "HTTP 302", stopPass: false });
  });
});

describe("timeoutMs", () => {
  it("60 秒以上で、画像の大きさ ÷ 50KB/秒", () => {
    expect(timeoutMs(0)).toBe(60_000);
    expect(timeoutMs(1024 * 1024)).toBe(60_000);
    expect(timeoutMs(5 * 1024 * 1024)).toBe(102_400);
    expect(timeoutMs(10 * 1024 * 1024)).toBe(204_800);
  });
});

describe("createFetchTransport", () => {
  const image = new Blob([new Uint8Array([1, 2, 3])], { type: "image/jpeg" });

  function response(status: number, body: string): Response {
    return new Response(body, { status });
  }

  afterEach(() => {
    jest.useRealTimers();
  });

  it("PUT の URL、ヘッダー、本文を組み立てる", async () => {
    const fetchImpl = jest.fn<typeof fetch>(async () => response(201, "{}"));
    const outcome = await createFetchTransport(fetchImpl).put(
      "http://example.test",
      "tok",
      "id-1",
      '{"a":1}',
      image,
      3,
    );
    expect(outcome).toEqual({ kind: "ok" });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://example.test/v1/observations/id-1");
    expect(init?.method).toBe("PUT");
    expect(init?.headers).toEqual({ Authorization: "Bearer tok" });
    const form = init?.body as FormData;
    expect(form.get("metadata")).toBe('{"a":1}');
    expect(form.get("image")).toBeInstanceOf(Blob);
  });

  it("応答の JSON から detail を取り出す", async () => {
    const fetchImpl = jest.fn<typeof fetch>(async () =>
      response(409, JSON.stringify({ detail: "画像が違います" })),
    );
    const outcome = await createFetchTransport(fetchImpl).put("http://x", "t", "i", "{}", image, 3);
    expect(outcome).toEqual({ kind: "rejected", error: "HTTP 409: 画像が違います" });
  });

  it("通信のエラーは retry", async () => {
    const fetchImpl = jest.fn<typeof fetch>(async () => {
      throw new TypeError("Network request failed");
    });
    const outcome = await createFetchTransport(fetchImpl).put("http://x", "t", "i", "{}", image, 3);
    expect(outcome).toEqual({
      kind: "retry",
      error: "通信に失敗しました: Network request failed",
      stopPass: true,
    });
  });

  function hangingFetch() {
    return jest.fn<typeof fetch>(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
  }

  it("小さい画像は 60 秒で打ち切って、周も打ち切る retry にする", async () => {
    jest.useFakeTimers();
    const promise = createFetchTransport(hangingFetch()).put("http://x", "t", "i", "{}", image, 3);
    await jest.advanceTimersByTimeAsync(60_000);
    expect(await promise).toEqual({
      kind: "retry",
      error: "60秒たっても応答がありませんでした",
      stopPass: true,
    });
  });

  it("大きい画像は 60 秒では打ち切らず、大きさに応じた時間で打ち切る", async () => {
    jest.useFakeTimers();
    const size = 5 * 1024 * 1024;
    let settled = false;
    const promise = createFetchTransport(hangingFetch())
      .put("http://x", "t", "i", "{}", image, size)
      .then((o) => {
        settled = true;
        return o;
      });
    await jest.advanceTimersByTimeAsync(102_399);
    expect(settled).toBe(false);
    await jest.advanceTimersByTimeAsync(1);
    expect(await promise).toEqual({
      kind: "retry",
      error: "103秒たっても応答がありませんでした",
      stopPass: true,
    });
  });
});
