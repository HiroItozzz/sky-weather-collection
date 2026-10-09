import { describe, expect, it } from "@jest/globals";
import {
  angularSpreadDeg,
  cameraAngles,
  cameraDirection,
  normalizeDeg,
  rotationFromDeviceMotion,
  toTrueAzimuth,
  type Mat3,
  type Vec3,
} from "./orientation";

const RAD = Math.PI / 180;

// 乱数の種を固定した簡単な疑似乱数（mulberry32）。
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// 端末の x, y, z が世界のどちらを向くかから R を作る（列として並べる）。
function fromColumns(x: Vec3, y: Vec3, z: Vec3): Mat3 {
  return [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
}

const E: Vec3 = [1, 0, 0];
const N: Vec3 = [0, 1, 0];
const U: Vec3 = [0, 0, 1];
const S: Vec3 = [0, -1, 0];
const W: Vec3 = [-1, 0, 0];
const D: Vec3 = [0, 0, -1];

// 方位・仰角・ロール（度）から R を作る補助関数。
function rotationFromAngles(azDeg: number, pitchDeg: number, rollDeg: number): Mat3 {
  const az = azDeg * RAD;
  const p = pitchDeg * RAD;
  const th = rollDeg * RAD;
  const c: Vec3 = [Math.cos(p) * Math.sin(az), Math.cos(p) * Math.cos(az), Math.sin(p)];
  const u0: Vec3 = [-Math.sin(p) * Math.sin(az), -Math.sin(p) * Math.cos(az), Math.cos(p)];
  const r0: Vec3 = [
    c[1] * u0[2] - c[2] * u0[1],
    c[2] * u0[0] - c[0] * u0[2],
    c[0] * u0[1] - c[1] * u0[0],
  ];
  const mix = (a: Vec3, ka: number, b: Vec3, kb: number): Vec3 => [
    ka * a[0] + kb * b[0],
    ka * a[1] + kb * b[1],
    ka * a[2] + kb * b[2],
  ];
  const u = mix(u0, Math.cos(th), r0, Math.sin(th));
  const r = mix(r0, Math.cos(th), u0, -Math.sin(th));
  return fromColumns(r, u, [-c[0], -c[1], -c[2]]);
}

// ランダムな四元数から回転行列を作る。
function randomRotation(rand: () => number): Mat3 {
  let q: number[];
  let n: number;
  do {
    q = [0, 0, 0, 0].map(() => rand() * 2 - 1);
    n = Math.hypot(q[0], q[1], q[2], q[3]);
  } while (n < 0.1 || n > 1);
  const [w, x, y, z] = q.map((v) => v / n);
  return [
    1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
    2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
    2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y),
  ];
}

// Android の getOrientation の式で alpha, beta, gamma を求める。
function toDeviceMotionAngles(R: Mat3): [number, number, number] {
  const azimuth = Math.atan2(R[1], R[4]);
  const pitch = Math.asin(-R[7]);
  const roll = Math.atan2(-R[6], R[8]);
  return [-azimuth, -pitch, roll];
}

function angleDiffDeg(a: number, b: number): number {
  return Math.abs(((a - b + 540) % 360) - 180);
}

describe("cameraAngles（既知の姿勢）", () => {
  it("縦持ち・カメラ北・水平", () => {
    const a = cameraAngles(fromColumns(E, U, S));
    expect(a.azimuthDeg).toBeCloseTo(0, 9);
    expect(a.pitchDeg).toBeCloseTo(0, 9);
    expect(a.rollDeg).toBeCloseTo(0, 9);
  });

  it("縦持ち・カメラ東・水平", () => {
    const a = cameraAngles(fromColumns(S, U, W));
    expect(a.azimuthDeg).toBeCloseTo(90, 9);
    expect(a.pitchDeg).toBeCloseTo(0, 9);
    expect(a.rollDeg).toBeCloseTo(0, 9);
  });

  it("縦持ち・カメラ西・水平", () => {
    const a = cameraAngles(fromColumns(N, U, E));
    expect(a.azimuthDeg).toBeCloseTo(270, 9);
    expect(a.pitchDeg).toBeCloseTo(0, 9);
  });

  it("縦持ち・カメラ南を向き 45° 見上げる", () => {
    const s = Math.SQRT1_2;
    const y: Vec3 = [0, s, s]; // 北·cos45 + 天頂·sin45
    const z: Vec3 = [0, s, -s]; // カメラの反対側
    const a = cameraAngles(fromColumns(W, y, z));
    expect(a.azimuthDeg).toBeCloseTo(180, 9);
    expect(a.pitchDeg).toBeCloseTo(45, 9);
    expect(a.rollDeg).toBeCloseTo(0, 9);
  });

  it("縦持ち・カメラ北・時計回りに 30° 回す", () => {
    const t = 30 * RAD;
    const x: Vec3 = [Math.cos(t), 0, -Math.sin(t)];
    const y: Vec3 = [Math.sin(t), 0, Math.cos(t)];
    const a = cameraAngles(fromColumns(x, y, S));
    expect(a.azimuthDeg).toBeCloseTo(0, 9);
    expect(a.pitchDeg).toBeCloseTo(0, 9);
    expect(a.rollDeg).toBeCloseTo(30, 9);
  });

  it("画面を上にして机に置く・上端が北（真下）", () => {
    const a = cameraAngles(fromColumns(E, N, U));
    expect(a.pitchDeg).toBeCloseTo(-90, 9);
    expect(a.azimuthDeg).toBeCloseTo(0, 9);
    expect(a.rollDeg).toBe(0);
  });

  it("画面を下にして頭上にかざす・上端が北（真上）", () => {
    const a = cameraAngles(fromColumns(W, N, D));
    expect(a.pitchDeg).toBeCloseTo(90, 9);
    expect(a.azimuthDeg).toBeCloseTo(180, 9);
    expect(a.rollDeg).toBe(0);
  });

  it("真上から 0.5° 手前でも方位とロールが飛ばない", () => {
    const p = 89.5 * RAD;
    const y: Vec3 = [0, -Math.sin(p), Math.cos(p)];
    const z: Vec3 = [0, -Math.cos(p), -Math.sin(p)];
    const a = cameraAngles(fromColumns(E, y, z));
    expect(a.azimuthDeg).toBeCloseTo(0, 9);
    expect(a.pitchDeg).toBeCloseTo(89.5, 9);
    expect(a.rollDeg).toBeCloseTo(0, 9);
  });
});

