import { afterEach, describe, expect, it, jest } from "@jest/globals";
import type { Mat3 } from "./orientation";
import { buildMetadata, type MetadataInput } from "./metadata";

const PRESSED = Date.parse("2026-10-09T03:12:45.123Z");
const R_NORTH: Mat3 = [1, 0, 0, 0, 0, -1, 0, 1, 0];

function input(overrides: Partial<MetadataInput> = {}): MetadataInput {
  return {
    observationId: "123e4567-e89b-42d3-a456-426614174000",
    pressedAtMs: PRESSED,
    samples: [
      { t: PRESSED - 100, R: R_NORTH },
      { t: PRESSED + 100, R: R_NORTH },
    ],
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
    exif: { FocalLength: 4.38, ISOSpeedRatings: 100, ImageWidth: 4000, ImageLength: 3000 },
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
});
