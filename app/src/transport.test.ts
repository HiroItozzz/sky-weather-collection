import { afterEach, describe, expect, it, jest } from "@jest/globals";
import { classifyResponse, createFetchTransport } from "./transport";

describe("classifyResponse", () => {
  it("200 と 201 は成功", () => {
    expect(classifyResponse(200, "")).toEqual({ kind: "ok" });
    expect(classifyResponse(201, "")).toEqual({ kind: "ok" });
  });

  it("401 は認証の失敗", () => {
    expect(classifyResponse(401, "invalid token")).toEqual({ kind: "auth" });
  });

  it("409、413、422、その他の 4xx は断られた", () => {
    for (const status of [400, 403, 404, 409, 413, 422, 451]) {
      expect(classifyResponse(status, "x")).toEqual({
        kind: "rejected",
        error: `HTTP ${status}: x`,
      });
    }
  });

  it("408、429、5xx は一時的な失敗", () => {
    for (const status of [408, 429, 500, 502, 503, 504]) {
      expect(classifyResponse(status, "busy")).toEqual({
        kind: "retry",
        error: `HTTP ${status}: busy`,
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
    expect(classifyResponse(204, "").kind).toBe("retry");
    expect(classifyResponse(302, "").kind).toBe("retry");
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
    const outcome = await createFetchTransport(fetchImpl).put("http://x", "t", "i", "{}", image);
    expect(outcome).toEqual({ kind: "rejected", error: "HTTP 409: 画像が違います" });
  });

  it("通信のエラーは retry", async () => {
    const fetchImpl = jest.fn<typeof fetch>(async () => {
      throw new TypeError("Network request failed");
    });
    const outcome = await createFetchTransport(fetchImpl).put("http://x", "t", "i", "{}", image);
    expect(outcome).toEqual({
      kind: "retry",
      error: "通信に失敗しました: Network request failed",
    });
  });

  it("60 秒で打ち切って retry にする", async () => {
    jest.useFakeTimers();
    const fetchImpl = jest.fn<typeof fetch>(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const promise = createFetchTransport(fetchImpl).put("http://x", "t", "i", "{}", image);
    await jest.advanceTimersByTimeAsync(60_000);
    expect(await promise).toEqual({
      kind: "retry",
      error: "60秒たっても応答がありませんでした",
    });
  });
});
