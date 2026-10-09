// 「記録」ボタンを押した瞬間の値を JSON の形にまとめる。仕様は docs/m2-app-sensors.md の 5 節。
import { magneticModel } from "./declination";
import {
  angularSpreadDeg,
  cameraAngles,
  cameraDirection,
  normalizeDeg,
  toTrueAzimuth,
  type Mat3,
} from "./orientation";

export type Sample = { t: number; R: Mat3 };

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
  /** 押した時刻の前後 0.5 秒のサンプル */
  samples: Sample[];
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

function round(value: number, digits: number): number {
  const k = 10 ** digits;
  // -0 を 0 にする
  return Math.round(value * k) / k + 0;
}

function roundOrNull(value: number | null, digits: number): number | null {
  return value === null ? null : round(value, digits);
}

function nearestSample(samples: Sample[], t: number): Sample | null {
  let best: Sample | null = null;
  for (const s of samples) {
    if (best === null || Math.abs(s.t - t) < Math.abs(best.t - t)) best = s;
  }
  return best;
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
  const { pressedAtMs, samples, location, headingAccuracy } = input;

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
  const nearest = nearestSample(samples, pressedAtMs);
  if (nearest !== null) {
    const angles = cameraAngles(nearest.R);
    const azimuth =
      declinationDeg === null
        ? null
        : normalizeDeg(round(toTrueAzimuth(angles.azimuthDeg, declinationDeg), 2));
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
