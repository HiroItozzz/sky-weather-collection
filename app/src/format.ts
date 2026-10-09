// 画面に出す文言と、表示用の計算。仕様は docs/m5-app.md の 5 節と 6 節。
import type { AnswerResult, Stats, WeatherAtCapture } from "./api";

export type CategoryKey = "clear" | "cloudy" | "rain" | "unknown";

const CATEGORY_LABELS: Record<CategoryKey, string> = {
  clear: "晴れ",
  cloudy: "くもり",
  rain: "雨",
  unknown: "不明",
};

/** 「特に貴重です」を添える、曇りと雨の割合の下限 */
const RARE_RATIO = 0.3;

export function categoryLabel(category: CategoryKey): string {
  return CATEGORY_LABELS[category];
}

export function guessLabel(guess: "rain" | "no_rain" | null): string {
  if (guess === "rain") return "降る";
  if (guess === "no_rain") return "降らない";
  return "予想なし";
}

export function answerLabel(result: AnswerResult): string {
  if (result === "rain") return "降った";
  if (result === "no_rain") return "降らなかった";
  return "答え合わせ待ち（撮影の約6〜7時間後）";
}

/** 当たりは「当たり」、はずれは「はずれ」。まだわからないときは null（表示しない）。 */
export function correctLabel(correct: boolean | null): string | null {
  if (correct === null) return null;
  return correct ? "当たり" : "はずれ";
}

/** 「くもり 18.2℃ 雲量 90%」のような 1 行。値が null の項目は出さない。 */
export function weatherSummary(weather: WeatherAtCapture): string {
  const parts = [categoryLabel(weather.category)];
  if (weather.temperature_c !== null) parts.push(`${weather.temperature_c.toFixed(1)}℃`);
  if (weather.cloud_cover_pct !== null) parts.push(`雲量 ${Math.round(weather.cloud_cover_pct)}%`);
  return parts.join(" ");
}

/** 的中率（0〜1）。予想が 1 件もなければ null。 */
export function hitRate(stats: Pick<Stats, "guesses_correct" | "guesses_total">): number | null {
  if (stats.guesses_total <= 0) return null;
  return stats.guesses_correct / stats.guesses_total;
}

/** 「70%（14 / 20）」。予想がなければ「まだありません」。 */
export function hitRateLabel(stats: Pick<Stats, "guesses_correct" | "guesses_total">): string {
  const rate = hitRate(stats);
  if (rate === null) return "まだありません";
  return `${Math.round(rate * 100)}%（${stats.guesses_correct} / ${stats.guesses_total}）`;
}

/**
 * 曇りと雨の写真が少ないか。cloudy + rain が clear + cloudy + rain の 30% 未満、
 * または 3 つの合計が 0 のとき true。unknown は数えない。
 */
export function isCloudyRainScarce(byCategory: Stats["by_category"]): boolean {
  const total = byCategory.clear + byCategory.cloudy + byCategory.rain;
  if (total === 0) return true;
  return (byCategory.cloudy + byCategory.rain) / total < RARE_RATIO;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** 端末のタイムゾーンで `2026/10/09 14:05` の形にする。読めない日時はそのまま返す。 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return (
    `${date.getFullYear()}/${pad2(date.getMonth() + 1)}/${pad2(date.getDate())} ` +
    `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  );
}
