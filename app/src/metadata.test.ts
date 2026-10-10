import { afterEach, describe, expect, it, jest } from "@jest/globals";
import type { Mat3 } from "./orientation";
import { buildMetadata, type MetadataInput } from "./metadata";
import type { Sample } from "./record";

const PRESSED = Date.parse("2026-10-09T03:12:45.123Z");
const R_NORTH: Mat3 = [1, 0, 0, 0, 0, -1, 0, 1, 0];
const R_EAST: Mat3 = [0, 0, -1, -1, 0, 0, 0, 1, 0];

/** センサーの時計では tSensorMs の時刻のサンプル（offsetMs が 0 なら壁時計と同じ）。 */
function sample(tSensorMs: number, R: Mat3 = R_NORTH, [alpha, beta, gamma] = [0, 0, 0]): Sample {
  return { tSensorMs, alpha, beta, gamma, R };
}

function input(overrides: Partial<MetadataInput> = {}): MetadataInput {
  return {
    observationId: "123e4567-e89b-42d3-a456-426614174000",
    pressedAtMs: PRESSED,
    completedAtMs: PRESSED + 985,
    samples: [sample(PRESSED - 100), sample(PRESSED + 100)],
    offsetMs: 0,
    location: {
      coords: {
        latitude: 35.6812362,
        longitude: 139.7671248,
        accuracy: 12.34,
        altitude: 45.65,
        altitudeAccuracy: 3,
      },
      timestamp: Date.parse("2026-10-09T03:12:44.000Z"),
    },
    headingAccuracy: 3,
    exif: {
      FocalLength: 4.38,
      ISOSpeedRatings: 100,
      ImageWidth: 4000,
      ImageLength: 3000,
      DateTimeOriginal: "2026:10:09 12:12:46",
      SubSecTimeOriginal: "512",
    },
    width: 1,
    height: 1,
    device: { platform: "android", os_version: "15", model: "Pixel 9", app_version: "1.0.0" },
    imageSha256: "a".repeat(64),
    userGuess: null,
    ...overrides,
  };
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe("buildMetadata", () => {
  // server/src/sky_server/models.py の ObservationMetadata と、その中のモデルの項目
  it("キーの一覧がサーバーのモデルと一致する", () => {
    const m = buildMetadata(input());
    expect(Object.keys(m)).toEqual([
      "schema_version",
      "observation_id",
      "captured_at",
      "tz_offset_min",
      "location",
      "orientation",
      "camera",
      "device",
      "capture_path",
      "user_guess",
      "image_sha256",
      "capture",
    ]);
    expect(Object.keys(m.location ?? {})).toEqual([
      "lat",
      "lon",
      "accuracy_m",
      "altitude_m",
      "fix_time",
    ]);
    expect(Object.keys(m.orientation ?? {})).toEqual([
      "azimuth_deg",
      "pitch_deg",
      "roll_deg",
      "declination_deg",
      "accuracy",
      "stddev_deg",
    ]);
    expect(Object.keys(m.camera)).toEqual([
      "focal_length_mm",
      "focal_length_35mm",
      "exposure_time_s",
      "iso",
      "f_number",
      "white_balance",
      "image_width",
      "image_height",
    ]);
    expect(Object.keys(m.device)).toEqual(["platform", "os_version", "model", "app_version"]);
    expect(Object.keys(m.capture)).toEqual([
      "pressed_at",
      "completed_at",
      "duration_ms",
      "motion_deg",
      "sensor_clock_offset_ms",
      "exif_datetime_original",
      "exif_subsec_time_original",
      "orientation_trace",
    ]);
    expect(Object.keys(m.capture.orientation_trace ?? {})).toEqual([
      "source",
      "t_sensor_ms",
      "alpha",
      "beta",
      "gamma",
    ]);
  });

  it("固定の項目と、渡した値をそのまま入れる", () => {
    const m = buildMetadata(input());
    expect(m.schema_version).toBe(1);
    expect(m.observation_id).toBe("123e4567-e89b-42d3-a456-426614174000");
    expect(m.captured_at).toBe("2026-10-09T03:12:45.123Z");
    expect(m.capture_path).toBe("native");
    expect(m.user_guess).toBeNull();
    expect(m.image_sha256).toBe("a".repeat(64));
    expect(m.location?.accuracy_m).toBe(12.3);
    expect(m.orientation?.pitch_deg).toBe(0);
    expect(m.orientation?.accuracy).toBe("high");
    expect(m.camera.image_width).toBe(4000);
  });

  it("位置の精度が null か 0 以下なら location が null", () => {
    for (const accuracy of [null, 0, -1]) {
      const base = input();
      const m = buildMetadata({
        ...base,
        location: base.location && {
          ...base.location,
          coords: { ...base.location.coords, accuracy },
        },
      });
      expect(m.location).toBeNull();
    }
  });

  it("位置がなければ location が null", () => {
    expect(buildMetadata(input({ location: null })).location).toBeNull();
  });

  it("EXIF がなければ camera の値が null（幅と高さは引数）", () => {
    const m = buildMetadata(input({ exif: null, width: 4032, height: 3024 }));
    expect(m.camera).toEqual({
      focal_length_mm: null,
      focal_length_35mm: null,
      exposure_time_s: null,
      iso: null,
      f_number: null,
      white_balance: null,
      image_width: 4032,
      image_height: 3024,
    });
  });

  it("tz_offset_min は getTimezoneOffset の符号を逆にする", () => {
    jest.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(-540);
    expect(buildMetadata(input()).tz_offset_min).toBe(540);
    jest.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(0);
    expect(Object.is(buildMetadata(input()).tz_offset_min, 0)).toBe(true);
    jest.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(300);
    expect(buildMetadata(input()).tz_offset_min).toBe(-300);
  });

  it("user_guess に予想を入れる", () => {
    expect(buildMetadata(input({ userGuess: "rain" })).user_guess).toBe("rain");
    expect(buildMetadata(input({ userGuess: "no_rain" })).user_guess).toBe("no_rain");
    expect(buildMetadata(input({ userGuess: null })).user_guess).toBeNull();
  });

  it("端末の情報が取れないときは unknown", () => {
    const m = buildMetadata(
      input({ device: { platform: "ios", os_version: null, model: null, app_version: "" } }),
    );
    expect(m.device).toEqual({
      platform: "ios",
      os_version: "unknown",
      model: "unknown",
      app_version: "unknown",
    });
  });

  it("capture に押した時刻・終わった時刻・所要時間を入れる", () => {
    const { capture } = buildMetadata(input());
    expect(capture.pressed_at).toBe("2026-10-09T03:12:45.123Z");
    expect(capture.completed_at).toBe("2026-10-09T03:12:46.108Z");
    expect(capture.duration_ms).toBe(985);
    expect(capture.sensor_clock_offset_ms).toBe(0);
  });

  it("capture の EXIF の時刻は文字列のまま入れる", () => {
    const { capture } = buildMetadata(input());
    expect(capture.exif_datetime_original).toBe("2026:10:09 12:12:46");
    expect(capture.exif_subsec_time_original).toBe("512");
  });

  it("EXIF がない、または値が文字列でないか長すぎるときは null", () => {
    for (const exif of [
      null,
      undefined,
      {},
      { DateTimeOriginal: 20261009, SubSecTimeOriginal: null },
      { DateTimeOriginal: "x".repeat(65), SubSecTimeOriginal: 512 },
    ]) {
      const { capture } = buildMetadata(input({ exif }));
      expect(capture.exif_datetime_original).toBeNull();
      expect(capture.exif_subsec_time_original).toBeNull();
    }
    // 64 文字ちょうどは残る
    const edge = "x".repeat(64);
    expect(
      buildMetadata(input({ exif: { DateTimeOriginal: edge } })).capture.exif_datetime_original,
    ).toBe(edge);
  });

  it("窓のサンプルがなければ motion_deg と orientation_trace が null で、orientation も null", () => {
    const m = buildMetadata(input({ samples: [], offsetMs: null }));
    expect(m.orientation).toBeNull();
    expect(m.capture.motion_deg).toBeNull();
    expect(m.capture.sensor_clock_offset_ms).toBeNull();
    expect(m.capture.orientation_trace).toBeNull();
  });

  it("サンプルが1件なら motion_deg は null で、orientation_trace は1件", () => {
    const m = buildMetadata(input({ samples: [sample(PRESSED, R_NORTH, [0.12, 1.45, -0.03])] }));
    expect(m.capture.motion_deg).toBeNull();
    expect(m.capture.orientation_trace).toEqual({
      source: "expo-sensors DeviceMotion rotation (alpha, beta, gamma)",
      t_sensor_ms: [PRESSED],
      alpha: [0.12],
      beta: [1.45],
      gamma: [-0.03],
    });
  });

  it("orientation_trace は項目ごとの配列で、同じ添字が 1 件のサンプル。時刻の古い順", () => {
    const m = buildMetadata(
      input({
        samples: [sample(300, R_NORTH, [3, 3, 3]), sample(100, R_NORTH, [1, 1, 1]), sample(200, R_NORTH, [2, 2, 2])],
      }),
    );
    const trace = m.capture.orientation_trace;
    expect(trace?.t_sensor_ms).toEqual([100, 200, 300]);
    expect(trace?.alpha).toEqual([1, 2, 3]);
    expect(trace?.beta).toEqual([1, 2, 3]);
    expect(trace?.gamma).toEqual([1, 2, 3]);
  });

  it("motion_deg は最も離れた2サンプルのなす角（北と東なら 90 度）", () => {
    const m = buildMetadata(
      input({ samples: [sample(0, R_NORTH), sample(20, R_EAST), sample(40, R_NORTH)] }),
    );
    expect(m.capture.motion_deg).toBe(90);
  });

  it("motion_deg は小数第2位、sensor_clock_offset_ms は小数第1位に丸める", () => {
    // 北から東へ 1.23456 度だけ傾けたカメラ方向（水平のまま方位だけ変える）
    const az = (1.23456 * Math.PI) / 180;
    const [s, c] = [Math.sin(az), Math.cos(az)];
    const R: Mat3 = [c, 0, -s, -s, 0, -c, 0, 1, 0];
    const m = buildMetadata(
      input({ samples: [sample(0, R_NORTH), sample(20, R)], offsetMs: 1791530000123.456 }),
    );
    expect(m.capture.motion_deg).toBe(1.23);
    expect(m.capture.sensor_clock_offset_ms).toBe(1791530000123.5);
  });

  it("窓のサンプルが 1000 件を超えたら、trace だけ最初と最後を含めて 1000 件に間引く", () => {
    const samples = Array.from({ length: 2500 }, (_, i) => sample(i * 4, R_NORTH, [i, i, i]));
    const m = buildMetadata(input({ samples }));
    const trace = m.capture.orientation_trace;
    expect(trace?.t_sensor_ms).toHaveLength(1000);
    expect(trace?.alpha).toHaveLength(1000);
    expect(trace?.beta).toHaveLength(1000);
    expect(trace?.gamma).toHaveLength(1000);
    expect(trace?.t_sensor_ms[0]).toBe(0);
    expect(trace?.t_sensor_ms[999]).toBe(2499 * 4);
    // 同じ添字は同じサンプル
    expect(trace?.alpha.every((a, i) => a * 4 === trace.t_sensor_ms[i])).toBe(true);
    // orientation は間引く前の全サンプルから求める
    expect(m.orientation?.stddev_deg).not.toBeNull();
  });

  it("ちょうど 1000 件なら間引かない", () => {
    const samples = Array.from({ length: 1000 }, (_, i) => sample(i));
    expect(buildMetadata(input({ samples })).capture.orientation_trace?.t_sensor_ms).toHaveLength(1000);
  });

  it("終わった時刻が押した時刻より前なら、押した時刻に終わったものとみなす", () => {
    const { capture } = buildMetadata(input({ completedAtMs: PRESSED - 300 }));
    expect(capture.completed_at).toBe(capture.pressed_at);
    expect(capture.duration_ms).toBe(0);
  });
});
