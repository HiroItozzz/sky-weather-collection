// 撮影の窓のサンプルから、向きと位置を JSON の形にまとめる。仕様は docs/m2-app-sensors.md の 5 節と docs/m3-app-capture.md の 9.4 節。
import { magneticModel } from "./declination";
import { sampleTimeMs } from "./sampleClock";
import {
  angularSpreadDeg,
  cameraAngles,
  cameraDirection,
  toTrueAzimuth,
  type Mat3,
} from "./orientation";

/** DeviceMotion の 1 サンプル。壁時計の時刻は `sampleTimeMs` で求める。 */
export type Sample = {
  /** rotation.timestamp × 1000（センサーの時計。端末の起動からの経過ミリ秒） */
  tSensorMs: number;
  /** DeviceMotion の rotation（ラジアン） */
  alpha: number;
  beta: number;
  gamma: number;
  R: Mat3;
};

/** expo-location の `LocationObject` と同じ形の最小の型。 */
export type LocationInput = {
  coords: {
    latitude: number;
    longitude: number;
    accuracy: number | null;
    altitude: number | null;
    altitudeAccuracy: number | null;
  };
  timestamp: number;
};

export type RecordInput = {
  /** 記録ボタンを押した時刻（UNIX ミリ秒） */
  pressedAtMs: number;
  /** 撮影が終わった時刻（UNIX ミリ秒） */
  completedAtMs: number;
  /** 撮影の窓（押した 0.5 秒前から撮影が終わるまで）のサンプル */
  samples: Sample[];
  /** センサーの時計と `Date.now()` のずれ。サンプルがあるときは null にならない */
  offsetMs: number | null;
  location: LocationInput | null;
  /** watchHeadingAsync の accuracy */
  headingAccuracy: number | null;
};

export type OrientationAccuracy = "high" | "medium" | "low" | "unreliable";

export type SensorRecord = {
  captured_at: string;
  orientation: {
    azimuth_deg: number | null;
    pitch_deg: number;
    roll_deg: number;
    declination_deg: number | null;
    accuracy: OrientationAccuracy | null;
    stddev_deg: number | null;
  } | null;
  location: {
    lat: number;
    lon: number;
    accuracy_m: number | null;
    altitude_m: number | null;
    fix_time: string;
  } | null;
};

const ACCURACY_LABELS: Record<number, OrientationAccuracy> = {
  3: "high",
  2: "medium",
  1: "low",
  0: "unreliable",
};

export function round(value: number, digits: number): number {
  const k = 10 ** digits;
  // -0 を 0 にする
  return Math.round(value * k) / k + 0;
}

/** 方位角を小数第2位に丸める。丸めて 360 になったら 0 にする（% で丸め誤差が戻らないよう、剰余は取らない）。 */
function roundAzimuth(deg: number): number {
  const r = round(deg, 2);
  return r >= 360 ? 0 : r;
}

export function roundOrNull(value: number | null, digits: number): number | null {
  return value === null ? null : round(value, digits);
}

/** 壁時計の時刻が t にいちばん近いサンプル。同じ近さなら先のもの。 */
function nearestSample(samples: Sample[], offsetMs: number, t: number): Sample | null {
  let best: Sample | null = null;
  let bestGap = Infinity;
  for (const s of samples) {
    const gap = Math.abs(sampleTimeMs(s, offsetMs) - t);
    if (gap < bestGap) {
      best = s;
      bestGap = gap;
    }
  }
  return best;
}

/** サンプルのうち、カメラの向きが最も離れた2つのなす角（度）。2つ未満なら null。 */
export function motionDeg(samples: Sample[]): number | null {
  if (samples.length < 2) return null;
  const directions = samples.map((s) => cameraDirection(s.R));
  let max = 0;
  for (let i = 0; i < directions.length; i++) {
    for (let j = i + 1; j < directions.length; j++) {
      const [a, b] = [directions[i], directions[j]];
      const cross = Math.hypot(
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
      );
      const dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
      max = Math.max(max, Math.atan2(cross, dot) * (180 / Math.PI));
    }
  }
  return max;
}

function toAccuracy(value: number | null): OrientationAccuracy | null {
  if (value === null) return null;
  return ACCURACY_LABELS[value] ?? null;
}

/** 楕円体高のまま返す。高度の精度が取れていない（null か 0 以下）ときは null。 */
function toAltitude(coords: LocationInput["coords"]): number | null {
  const { altitude, altitudeAccuracy } = coords;
  if (altitude === null || altitudeAccuracy === null || altitudeAccuracy <= 0) return null;
  return altitude;
}

export function buildRecord(input: RecordInput): SensorRecord {
  const { pressedAtMs, completedAtMs, samples, offsetMs, location, headingAccuracy } = input;

  let declinationDeg: number | null = null;
  if (location !== null) {
    declinationDeg = magneticModel(
      location.coords.latitude,
      location.coords.longitude,
      toAltitude(location.coords),
      new Date(pressedAtMs),
    ).declinationDeg;
  }

  let orientation: SensorRecord["orientation"] = null;
  // 代表の向きは、露光が窓のどこで起きたかわからないので、窓の中ほどに近いサンプルにする
  const nearest = nearestSample(samples, offsetMs ?? 0, (pressedAtMs + completedAtMs) / 2);
  if (nearest !== null) {
    const angles = cameraAngles(nearest.R);
    const azimuth =
      declinationDeg === null
        ? null
        : roundAzimuth(toTrueAzimuth(angles.azimuthDeg, declinationDeg));
    orientation = {
      azimuth_deg: azimuth,
      pitch_deg: round(angles.pitchDeg, 2),
      roll_deg: round(angles.rollDeg, 2),
      declination_deg: roundOrNull(declinationDeg, 2),
      accuracy: toAccuracy(headingAccuracy),
      stddev_deg: roundOrNull(angularSpreadDeg(samples.map((s) => cameraDirection(s.R))), 2),
    };
  }

  return {
    captured_at: new Date(pressedAtMs).toISOString(),
    orientation,
    location:
      location === null
        ? null
        : {
            lat: round(location.coords.latitude, 7),
            lon: round(location.coords.longitude, 7),
            accuracy_m: roundOrNull(location.coords.accuracy, 1),
            altitude_m: roundOrNull(toAltitude(location.coords), 1),
            fix_time: new Date(location.timestamp).toISOString(),
          },
  };
}
