// サーバーに送るメタデータ（schema_version 1）を組み立てる。仕様は docs/m3-app-capture.md の 5 節。
// 項目の名前と並びは server/src/sky_server/models.py の ObservationMetadata にそろえる。
import { parseCameraExif, type CameraMetadata, type ExifInput } from "./exif";
import { buildRecord, type LocationInput, type Sample, type SensorRecord } from "./record";

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
  /** 押した時刻の前後 0.5 秒のサンプル */
  samples: Sample[];
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
};

function orUnknown(value: string | null | undefined): string {
  return value ? value : "unknown";
}

export function buildMetadata(input: MetadataInput): ObservationMetadata {
  const record = buildRecord({
    pressedAtMs: input.pressedAtMs,
    samples: input.samples,
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
  };
}
