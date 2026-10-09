import { describe, expect, it } from "@jest/globals";
import { createApiClient } from "./api";

const SETTINGS = { serverUrl: "https://example.com", inviteCode: "code-123" };

const OBSERVATION = {
  observation_id: "abc",
  received_at: "2026-10-09T05:00:00+00:00",
  captured_at: "2026-10-09T04:59:00+09:00",
  user_guess: "rain",
  weather_at_capture: {
    category: "cloudy",
    weather_code: 3,
    temperature_c: 18.2,
    precipitation_mm: 0.0,
    cloud_cover_pct: 90,
  },
  answer: { result: "rain", source: "amedas" },
  correct: true,
};

const STATS = {
  observations_total: 42,
  answered_total: 30,
  guesses_total: 20,
  guesses_correct: 14,
  by_category: { clear: 20, cloudy: 15, rain: 5, unknown: 2 },
};

type Call = { url: string; init: RequestInit | undefined };

function fakeFetch(status: number, body: unknown, rawBody?: string) {
  const calls: Call[] = [];
  const impl = (async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(rawBody ?? JSON.stringify(body), { status });
  }) as unknown as typeof fetch;
  return { impl, calls };
}

describe("getObservation", () => {
  it("13.5 の例を型どおりに返し、認証ヘッダを付ける", async () => {
    const f = fakeFetch(200, OBSERVATION);
    const result = await createApiClient(f.impl).getObservation(SETTINGS, "abc");
    expect(result).toEqual({ kind: "ok", data: OBSERVATION });
    expect(f.calls[0]!.url).toBe("https://example.com/v1/observations/abc");
    expect(f.calls[0]!.init?.headers).toEqual({ Authorization: "Bearer code-123" });
  });

  it("weather_at_capture が null でもよい", async () => {
    const f = fakeFetch(200, { ...OBSERVATION, weather_at_capture: null, correct: null });
    const result = await createApiClient(f.impl).getObservation(SETTINGS, "abc");
    expect(result.kind).toBe("ok");
  });

  it("天気の数値が null でもよい", async () => {
    const weather = { ...OBSERVATION.weather_at_capture, temperature_c: null, precipitation_mm: null, cloud_cover_pct: null };
    const f = fakeFetch(200, { ...OBSERVATION, weather_at_capture: weather });
    const result = await createApiClient(f.impl).getObservation(SETTINGS, "abc");
    expect(result).toEqual({ kind: "ok", data: { ...OBSERVATION, weather_at_capture: weather } });
  });

  it("知らない項目は無視する", async () => {
    const f = fakeFetch(200, { ...OBSERVATION, extra: 1, answer: { ...OBSERVATION.answer, x: 2 } });
    const result = await createApiClient(f.impl).getObservation(SETTINGS, "abc");
    expect(result).toEqual({ kind: "ok", data: OBSERVATION });
  });

  it("id は URL エンコードする", async () => {
    const f = fakeFetch(200, OBSERVATION);
    await createApiClient(f.impl).getObservation(SETTINGS, "a/b");
    expect(f.calls[0]!.url).toBe("https://example.com/v1/observations/a%2Fb");
  });

  it("401 は auth、404 は not_found", async () => {
    expect(await createApiClient(fakeFetch(401, {}).impl).getObservation(SETTINGS, "a")).toEqual({ kind: "auth" });
    expect(await createApiClient(fakeFetch(404, {}).impl).getObservation(SETTINGS, "a")).toEqual({ kind: "not_found" });
  });

  it("5xx は error", async () => {
    const result = await createApiClient(fakeFetch(503, {}).impl).getObservation(SETTINGS, "a");
    expect(result).toEqual({ kind: "error", error: "HTTP 503" });
  });

  it("通信のエラーは error", async () => {
    const impl = (async () => {
      throw new TypeError("Network request failed");
    }) as unknown as typeof fetch;
    const result = await createApiClient(impl).getObservation(SETTINGS, "a");
    expect(result).toEqual({ kind: "error", error: "通信に失敗しました: Network request failed" });
  });

  it.each([
    ["必要な項目がない", { ...OBSERVATION, captured_at: undefined }],
    ["user_guess が想定外の値", { ...OBSERVATION, user_guess: "maybe" }],
    ["category が想定外の値", { ...OBSERVATION, weather_at_capture: { ...OBSERVATION.weather_at_capture, category: "snow" } }],
    ["数値が文字列", { ...OBSERVATION, weather_at_capture: { ...OBSERVATION.weather_at_capture, temperature_c: "18" } }],
    ["answer がない", { ...OBSERVATION, answer: undefined }],
    ["answer.result が想定外の値", { ...OBSERVATION, answer: { result: "snow", source: null } }],
    ["correct が数値", { ...OBSERVATION, correct: 1 }],
    ["オブジェクトではない", [1, 2]],
  ])("形が違う応答は error: %s", async (_name, body) => {
    const result = await createApiClient(fakeFetch(200, body).impl).getObservation(SETTINGS, "a");
    expect(result).toEqual({ kind: "error", error: "サーバーの応答の形が想定と違います" });
  });

  it("JSON ではない本文は形の違いとして error", async () => {
    const result = await createApiClient(fakeFetch(200, null, "<html>").impl).getObservation(SETTINGS, "a");
    expect(result).toEqual({ kind: "error", error: "サーバーの応答の形が想定と違います" });
  });
});

