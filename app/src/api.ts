// サーバーの閲覧用 API（設計メモ 13.5）の呼び出し。仕様は docs/m5-app.md の 4 節。
import type { Settings } from "./settings";

export type WeatherCategory = "clear" | "cloudy" | "rain";

export type WeatherAtCapture = {
  category: WeatherCategory;
  weather_code: number;
  temperature_c: number | null;
  precipitation_mm: number | null;
  cloud_cover_pct: number | null;
};

export type AnswerResult = "rain" | "no_rain" | "unknown";

export type ObservationView = {
  observation_id: string;
  received_at: string;
  captured_at: string;
  user_guess: "rain" | "no_rain" | null;
  weather_at_capture: WeatherAtCapture | null;
  /** pending：答え合わせのジョブがまだ終わっていなければ true（13.5） */
  answer: { result: AnswerResult; source: "amedas" | "open_meteo" | null; pending: boolean };
  correct: boolean | null;
};

export type ObservationPage = { observations: ObservationView[]; next_before: string | null };

export type Stats = {
  observations_total: number;
  answered_total: number;
  guesses_total: number;
  guesses_correct: number;
  by_category: { clear: number; cloudy: number; rain: number; unknown: number };
};

export type ApiResult<T> =
  | { kind: "ok"; data: T }
  | { kind: "not_found" }
  | { kind: "auth" }
  | { kind: "error"; error: string };

export interface ApiClient {
  getObservation(settings: Settings, id: string): Promise<ApiResult<ObservationView>>;
  listObservations(
    settings: Settings,
    options: { limit: number; before?: string | null },
  ): Promise<ApiResult<ObservationPage>>;
  getStats(settings: Settings): Promise<ApiResult<Stats>>;
}

const TIMEOUT_MS = 15_000;
const SHAPE_ERROR = "サーバーの応答の形が想定と違います";

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNumberOrNull(value: unknown): value is number | null {
  return value === null || isNumber(value);
}

function parseWeather(value: unknown): WeatherAtCapture | null | undefined {
  if (value === null) return null;
  if (!isObject(value)) return undefined;
  const { category, weather_code, temperature_c, precipitation_mm, cloud_cover_pct } = value;
  if (category !== "clear" && category !== "cloudy" && category !== "rain") return undefined;
  if (!isNumber(weather_code)) return undefined;
  if (!isNumberOrNull(temperature_c) || !isNumberOrNull(precipitation_mm)) return undefined;
  if (!isNumberOrNull(cloud_cover_pct)) return undefined;
  return { category, weather_code, temperature_c, precipitation_mm, cloud_cover_pct };
}

/** 形が合わなければ null。知らない項目は捨てる。 */
export function parseObservationView(value: unknown): ObservationView | null {
  if (!isObject(value)) return null;
  const { observation_id, received_at, captured_at, user_guess, answer, correct } = value;
  if (!isString(observation_id) || !isString(received_at) || !isString(captured_at)) return null;
  if (user_guess !== null && user_guess !== "rain" && user_guess !== "no_rain") return null;
  if (correct !== null && typeof correct !== "boolean") return null;
  const weather = parseWeather(value.weather_at_capture);
  if (weather === undefined) return null;
  if (!isObject(answer)) return null;
  const { result, source } = answer;
  // pending がない・bool でない応答は、項目を足す前のサーバーとみなし、まだ終わっていない（true）扱いにする
  const pending = typeof answer.pending === "boolean" ? answer.pending : true;
  if (result !== "rain" && result !== "no_rain" && result !== "unknown") return null;
  if (source !== null && source !== "amedas" && source !== "open_meteo") return null;
  return {
    observation_id,
    received_at,
    captured_at,
    user_guess,
    weather_at_capture: weather,
    answer: { result, source, pending },
    correct,
  };
}

export function parseObservationPage(value: unknown): ObservationPage | null {
  if (!isObject(value) || !Array.isArray(value.observations)) return null;
  const next = value.next_before;
  if (next !== null && !isString(next)) return null;
  const observations: ObservationView[] = [];
  for (const item of value.observations) {
    const view = parseObservationView(item);
    if (view === null) return null;
    observations.push(view);
  }
  return { observations, next_before: next };
}

export function parseStats(value: unknown): Stats | null {
  if (!isObject(value) || !isObject(value.by_category)) return null;
  const { observations_total, answered_total, guesses_total, guesses_correct } = value;
  const { clear, cloudy, rain, unknown } = value.by_category;
  if (!isNumber(observations_total) || !isNumber(answered_total)) return null;
  if (!isNumber(guesses_total) || !isNumber(guesses_correct)) return null;
  if (!isNumber(clear) || !isNumber(cloudy) || !isNumber(rain) || !isNumber(unknown)) return null;
  return {
    observations_total,
    answered_total,
    guesses_total,
    guesses_correct,
    by_category: { clear, cloudy, rain, unknown },
  };
}

export function createApiClient(fetchImpl: typeof fetch = fetch): ApiClient {
  async function get<T>(
    settings: Settings,
    path: string,
    parse: (value: unknown) => T | null,
  ): Promise<ApiResult<T>> {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT_MS);
    try {
      const response = await fetchImpl(`${settings.serverUrl}${path}`, {
        method: "GET",
        headers: { Authorization: `Bearer ${settings.inviteCode}` },
        signal: controller.signal,
      });
      if (response.status === 401) return { kind: "auth" };
      if (response.status === 404) return { kind: "not_found" };
      if (response.status !== 200) return { kind: "error", error: `HTTP ${response.status}` };
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        return { kind: "error", error: SHAPE_ERROR };
      }
      const data = parse(body);
      return data === null ? { kind: "error", error: SHAPE_ERROR } : { kind: "ok", data };
    } catch (e) {
      if (timedOut) {
        return { kind: "error", error: `${TIMEOUT_MS / 1000}秒たっても応答がありませんでした` };
      }
      const message = e instanceof Error ? e.message : String(e);
      return { kind: "error", error: `通信に失敗しました: ${message}` };
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    getObservation(settings, id) {
      return get(settings, `/v1/observations/${encodeURIComponent(id)}`, parseObservationView);
    },
    listObservations(settings, { limit, before }) {
      let query = `limit=${encodeURIComponent(String(limit))}`;
      if (before) query += `&before=${encodeURIComponent(before)}`;
      return get(settings, `/v1/me/observations?${query}`, parseObservationPage);
    },
    getStats(settings) {
      return get(settings, "/v1/me/stats", parseStats);
    },
  };
}
