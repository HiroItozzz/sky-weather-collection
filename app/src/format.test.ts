import { describe, expect, it } from "@jest/globals";
import {
  answerLabel,
  categoryLabel,
  correctLabel,
  formatDateTime,
  guessLabel,
  hitRate,
  hitRateLabel,
  isCloudyRainScarce,
  weatherSummary,
} from "./format";

describe("ラベル", () => {
  it("分類の名前", () => {
    expect(categoryLabel("clear")).toBe("晴れ");
    expect(categoryLabel("cloudy")).toBe("くもり");
    expect(categoryLabel("rain")).toBe("雨");
    expect(categoryLabel("unknown")).toBe("不明");
  });

  it("予想", () => {
    expect(guessLabel("rain")).toBe("降る");
    expect(guessLabel("no_rain")).toBe("降らない");
    expect(guessLabel(null)).toBe("予想なし");
  });

  it("答え", () => {
    expect(answerLabel("rain", false)).toBe("降った");
    expect(answerLabel("no_rain", false)).toBe("降らなかった");
    expect(answerLabel("unknown", true)).toBe("答え合わせ待ち（撮影の約6〜7時間後）");
    expect(answerLabel("unknown", false)).toBe("答え合わせできませんでした");
  });

  it("当たり外れ", () => {
    expect(correctLabel(true)).toBe("当たり");
    expect(correctLabel(false)).toBe("はずれ");
    expect(correctLabel(null)).toBeNull();
  });
});

describe("weatherSummary", () => {
  it("分類、気温、雲量を並べる", () => {
    expect(
      weatherSummary({
        category: "cloudy",
        weather_code: 3,
        temperature_c: 18.2,
        precipitation_mm: 0,
        cloud_cover_pct: 90,
      }),
    ).toBe("くもり 18.2℃ 雲量 90%");
  });

  it("null の項目は出さない", () => {
    expect(
      weatherSummary({
        category: "rain",
        weather_code: 61,
        temperature_c: null,
        precipitation_mm: null,
        cloud_cover_pct: 100,
      }),
    ).toBe("雨 雲量 100%");
    expect(
      weatherSummary({
        category: "clear",
        weather_code: 0,
        temperature_c: 20,
        precipitation_mm: null,
        cloud_cover_pct: null,
      }),
    ).toBe("晴れ 20.0℃");
  });
});

describe("的中率", () => {
  it("予想が 0 件なら null と「まだありません」", () => {
    expect(hitRate({ guesses_correct: 0, guesses_total: 0 })).toBeNull();
    expect(hitRateLabel({ guesses_correct: 0, guesses_total: 0 })).toBe("まだありません");
  });

  it("割合と件数を出す", () => {
    expect(hitRate({ guesses_correct: 14, guesses_total: 20 })).toBe(0.7);
    expect(hitRateLabel({ guesses_correct: 14, guesses_total: 20 })).toBe("70%（14 / 20）");
    expect(hitRateLabel({ guesses_correct: 1, guesses_total: 3 })).toBe("33%（1 / 3）");
    expect(hitRateLabel({ guesses_correct: 0, guesses_total: 5 })).toBe("0%（0 / 5）");
  });
});

describe("isCloudyRainScarce", () => {
  const by = (clear: number, cloudy: number, rain: number, unknown = 0) => ({
    clear,
    cloudy,
    rain,
    unknown,
  });

  it("30% 未満なら true", () => {
    expect(isCloudyRainScarce(by(71, 20, 8))).toBe(true);
  });

  it("30% ちょうどは false", () => {
    expect(isCloudyRainScarce(by(70, 20, 10))).toBe(false);
  });

  it("30% より多ければ false", () => {
    expect(isCloudyRainScarce(by(20, 15, 5))).toBe(false);
  });

  it("合計が 0 なら true", () => {
    expect(isCloudyRainScarce(by(0, 0, 0))).toBe(true);
    expect(isCloudyRainScarce(by(0, 0, 0, 4))).toBe(true);
  });

  it("unknown は数えない", () => {
    expect(isCloudyRainScarce(by(10, 5, 0, 100))).toBe(false);
  });
});

describe("formatDateTime", () => {
  it("端末のタイムゾーンで YYYY/MM/DD HH:mm にする", () => {
    // 端末の時刻で組み立てるので、どのタイムゾーンでも同じ結果になる
    const iso = new Date(2026, 9, 9, 14, 5, 30).toISOString();
    expect(formatDateTime(iso)).toBe("2026/10/09 14:05");
  });

  it("月日と時刻を 0 埋めする", () => {
    expect(formatDateTime(new Date(2026, 0, 2, 3, 4).toISOString())).toBe("2026/01/02 03:04");
  });

  it("読めない日時はそのまま返す", () => {
    expect(formatDateTime("きのう")).toBe("きのう");
  });
});
