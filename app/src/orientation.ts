// 端末の向きの計算。世界座標は ENU（x: 東、y: 北、z: 天頂）、端末座標は Android の定義。
// 仕様は docs/m2-app-sensors.md の 3 節。

export type Vec3 = readonly [number, number, number];
/** 長さ 9、行優先。`R[3*i + j]` が i 行 j 列。端末座標のベクトルを世界座標に直す。 */
export type Mat3 = readonly number[];

const DEG = 180 / Math.PI;
const POLE_EPSILON = 1e-9;

function mul(a: Mat3, b: Mat3): number[] {
  const out: number[] = [];
  for (let i = 0; i < 3; i++) {
    for (let j = 0; j < 3; j++) {
      out.push(a[3 * i] * b[j] + a[3 * i + 1] * b[3 + j] + a[3 * i + 2] * b[6 + j]);
    }
  }
  return out;
}

/** DeviceMotion の `rotation`（ラジアン）から、Rz(alpha) · Rx(beta) · Ry(gamma) を作る。 */
export function rotationFromDeviceMotion(alpha: number, beta: number, gamma: number): Mat3 {
  const [ca, sa] = [Math.cos(alpha), Math.sin(alpha)];
  const [cb, sb] = [Math.cos(beta), Math.sin(beta)];
  const [cg, sg] = [Math.cos(gamma), Math.sin(gamma)];
  const rz = [ca, -sa, 0, sa, ca, 0, 0, 0, 1];
  const rx = [1, 0, 0, 0, cb, -sb, 0, sb, cb];
  const ry = [cg, 0, sg, 0, 1, 0, -sg, 0, cg];
  return mul(mul(rz, rx), ry);
}

/** 端末の -Z（背面カメラの向き）を世界座標で返す。 */
export function cameraDirection(R: Mat3): Vec3 {
  return [-R[2], -R[5], -R[8]];
}

/** 0 以上 360 未満に直す。丸め誤差で 360 になる場合も 0 にする。 */
export function normalizeDeg(deg: number): number {
  const x = ((deg % 360) + 360) % 360;
  return x >= 360 ? 0 : x;
}

/** 磁北基準の方位角を真北基準に直す。偏角は東偏が正。 */
export function toTrueAzimuth(magneticAzimuthDeg: number, declinationDeg: number): number {
  return normalizeDeg(magneticAzimuthDeg + declinationDeg);
}

/** カメラの方位角・仰角・ロール（度）。方位角は R の北を基準にする。 */
export function cameraAngles(R: Mat3): { azimuthDeg: number; pitchDeg: number; rollDeg: number } {
  const [cE, cN, cU] = cameraDirection(R);
  const pitchDeg = Math.asin(Math.max(-1, Math.min(1, cU))) * DEG;
  const h = Math.hypot(cE, cN);

  if (h >= POLE_EPSILON) {
    // r: 端末の右、u: 端末の上。天頂成分だけ使う。
    const rU = R[6];
    const uU = R[7];
    return {
      azimuthDeg: normalizeDeg(Math.atan2(cE, cN) * DEG),
      pitchDeg,
      rollDeg: Math.atan2(-rU, uU) * DEG,
    };
  }

  // カメラがちょうど真上か真下。ロールを 0 として、方位角は端末の上端の向きで決める。
  const sign = cU > 0 ? 1 : -1;
  const sE = -sign * R[1];
  const sN = -sign * R[4];
  return {
    azimuthDeg: normalizeDeg(Math.atan2(sE, sN) * DEG),
    pitchDeg,
    rollDeg: 0,
  };
}

function normalize(v: Vec3): Vec3 | null {
  const len = Math.hypot(v[0], v[1], v[2]);
  if (len === 0) return null;
  return [v[0] / len, v[1] / len, v[2] / len];
}

/** カメラ方向ベクトルの、平均方向からのなす角の二乗平均平方根（度）。 */
export function angularSpreadDeg(directions: Vec3[]): number | null {
  if (directions.length < 2) return null;
  const units: Vec3[] = [];
  const sum: [number, number, number] = [0, 0, 0];
  for (const d of directions) {
    const u = normalize(d);
    if (u === null) continue;
    units.push(u);
    sum[0] += u[0];
    sum[1] += u[1];
    sum[2] += u[2];
  }
  if (units.length < 2) return null;
  const m = normalize(sum);
  if (m === null) return null;

  let squares = 0;
  for (const a of units) {
    const cross = Math.hypot(
      a[1] * m[2] - a[2] * m[1],
      a[2] * m[0] - a[0] * m[2],
      a[0] * m[1] - a[1] * m[0],
    );
    const dot = a[0] * m[0] + a[1] * m[1] + a[2] * m[2];
    const angle = Math.atan2(cross, dot) * DEG;
    squares += angle * angle;
  }
  return Math.sqrt(squares / units.length);
}