describe("listObservations", () => {
  const page = { observations: [OBSERVATION], next_before: "2026-10-09T04:00:00+09:00" };

  it("一覧を返す。before がなければ付けない", async () => {
    const f = fakeFetch(200, page);
    const result = await createApiClient(f.impl).listObservations(SETTINGS, { limit: 50 });
    expect(result).toEqual({ kind: "ok", data: page });
    expect(f.calls[0]!.url).toBe("https://example.com/v1/me/observations?limit=50");
  });

  it("before を URL エンコードする（+ が %2B になる）", async () => {
    const f = fakeFetch(200, { observations: [], next_before: null });
    const result = await createApiClient(f.impl).listObservations(SETTINGS, {
      limit: 20,
      before: "2026-10-09T04:00:00+09:00",
    });
    expect(result).toEqual({ kind: "ok", data: { observations: [], next_before: null } });
    expect(f.calls[0]!.url).toBe(
      "https://example.com/v1/me/observations?limit=20&before=2026-10-09T04%3A00%3A00%2B09%3A00",
    );
  });

  it("要素の形が違えば error", async () => {
    const body = { observations: [{ ...OBSERVATION, observation_id: 1 }], next_before: null };
    const result = await createApiClient(fakeFetch(200, body).impl).listObservations(SETTINGS, { limit: 50 });
    expect(result.kind).toBe("error");
  });

  it("observations が配列でなければ error", async () => {
    const result = await createApiClient(fakeFetch(200, { next_before: null }).impl).listObservations(SETTINGS, { limit: 50 });
    expect(result.kind).toBe("error");
  });

  it("401 は auth", async () => {
    const result = await createApiClient(fakeFetch(401, {}).impl).listObservations(SETTINGS, { limit: 50 });
    expect(result).toEqual({ kind: "auth" });
  });
});

describe("getStats", () => {
  it("13.5 の例を返す", async () => {
    const f = fakeFetch(200, STATS);
    const result = await createApiClient(f.impl).getStats(SETTINGS);
    expect(result).toEqual({ kind: "ok", data: STATS });
    expect(f.calls[0]!.url).toBe("https://example.com/v1/me/stats");
  });

  it("項目が足りなければ error", async () => {
    const { by_category: _omit, ...rest } = STATS;
    const result = await createApiClient(fakeFetch(200, rest).impl).getStats(SETTINGS);
    expect(result.kind).toBe("error");
    const bad = { ...STATS, by_category: { clear: 1, cloudy: 1, rain: 1 } };
    expect((await createApiClient(fakeFetch(200, bad).impl).getStats(SETTINGS)).kind).toBe("error");
  });

  it("5xx は error、401 は auth", async () => {
    expect(await createApiClient(fakeFetch(500, {}).impl).getStats(SETTINGS)).toEqual({ kind: "error", error: "HTTP 500" });
    expect(await createApiClient(fakeFetch(401, {}).impl).getStats(SETTINGS)).toEqual({ kind: "auth" });
  });
});
