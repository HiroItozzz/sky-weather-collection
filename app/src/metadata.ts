// サーバーに送るメタデータ（schema_version 1）を組み立てる。仕様は docs/m3-app-capture.md の 5 節。
// 項目の名前と並びは server/src/sky_server/models.py の ObservationMetadata にそろえる。
import { parseCameraExif, type CameraMetadata, type ExifInput } from "./exif";
import {
  buildRecord,
  motionDeg,
  roundOrNull,
  type LocationInput,
  type Sample,
  type SensorRecord,
} from "./record";
import { thinEvenly } from "./sampleClock";

export type UserGuess = "rain" | "no_rain";

export type DeviceInfo = {
  platform: "android" | "ios";
  os_version: string | null;
  model: string | null;
  app_version: string | null;
};

export type MetadataInput = {
  observationId: string;
  /** シャッターのボタンを押した時刻（UNIX ミリ秒） */
  pressedAtMs: number;
  /** 撮影が終わった（takePictureAsync が返った）時刻（UNIX ミリ秒） */
  completedAtMs: number;
  /** 撮影の窓（押した 0.5 秒前から撮影が終わるまで）のサンプル */
  samples: Sample[];
  /** センサーの時計と `Date.now()` のずれ。サンプルがまだ届いていなければ null */
  offsetMs: number | null;
  location: LocationInput | null;
  /** watchHeadingAsync の accuracy */
  headingAccuracy: number | null;
  exif: ExifInput | null | undefined;
  /** takePictureAsync の戻り値の幅と高さ */
  width: number | null | undefined;
  height: number | null | undefined;
  device: DeviceInfo;
  imageSha256: string;
  /** シャッターを押した時点の予想。答えなかったときは null */
  userGuess: UserGuess | null;
};

/** 9.5 節の capture。サーバーの Capture にそろえる。 */
export type CaptureMetadata = {
  pressed_at: string;
  completed_at: string;
  duration_ms: number;
  motion_deg: number | null;
  sensor_clock_offset_ms: number | null;
  exif_datetime_original: string | null;
  exif_subsec_time_original: string | null;
  orientation_trace: {
    source: string;
    /** 同じ添字が 1 件のサンプル（ミリ秒・ラジアン）。時刻の古い順 */
    t_sensor_ms: number[];
    alpha: number[];
    beta: number[];
    gamma: number[];
  } | null;
};

export type ObservationMetadata = {
  schema_version: 1;
  observation_id: string;
  captured_at: string;
  tz_offset_min: number;
  location: {
    lat: number;
    lon: number;
    accuracy_m: number;
    altitude_m: number | null;
    fix_time: string;
  } | null;
  orientation: SensorRecord["orientation"];
  camera: CameraMetadata;
  device: {
    platform: "android" | "ios";
    os_version: string;
    model: string;
    app_version: string;
  };
  capture_path: "native";
  user_guess: UserGuess | null;
  image_sha256: string;
  capture: CaptureMetadata;
};

/** サーバーが受け付ける文字列の長さの上限（exif の文字列） */
const EXIF_TEXT_MAX_LENGTH = 64;

/** サーバーが受け付ける trace の件数の上限 */
const TRACE_MAX_SAMPLES = 1000;

const TRACE_SOURCE = "expo-sensors DeviceMotion rotation (alpha, beta, gamma)";

function orUnknown(value: string | null | undefined): string {
  return value ? value : "unknown";
}

/** EXIF の値を文字列のまま返す。文字列でないか、長すぎる値は null。 */
function exifText(exif: ExifInput | null | undefined, key: string): string | null {
  const value = exif?.[key];
  return typeof value === "string" && value.length <= EXIF_TEXT_MAX_LENGTH ? value : null;
}

function buildCapture(input: MetadataInput, completedAtMs: number): CaptureMetadata {
  const samples = [...input.samples].sort((a, b) => a.tSensorMs - b.tSensorMs);
  // 件数が多すぎるときは、trace だけを間引く（motion_deg は全部のサンプルで求める）
  const traced = thinEvenly(samples, TRACE_MAX_SAMPLES);
  return {
    pressed_at: new Date(input.pressedAtMs).toISOString(),
    completed_at: new Date(completedAtMs).toISOString(),
    duration_ms: Math.round(completedAtMs - input.pressedAtMs),
    motion_deg: roundOrNull(motionDeg(samples), 2),
    sensor_clock_offset_ms: roundOrNull(input.offsetMs, 1),
    exif_datetime_original: exifText(input.exif, "DateTimeOriginal"),
    exif_subsec_time_original: exifText(input.exif, "SubSecTimeOriginal"),
    orientation_trace:
      traced.length === 0
        ? null
        : {
            source: TRACE_SOURCE,
            t_sensor_ms: traced.map((s) => s.tSensorMs),
            alpha: traced.map((s) => s.alpha),
            beta: traced.map((s) => s.beta),
            gamma: traced.map((s) => s.gamma),
          },
  };
}

export function buildMetadata(input: MetadataInput): ObservationMetadata {
  // 撮影中に Date.now() が巻き戻ったときは、押した時刻に終わったものとみなす
  const completedAtMs = Math.max(input.completedAtMs, input.pressedAtMs);
  const record = buildRecord({
    pressedAtMs: input.pressedAtMs,
    completedAtMs,
    samples: input.samples,
    offsetMs: input.offsetMs,
    location: input.location,
    headingAccuracy: input.headingAccuracy,
  });

  // サーバーは accuracy_m を null 不可にしているので、精度のわからない位置は送らない
  const accuracy = input.location?.coords.accuracy;
  const location =
    record.location !== null && record.location.accuracy_m !== null && accuracy != null && accuracy > 0
      ? { ...record.location, accuracy_m: record.location.accuracy_m }
      : null;

  return {
    schema_version: 1,
    observation_id: input.observationId,
    captured_at: record.captured_at,
    // -0 を 0 にする
    tz_offset_min: -new Date(input.pressedAtMs).getTimezoneOffset() + 0,
    location,
    orientation: record.orientation,
    camera: parseCameraExif(input.exif, input.width, input.height),
    device: {
      platform: input.device.platform,
      os_version: orUnknown(input.device.os_version),
      model: orUnknown(input.device.model),
      app_version: orUnknown(input.device.app_version),
    },
    capture_path: "native",
    user_guess: input.userGuess,
    image_sha256: input.imageSha256,
    capture: buildCapture(input, completedAtMs),
  };
}
