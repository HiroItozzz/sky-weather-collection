import { describe, expect, it } from "@jest/globals";
import type { ApiResult, ObservationView, WeatherAtCapture } from "./api";
import { pollWeather } from "./pollWeather";

const WEATHER: WeatherAtCapture = {
  category: "cloudy",
  weather_code: 3,
  temperature_c: 18.2,
  precipitation_mm: 0,
  cloud_cover_pct: 90,
};

function view(weather: WeatherAtCapture | null): ApiResult<ObservationView> {
  return {
    kind: "ok",
    data: {
      observation_id: "id",
      received_at: "2026-10-09T05:00:00Z",
      captured_at: "2026-10-09T05:00:00Z",
      user_guess: null,
      weather_at_capture: weather,
      answer: { result: "unknown", source: null },
      correct: null,
    },
  };
}

/** 偽の時計。sleep で時間が進む。 */
function setup(responses: ApiResult<ObservationView>[], controller = new AbortController()) {
  let t = 1_000_000;
  let calls = 0;
  const sleeps: number[] = [];
  const options = {
    get: async () => {
      const r = responses[Math.min(calls, responses.length - 1)]!;
      calls += 1;
      return r;
    },
    sleep: async (ms: number) => {
      sleeps.push(ms);
      t += ms;
    },
    now: () => t,
    intervalMs: 5000,
    timeoutMs: 60_000,
    signal: controller.signal,
  };
  return { options, controller, calls: () => calls, sleeps };
}

describe("pollWeather", () => {
  it("すぐ取れたら 1 回で返す", async () => {
    const s = setup([view(WEATHER)]);
    expect(await pollWeather(s.options)).toEqual(WEATHER);
    expect(s.calls()).toBe(1);
    expect(s.sleeps).toEqual([]);
  });

  it("何回か 404 や通信のエラーのあとに取れる", async () => {
    const s = setup([
      { kind: "not_found" },
      { kind: "error", error: "通信に失敗しました: x" },
      view(null),
      view(WEATHER),
    ]);
    expect(await pollWeather(s.options)).toEqual(WEATHER);
    expect(s.calls()).toBe(4);
    expect(s.sleeps).toEqual([5000, 5000, 5000]);
  });

  it("60 秒たっても取れなければ null（12 回問い合わせる）", async () => {
    const s = setup([{ kind: "not_found" }]);
    expect(await pollWeather(s.options)).toBeNull();
    expect(s.calls()).toBe(12);
  });

  it("401 ですぐやめる", async () => {
    const s = setup([{ kind: "not_found" }, { kind: "auth" }, view(WEATHER)]);
    expect(await pollWeather(s.options)).toBeNull();
    expect(s.calls()).toBe(2);
  });

  it("始める前に止められていれば、問い合わせない", async () => {
    const controller = new AbortController();
    controller.abort();
    const s = setup([view(WEATHER)], controller);
    expect(await pollWeather(s.options)).toBeNull();
    expect(s.calls()).toBe(0);
  });

  it("待っている途中で止められたら null", async () => {
    const controller = new AbortController();
    const s = setup([{ kind: "not_found" }], controller);
    const original = s.options.sleep;
    s.options.sleep = async (ms: number) => {
      await original(ms);
      if (s.calls() === 2) controller.abort();
    };
    expect(await pollWeather(s.options)).toBeNull();
    expect(s.calls()).toBe(2);
  });

  it("問い合わせている間に止められたら、取れた値も返さない", async () => {
    const controller = new AbortController();
    const s = setup([view(WEATHER)], controller);
    const original = s.options.get;
    s.options.get = async () => {
      const r = await original();
      controller.abort();
      return r;
    };
    expect(await pollWeather(s.options)).toBeNull();
  });
});
