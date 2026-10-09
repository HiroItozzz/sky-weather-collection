import { describe, expect, it } from "@jest/globals";
import { magneticModel } from "./declination";
import type { Mat3 } from "./orientation";
import { buildRecord, type LocationInput, type Sample } from "./record";

const PRESSED = Date.parse("2026-10-09T03:12:45.123Z");

// 縦持ち・カメラ北・水平
const R_NORTH: Mat3 = [1, 0, 0, 0, 0, -1, 0, 1, 0];
// 縦持ち・カメラ東・水平
const R_EAST: Mat3 = [0, 0, -1, -1, 0, 0, 0, 1, 0];

function location(overrides: Partial<LocationInput["coords"]> = {}): LocationInput {
  return {
    coords: {
      latitude: 35.6812362,
      longitude: 139.7671248,
      accuracy: 12.34,
      altitude: 45.65,
      altitudeAccuracy: 3,
      ...overrides,
    },
    timestamp: Date.parse("2026-10-09T03:12:44.000Z"),
  };
}

describe("buildRecord", () => {
  it("captured_at と location を組み立てる", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      samples: [{ t: PRESSED, R: R_NORTH }],
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.captured_at).toBe("2026-10-09T03:12:45.123Z");
    expect(r.location).toEqual({
      lat: 35.6812362,
      lon: 139.7671248,
      accuracy_m: 12.3,
      altitude_m: 45.7,
      fix_time: "2026-10-09T03:12:44.000Z",
    });
    expect(r.orientation?.accuracy).toBe("high");
    expect(r.orientation?.pitch_deg).toBe(0);
    expect(r.orientation?.roll_deg).toBe(0);
  });

  it("真北基準の方位角は磁北の方位角に偏角を足したもの", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      samples: [{ t: PRESSED, R: R_EAST }],
      location: location(),
      headingAccuracy: null,
    });
    const decl = magneticModel(35.6812362, 139.7671248, 45.65, new Date(PRESSED)).declinationDeg;
    expect(r.orientation?.declination_deg).toBeCloseTo(decl, 2);
    expect(r.orientation?.azimuth_deg).toBeCloseTo(90 + decl, 2);
  });

  it("押した時刻にいちばん近いサンプルが選ばれる", () => {
    const samples: Sample[] = [
      { t: PRESSED - 500, R: R_NORTH },
      { t: PRESSED - 30, R: R_EAST },
      { t: PRESSED + 100, R: R_NORTH },
      { t: PRESSED + 500, R: R_NORTH },
    ];
    const r = buildRecord({ pressedAtMs: PRESSED, samples, location: location(), headingAccuracy: 2 });
    const decl = r.orientation?.declination_deg ?? NaN;
    expect(r.orientation?.azimuth_deg).toBeCloseTo(90 + decl, 2);
    expect(r.orientation?.stddev_deg).toBeGreaterThan(0);
  });

  it("位置がないと declination_deg と azimuth_deg は null", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      samples: [{ t: PRESSED, R: R_NORTH }],
      location: null,
      headingAccuracy: 1,
    });
    expect(r.location).toBeNull();
    expect(r.orientation?.declination_deg).toBeNull();
    expect(r.orientation?.azimuth_deg).toBeNull();
    expect(r.orientation?.pitch_deg).toBe(0);
  });

  it("サンプルがないと orientation は null", () => {
    const r = buildRecord({ pressedAtMs: PRESSED, samples: [], location: location(), headingAccuracy: 3 });
    expect(r.orientation).toBeNull();
    expect(r.location).not.toBeNull();
  });

  it("サンプルが 1 個なら stddev_deg は null", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      samples: [{ t: PRESSED, R: R_NORTH }],
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.orientation?.stddev_deg).toBeNull();
  });

  it("altitudeAccuracy が 0 以下か null なら altitude_m は null", () => {
    for (const acc of [0, -1, null]) {
      const r = buildRecord({
        pressedAtMs: PRESSED,
        samples: [],
        location: location({ altitudeAccuracy: acc }),
        headingAccuracy: null,
      });
      expect(r.location?.altitude_m).toBeNull();
    }
  });

  it("方位角の丸めで 360 にならない（359.996 → 0）", () => {
    // 偏角を足したあとが 359.996 になるように、磁北の方位角を調整する
    const decl = magneticModel(35.6812362, 139.7671248, 45.65, new Date(PRESSED)).declinationDeg;
    const az = (359.996 - decl) * (Math.PI / 180);
    // 方位 az に向けた縦持ち・水平の R
    const s = Math.sin(az);
    const c = Math.cos(az);
    const R: Mat3 = [c, 0, -s, -s, 0, -c, 0, 1, 0];
    const r = buildRecord({
      pressedAtMs: PRESSED,
      samples: [{ t: PRESSED, R }],
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.orientation?.azimuth_deg).toBe(0);
  });

  it("方位角は小数第2位ちょうどになる（剰余の丸め誤差が残らない）", () => {
    const decl = magneticModel(35.6812362, 139.7671248, 45.65, new Date(PRESSED)).declinationDeg;
    const az = (172.08 - decl) * (Math.PI / 180);
    const s = Math.sin(az);
    const c = Math.cos(az);
    const R: Mat3 = [c, 0, -s, -s, 0, -c, 0, 1, 0];
    const r = buildRecord({
      pressedAtMs: PRESSED,
      samples: [{ t: PRESSED, R }],
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.orientation?.azimuth_deg).toBe(172.08);
  });

  it("accuracy の対応づけ", () => {
    const acc = (v: number | null) =>
      buildRecord({
        pressedAtMs: PRESSED,
        samples: [{ t: PRESSED, R: R_NORTH }],
        location: null,
        headingAccuracy: v,
      }).orientation?.accuracy;
    expect(acc(3)).toBe("high");
    expect(acc(2)).toBe("medium");
    expect(acc(1)).toBe("low");
    expect(acc(0)).toBe("unreliable");
    expect(acc(4)).toBeNull();
    expect(acc(-1)).toBeNull();
    expect(acc(null)).toBeNull();
  });
});
