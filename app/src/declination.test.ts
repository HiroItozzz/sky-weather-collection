import { describe, expect, it, jest } from "@jest/globals";
import { magneticModel } from "./declination";

const DATE = new Date("2026-10-09T00:00:00Z");

describe("magneticModel", () => {
  it("東京の偏角と全磁力が妥当な範囲に入る", () => {
    const m = magneticModel(35.68, 139.77, null, DATE);
    expect(m.declinationDeg).toBeGreaterThanOrEqual(-8.4);
    expect(m.declinationDeg).toBeLessThanOrEqual(-7.4);
    expect(m.totalIntensityUT).toBeGreaterThanOrEqual(44);
    expect(m.totalIntensityUT).toBeLessThanOrEqual(48);
  });

  it("札幌の偏角は東京より西に大きい", () => {
    const tokyo = magneticModel(35.68, 139.77, null, DATE);
    const sapporo = magneticModel(43.06, 141.35, null, DATE);
    expect(sapporo.declinationDeg).toBeLessThan(tokyo.declinationDeg);
  });

  it("高度が null でも 0 m と同じ値になる", () => {
    expect(magneticModel(35.68, 139.77, null, DATE)).toEqual(
      magneticModel(35.68, 139.77, 0, DATE),
    );
  });

  it("有効期間外の日付でも例外にならない", () => {
    const spy = jest.spyOn(console, "error").mockImplementation(() => {});
    try {
      const m = magneticModel(35.68, 139.77, 0, new Date("2035-01-01T00:00:00Z"));
      expect(Number.isFinite(m.declinationDeg)).toBe(true);
    } finally {
      spy.mockRestore();
    }
  });
});
