// WMM（geomagnetism）による偏角と全磁力。仕様は docs/m2-app-sensors.md の 4 節。
import { model } from "geomagnetism";

/**
 * 位置と日時から偏角（度、東偏が正）と全磁力（µT）を求める。
 * 高度が null のときは 0 km とする。モデルの有効期間外でも例外にせず、最新のモデルで計算する
 * （ライブラリの既定では例外になるので allowOutOfBoundsModel を指定する。console.error に警告が出る）。
 */
export function magneticModel(
  lat: number,
  lon: number,
  altitudeM: number | null,
  date: Date,
): { declinationDeg: number; totalIntensityUT: number } {
  const altitudeKm = (altitudeM ?? 0) / 1000;
  const point = model(date, { allowOutOfBoundsModel: true }).point([lat, lon, altitudeKm]);
  return { declinationDeg: point.decl, totalIntensityUT: point.f / 1000 };
}
