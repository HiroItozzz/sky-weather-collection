// DeviceMotion のサンプルの時刻の扱い（重複の除去、時計のずれの更新、撮影の窓の取り出し）。
// 仕様は docs/m3-app-capture.md の 9.3 節と 9.4 節。React には依存しない。
import type { Sample } from "./record";

export type SampleBuffer = {
  /** 受け取った順（tSensorMs の古い順）のサンプル */
  samples: Sample[];
  /**
   * センサーの時計と `Date.now()` のずれ（ミリ秒）。受け取りの遅れが最も小さかったときの値。
   * まだ何も受け取っていなければ null。
   */
  offsetMs: number | null;
};

export const EMPTY_BUFFER: SampleBuffer = { samples: [], offsetMs: null };

/** サンプルの壁時計の時刻（UNIX ミリ秒）。offsetMs は使う時点の値で計算し直す。 */
export function sampleTimeMs(sample: Sample, offsetMs: number): number {
  return sample.tSensorMs + offsetMs;
}

/**
 * サンプルを1つ受け取ったときの新しい状態を返す（元の状態は変えない）。
 * - ずれ offsetMs は、受け取るたびに最小値で更新する
 * - 直前のサンプルと同じ tSensorMs なら、サンプルは捨てる
 * - 最新の tSensorMs より keepMs 以上前のサンプルは捨てる
 */
export function addSample(
  buffer: SampleBuffer,
  sample: Sample,
  receivedAtMs: number,
  keepMs: number,
): SampleBuffer {
  const delay = receivedAtMs - sample.tSensorMs;
  const offsetMs = buffer.offsetMs === null ? delay : Math.min(buffer.offsetMs, delay);

  const last = buffer.samples[buffer.samples.length - 1];
  if (last !== undefined && last.tSensorMs === sample.tSensorMs) {
    return { samples: buffer.samples, offsetMs };
  }

  const kept = buffer.samples.filter((s) => sample.tSensorMs - s.tSensorMs <= keepMs);
  kept.push(sample);
  return { samples: kept, offsetMs };
}

/**
 * 壁時計の時刻が [startMs, endMs] に入るサンプル（撮影の窓）と、そのときの offsetMs を返す。
 * offsetMs はまだサンプルが届いていなければ null で、サンプルは空になる。
 */
export function extractWindow(
  buffer: SampleBuffer,
  startMs: number,
  endMs: number,
): { samples: Sample[]; offsetMs: number | null } {
  const { offsetMs } = buffer;
  if (offsetMs === null) return { samples: [], offsetMs };
  const samples = buffer.samples.filter((s) => {
    const t = sampleTimeMs(s, offsetMs);
    return t >= startMs && t <= endMs;
  });
  return { samples, offsetMs };
}

/**
 * items が max 件を超えるとき、最初と最後を含めて等間隔に max 件へ間引く。超えなければそのまま返す。
 */
export function thinEvenly<T>(items: T[], max: number): T[] {
  if (items.length <= max) return items;
  if (max <= 1) return items.slice(0, max);
  const last = items.length - 1;
  return Array.from({ length: max }, (_, i) => items[Math.round((i * last) / (max - 1))]);
}
