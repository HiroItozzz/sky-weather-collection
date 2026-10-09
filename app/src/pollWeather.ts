// 撮影時の天気を、取れるまで一定の間隔で問い合わせる段取り。仕様は docs/m5-app.md の 5 節。
import type { ApiResult, ObservationView, WeatherAtCapture } from "./api";

export type PollWeatherOptions = {
  get: () => Promise<ApiResult<ObservationView>>;
  /** 待つ。signal が止まったら早く戻ってよい */
  sleep: (ms: number, signal: AbortSignal) => Promise<void>;
  now: () => number;
  intervalMs: number;
  timeoutMs: number;
  signal: AbortSignal;
};

/**
 * 最初の問い合わせから timeoutMs たつまで、intervalMs ごとに get を呼ぶ。
 * weather_at_capture が取れたらそれを返す。取れないまま時間切れ、401、signal が止められたときは null。
 * 404 や通信のエラーは、時間切れまで続ける。
 */
export async function pollWeather(options: PollWeatherOptions): Promise<WeatherAtCapture | null> {
  const { get, sleep, now, intervalMs, timeoutMs, signal } = options;
  const startedAt = now();
  while (!signal.aborted) {
    const result = await get();
    if (signal.aborted) return null;
    if (result.kind === "auth") return null;
    if (result.kind === "ok" && result.data.weather_at_capture !== null) {
      return result.data.weather_at_capture;
    }
    // 次の問い合わせが時間切れのあとになるなら、待たずにやめる
    if (now() - startedAt + intervalMs >= timeoutMs) return null;
    await sleep(intervalMs, signal);
  }
  return null;
}