describe("cameraDirection", () => {
  it("端末の -Z を世界座標で返す", () => {
    const c = cameraDirection(fromColumns(E, U, S));
    [0, 1, 0].forEach((v, i) => expect(c[i]).toBeCloseTo(v, 12));
  });
});

describe("rotationFromDeviceMotion", () => {
  it("(0, 0, 0) は単位行列", () => {
    const R = rotationFromDeviceMotion(0, 0, 0);
    [1, 0, 0, 0, 1, 0, 0, 0, 1].forEach((v, i) => expect(R[i]).toBeCloseTo(v, 12));
  });

  it("(0, π/2, 0) は縦持ち・カメラ北・水平と同じ", () => {
    const R = rotationFromDeviceMotion(0, Math.PI / 2, 0);
    const expected = fromColumns(E, U, S);
    expected.forEach((v, i) => expect(R[i]).toBeCloseTo(v, 12));
    const a = cameraAngles(R);
    expect(a.azimuthDeg).toBeCloseTo(0, 9);
    expect(a.pitchDeg).toBeCloseTo(0, 9);
  });

  it("(-π/2, 0, 0) は画面を上にして上端が東", () => {
    const R = rotationFromDeviceMotion(-Math.PI / 2, 0, 0);
    // u = R · (0, 1, 0) は東
    expect(R[1]).toBeCloseTo(1, 12);
    expect(R[4]).toBeCloseTo(0, 12);
    expect(R[7]).toBeCloseTo(0, 12);
  });

  it("ランダムな R を getOrientation の式で分解してから戻すと元に戻る", () => {
    const rand = mulberry32(12345);
    for (let i = 0; i < 2000; i++) {
      const R = randomRotation(rand);
      const [alpha, beta, gamma] = toDeviceMotionAngles(R);
      const back = rotationFromDeviceMotion(alpha, beta, gamma);
      R.forEach((v, k) => expect(Math.abs(back[k] - v)).toBeLessThan(1e-9));
    }
  });
});

describe("cameraAngles（往復）", () => {
  it("ランダムな方位・仰角・ロールから R を作って戻すと一致する", () => {
    const rand = mulberry32(678);
    for (let i = 0; i < 2000; i++) {
      const az = rand() * 360;
      const pitch = (rand() * 2 - 1) * 89.9;
      const roll = (rand() * 2 - 1) * 179.9;
      const a = cameraAngles(rotationFromAngles(az, pitch, roll));
      expect(angleDiffDeg(a.azimuthDeg, az)).toBeLessThan(1e-6);
      expect(Math.abs(a.pitchDeg - pitch)).toBeLessThan(1e-6);
      expect(angleDiffDeg(a.rollDeg, roll)).toBeLessThan(1e-6);
    }
  });
});

describe("normalizeDeg", () => {
  it("0 以上 360 未満に直す", () => {
    expect(normalizeDeg(-10)).toBe(350);
    expect(normalizeDeg(360)).toBe(0);
    expect(normalizeDeg(720.5)).toBeCloseTo(0.5, 9);
    expect(normalizeDeg(-1e-15)).toBe(0);
  });
});

describe("toTrueAzimuth", () => {
  it("偏角を足して 0〜360 に直す", () => {
    expect(toTrueAzimuth(5, -7.9)).toBeCloseTo(357.1, 9);
    expect(toTrueAzimuth(355, 10)).toBeCloseTo(5, 9);
  });
});

describe("angularSpreadDeg", () => {
  it("同じベクトル 3 つは 0", () => {
    expect(angularSpreadDeg([N, N, N])).toBeCloseTo(0, 12);
  });

  it("1 個は null", () => {
    expect(angularSpreadDeg([N])).toBeNull();
  });

  it("北に対して東西に ±1° 振った 2 つは 1", () => {
    const t = 1 * RAD;
    const a: Vec3 = [Math.sin(t), Math.cos(t), 0];
    const b: Vec3 = [-Math.sin(t), Math.cos(t), 0];
    expect(angularSpreadDeg([a, b])).toBeCloseTo(1, 9);
  });

  it("和の長さが 0 なら null", () => {
    expect(angularSpreadDeg([N, S])).toBeNull();
  });

  it("真上付近でもなす角で測る", () => {
    const t = 0.5 * RAD;
    const a: Vec3 = [Math.sin(t), 0, Math.cos(t)];
    const b: Vec3 = [-Math.sin(t), 0, Math.cos(t)];
    expect(angularSpreadDeg([a, b])).toBeCloseTo(0.5, 9);
  });
});
