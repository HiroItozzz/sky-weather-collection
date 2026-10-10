import { describe, expect, it } from "@jest/globals";
import { magneticModel } from "./declination";
import type { Mat3 } from "./orientation";
import { buildRecord, motionDeg, type LocationInput, type Sample } from "./record";

const PRESSED = Date.parse("2026-10-09T03:12:45.123Z");

// 縦持ち・カメラ北・水平
const R_NORTH: Mat3 = [1, 0, 0, 0, 0, -1, 0, 1, 0];
// 縦持ち・カメラ東・水平
const R_EAST: Mat3 = [0, 0, -1, -1, 0, 0, 0, 1, 0];

/** 壁時計の時刻が t になる（offsetMs が 0 のとき）サンプル。 */
function at(t: number, R: Mat3): Sample {
  return { tSensorMs: t, alpha: 0, beta: 0, gamma: 0, R };
}

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
      completedAtMs: PRESSED,
      offsetMs: 0,
      samples: [at(PRESSED, R_NORTH)],
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
      completedAtMs: PRESSED,
      offsetMs: 0,
      samples: [at(PRESSED, R_EAST)],
      location: location(),
      headingAccuracy: null,
    });
    const decl = magneticModel(35.6812362, 139.7671248, 45.65, new Date(PRESSED)).declinationDeg;
    expect(r.orientation?.declination_deg).toBeCloseTo(decl, 2);
    expect(r.orientation?.azimuth_deg).toBeCloseTo(90 + decl, 2);
  });

  it("窓の中点（押した時刻と終わった時刻の真ん中）にいちばん近いサンプルが選ばれる", () => {
    // 中点は PRESSED + 200。押した時刻にいちばん近いのは R_NORTH の PRESSED - 100 だが、選ばれるのは R_EAST
    const samples: Sample[] = [
      at(PRESSED - 100, R_NORTH),
      at(PRESSED + 190, R_EAST),
      at(PRESSED + 400, R_NORTH),
    ];
    const r = buildRecord({
      pressedAtMs: PRESSED,
      completedAtMs: PRESSED + 400,
      samples,
      offsetMs: 0,
      location: location(),
      headingAccuracy: 2,
    });
    const decl = r.orientation?.declination_deg ?? NaN;
    expect(r.orientation?.azimuth_deg).toBeCloseTo(90 + decl, 2);
    expect(r.orientation?.stddev_deg).toBeGreaterThan(0);
  });

  it("サンプルの壁時計の時刻は tSensorMs に offsetMs を足したもの", () => {
    // offsetMs = 1000 なら、tSensorMs が PRESSED + 200 − 1000 のものが中点にいちばん近い
    const samples: Sample[] = [
      at(PRESSED - 100 - 1000, R_NORTH),
      at(PRESSED + 190 - 1000, R_EAST),
      at(PRESSED + 400 - 1000, R_NORTH),
    ];
    const r = buildRecord({
      pressedAtMs: PRESSED,
      completedAtMs: PRESSED + 400,
      samples,
      offsetMs: 1000,
      location: location(),
      headingAccuracy: null,
    });
    const decl = r.orientation?.declination_deg ?? NaN;
    expect(r.orientation?.azimuth_deg).toBeCloseTo(90 + decl, 2);
  });

  it("中点から同じ近さのサンプルが2つあるときは先のものが選ばれる", () => {
    const samples: Sample[] = [at(PRESSED + 100, R_EAST), at(PRESSED + 300, R_NORTH)];
    const r = buildRecord({
      pressedAtMs: PRESSED,
      completedAtMs: PRESSED + 400,
      samples,
      offsetMs: 0,
      location: location(),
      headingAccuracy: null,
    });
    expect(r.orientation?.azimuth_deg).toBeCloseTo(90 + (r.orientation?.declination_deg ?? NaN), 2);
  });

  it("位置がないと declination_deg と azimuth_deg は null", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      completedAtMs: PRESSED,
      offsetMs: 0,
      samples: [at(PRESSED, R_NORTH)],
      location: null,
      headingAccuracy: 1,
    });
    expect(r.location).toBeNull();
    expect(r.orientation?.declination_deg).toBeNull();
    expect(r.orientation?.azimuth_deg).toBeNull();
    expect(r.orientation?.pitch_deg).toBe(0);
  });

  it("サンプルがないと orientation は null", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      completedAtMs: PRESSED,
      samples: [],
      offsetMs: null,
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.orientation).toBeNull();
    expect(r.location).not.toBeNull();
  });

  it("サンプルが 1 個なら stddev_deg は null", () => {
    const r = buildRecord({
      pressedAtMs: PRESSED,
      completedAtMs: PRESSED,
      offsetMs: 0,
      samples: [at(PRESSED, R_NORTH)],
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.orientation?.stddev_deg).toBeNull();
  });

  it("altitudeAccuracy が 0 以下か null なら altitude_m は null", () => {
    for (const acc of [0, -1, null]) {
      const r = buildRecord({
        pressedAtMs: PRESSED,
        completedAtMs: PRESSED,
        offsetMs: 0,
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
      completedAtMs: PRESSED,
      offsetMs: 0,
      samples: [at(PRESSED, R)],
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
      completedAtMs: PRESSED,
      offsetMs: 0,
      samples: [at(PRESSED, R)],
      location: location(),
      headingAccuracy: 3,
    });
    expect(r.orientation?.azimuth_deg).toBe(172.08);
  });

  it("accuracy の対応づけ", () => {
    const acc = (v: number | null) =>
      buildRecord({
        pressedAtMs: PRESSED,
        completedAtMs: PRESSED,
        offsetMs: 0,
        samples: [at(PRESSED, R_NORTH)],
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

describe("motionDeg", () => {
  it("サンプルが 0 個か 1 個なら null", () => {
    expect(motionDeg([])).toBeNull();
    expect(motionDeg([at(0, R_NORTH)])).toBeNull();
  });

  it("同じ向きのサンプルだけなら 0", () => {
    expect(motionDeg([at(0, R_NORTH), at(20, R_NORTH), at(40, R_NORTH)])).toBeCloseTo(0, 10);
  });

  it("北と東のなす角は 90 度", () => {
    expect(motionDeg([at(0, R_NORTH), at(20, R_EAST)])).toBeCloseTo(90, 10);
  });

  it("最も離れた2つのなす角を返す（時刻の順や隣どうしとは限らない）", () => {
    // 北・東・北の順でも、最大は北と東の 90 度
    expect(motionDeg([at(0, R_NORTH), at(20, R_EAST), at(40, R_NORTH)])).toBeCloseTo(90, 10);
    expect(motionDeg([at(0, R_EAST), at(20, R_NORTH), at(40, R_NORTH)])).toBeCloseTo(90, 10);
  });

  it("真反対（180 度）まで求められる", () => {
    const R_SOUTH: Mat3 = [-1, 0, 0, 0, 0, 1, 0, 1, 0];
    expect(motionDeg([at(0, R_NORTH), at(20, R_SOUTH)])).toBeCloseTo(180, 10);
  });

  it("水平のカメラ（北）と真上は 90 度、真下と真上は 180 度", () => {
    // カメラ方向は R の第3列の符号を逆にしたもの
    const R_DOWN: Mat3 = [1, 0, 0, 0, 1, 0, 0, 0, 1];
    const R_ZENITH: Mat3 = [1, 0, 0, 0, -1, 0, 0, 0, -1];
    expect(motionDeg([at(0, R_NORTH), at(20, R_ZENITH)])).toBeCloseTo(90, 10);
    expect(motionDeg([at(0, R_DOWN), at(20, R_ZENITH)])).toBeCloseTo(180, 10);
  });
});
