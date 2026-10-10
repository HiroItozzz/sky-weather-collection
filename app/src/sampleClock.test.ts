import { describe, expect, it } from "@jest/globals";
import type { Mat3 } from "./orientation";
import type { Sample } from "./record";
import { addSample, EMPTY_BUFFER, extractWindow, sampleTimeMs, thinEvenly, type SampleBuffer } from "./sampleClock";

const R: Mat3 = [1, 0, 0, 0, 0, -1, 0, 1, 0];

function sample(tSensorMs: number): Sample {
  return { tSensorMs, alpha: 0, beta: 0, gamma: 0, R };
}

/** [tSensorMs, 受け取った時刻] の並びを順に渡す。 */
function feed(events: [number, number][], keepMs = 10_000): SampleBuffer {
  return events.reduce((b, [t, received]) => addSample(b, sample(t), received, keepMs), EMPTY_BUFFER);
}

describe("addSample", () => {
  it("最初のサンプルでずれが決まる", () => {
    const b = feed([[1000, 1_700_000_001_050]]);
    expect(b.samples.map((s) => s.tSensorMs)).toEqual([1000]);
    expect(b.offsetMs).toBe(1_700_000_001_050 - 1000);
  });

  it("ずれは受け取りの遅れが最も小さかった値に下がっていき、上がらない", () => {
    // 遅れが 50 → 30 → 200 ミリ秒
    const base = 1_700_000_000_000;
    const b = feed([
      [1000, base + 1000 + 50],
      [1020, base + 1020 + 30],
      [1040, base + 1040 + 200],
    ]);
    expect(b.offsetMs).toBe(base + 30);
  });

  it("同じ tSensorMs が続けて届いたら2つめ以降は捨てる", () => {
    const b = feed([
      [1000, 2000],
      [1000, 2005],
      [1000, 2010],
      [1020, 2020],
    ]);
    expect(b.samples.map((s) => s.tSensorMs)).toEqual([1000, 1020]);
  });

  it("捨てたサンプルでも、受け取りの遅れが小さければずれは更新する", () => {
    const b = feed([
      [1000, 2100],
      [1000, 2050],
    ]);
    expect(b.samples).toHaveLength(1);
    expect(b.offsetMs).toBe(1050);
  });

  it("最新より keepMs を超えて古いサンプルは捨てる", () => {
    const b = feed(
      [
        [1000, 1000],
        [1500, 1500],
        [2000, 2000],
        [2600, 2600],
      ],
      1000,
    );
    // 2600 − 1500 = 1100 > 1000 で 1500 も落ちる。2600 − 2000 = 600 は残る
    expect(b.samples.map((s) => s.tSensorMs)).toEqual([2000, 2600]);
  });

  it("ちょうど keepMs 前のサンプルは残る", () => {
    const b = feed(
      [
        [1000, 1000],
        [2000, 2000],
      ],
      1000,
    );
    expect(b.samples.map((s) => s.tSensorMs)).toEqual([1000, 2000]);
  });

  it("元の状態は変えない", () => {
    const before = feed([[1000, 2000]]);
    addSample(before, sample(1020), 2020, 10_000);
    expect(before.samples).toHaveLength(1);
    expect(before.offsetMs).toBe(1000);
  });
});

describe("sampleTimeMs", () => {
  it("tSensorMs に offsetMs を足す", () => {
    expect(sampleTimeMs(sample(1000), 500)).toBe(1500);
  });
});

describe("extractWindow", () => {
  it("サンプルが届いていなければ、空とずれ null", () => {
    expect(extractWindow(EMPTY_BUFFER, 0, 1_000_000)).toEqual({ samples: [], offsetMs: null });
  });

  it("壁時計の時刻が [start, end] に入るものだけを、両端を含めて古い順に返す", () => {
    // ずれは 1000（遅れ 0 のサンプルが最初に届く）。壁時計の時刻は 2000, 2100, ..., 2500
    const b = feed([
      [1000, 2000],
      [1100, 2100],
      [1200, 2200],
      [1300, 2300],
      [1400, 2400],
      [1500, 2500],
    ]);
    const w = extractWindow(b, 2100, 2400);
    expect(w.offsetMs).toBe(1000);
    expect(w.samples.map((s) => s.tSensorMs)).toEqual([1100, 1200, 1300, 1400]);
  });

  it("窓の中にサンプルがなければ空（ずれは返す）", () => {
    const b = feed([[1000, 2000]]);
    expect(extractWindow(b, 5000, 6000)).toEqual({ samples: [], offsetMs: 1000 });
  });

  it("ずれは取り出す時点の値で計算し直す", () => {
    // 1つめは遅れ 100 で届き、あとからずれが 50 に下がる
    const b = feed([
      [1000, 1100],
      [1020, 1070],
    ]);
    expect(b.offsetMs).toBe(50);
    // 1000 の壁時計の時刻は 1050 になる（届いたときの 1100 ではない）
    expect(extractWindow(b, 1050, 1050).samples.map((s) => s.tSensorMs)).toEqual([1000]);
    expect(extractWindow(b, 1100, 1100).samples.map((s) => s.tSensorMs)).toEqual([]);
  });
});

describe("thinEvenly", () => {
  const range = (n: number) => Array.from({ length: n }, (_, i) => i);

  it("max 件以下ならそのまま返す", () => {
    expect(thinEvenly([], 5)).toEqual([]);
    expect(thinEvenly(range(5), 5)).toEqual(range(5));
    expect(thinEvenly(range(3), 5)).toEqual(range(3));
  });

  it("超えたら最初と最後を含めて等間隔に max 件にする", () => {
    expect(thinEvenly(range(10), 4)).toEqual([0, 3, 6, 9]);
    expect(thinEvenly(range(11), 3)).toEqual([0, 5, 10]);
  });

  it("1 件超えただけでも max 件になり、重複せず順番は変わらない", () => {
    const out = thinEvenly(range(1001), 1000);
    expect(out).toHaveLength(1000);
    expect(out[0]).toBe(0);
    expect(out[999]).toBe(1000);
    expect(new Set(out).size).toBe(1000);
    expect([...out].sort((a, b) => a - b)).toEqual(out);
  });

  it("大きな数でも max 件で、最初と最後が残る", () => {
    const out = thinEvenly(range(5000), 1000);
    expect(out).toHaveLength(1000);
    expect([out[0], out[999]]).toEqual([0, 4999]);
    expect(new Set(out).size).toBe(1000);
  });

  it("max が 1 なら最初の 1 件", () => {
    expect(thinEvenly(range(5), 1)).toEqual([0]);
  });
});
