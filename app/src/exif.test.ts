import { describe, expect, it } from "@jest/globals";
import { parseCameraExif } from "./exif";

const FULL = {
  FocalLength: 4.38,
  FocalLengthIn35mmFilm: 24,
  ExposureTime: 0.01,
  ISOSpeedRatings: 100,
  FNumber: 1.8,
  WhiteBalance: 0,
  ImageWidth: 4000,
  ImageLength: 3000,
};

describe("parseCameraExif", () => {
  it("数値のタグをそのまま取り出す", () => {
    expect(parseCameraExif(FULL, 1, 1)).toEqual({
      focal_length_mm: 4.38,
      focal_length_35mm: 24,
      exposure_time_s: 0.01,
      iso: 100,
      f_number: 1.8,
      white_balance: "auto",
      image_width: 4000,
      image_height: 3000,
    });
  });

  it("EXIF が null か undefined なら幅と高さ以外は null", () => {
    const expected = {
      focal_length_mm: null,
      focal_length_35mm: null,
      exposure_time_s: null,
      iso: null,
      f_number: null,
      white_balance: null,
      image_width: 4032,
      image_height: 3024,
    };
    expect(parseCameraExif(null, 4032, 3024)).toEqual(expected);
    expect(parseCameraExif(undefined, 4032, 3024)).toEqual(expected);
  });

  it("文字列の分数と10進数を読む", () => {
    const c = parseCameraExif({ ExposureTime: "1/100", FNumber: "2.8", FocalLength: " 9/2 " });
    expect(c.exposure_time_s).toBeCloseTo(0.01);
    expect(c.f_number).toBe(2.8);
    expect(c.focal_length_mm).toBe(4.5);
  });

  it("読めない値と有限でない値は null", () => {
    const c = parseCameraExif({
      ExposureTime: "1/0",
      FNumber: "abc",
      FocalLength: Number.NaN,
      FocalLengthIn35mmFilm: Number.POSITIVE_INFINITY,
      ISOSpeedRatings: {},
    });
    expect(c.exposure_time_s).toBeNull();
    expect(c.f_number).toBeNull();
    expect(c.focal_length_mm).toBeNull();
    expect(c.focal_length_35mm).toBeNull();
    expect(c.iso).toBeNull();
  });

  it("FocalLengthIn35mmFilm が 0 なら null", () => {
    expect(parseCameraExif({ FocalLengthIn35mmFilm: 0 }).focal_length_35mm).toBeNull();
  });

  it("ISO は整数に丸め、なければ PhotographicSensitivity を使う", () => {
    expect(parseCameraExif({ ISOSpeedRatings: 99.6 }).iso).toBe(100);
    expect(parseCameraExif({ PhotographicSensitivity: 400 }).iso).toBe(400);
    expect(parseCameraExif({ ISOSpeedRatings: 200, PhotographicSensitivity: 400 }).iso).toBe(200);
  });

  it("WhiteBalance の対応", () => {
    expect(parseCameraExif({ WhiteBalance: 0 }).white_balance).toBe("auto");
    expect(parseCameraExif({ WhiteBalance: 1 }).white_balance).toBe("manual");
    expect(parseCameraExif({ WhiteBalance: 2 }).white_balance).toBeNull();
    expect(parseCameraExif({}).white_balance).toBeNull();
  });

  it("幅と高さは ImageWidth、PixelXDimension、引数の順に決める", () => {
    expect(parseCameraExif({ ImageWidth: 10, ImageLength: 20, PixelXDimension: 1, PixelYDimension: 2 }, 5, 6)).toMatchObject({
      image_width: 10,
      image_height: 20,
    });
    expect(parseCameraExif({ PixelXDimension: 1, PixelYDimension: 2 }, 5, 6)).toMatchObject({
      image_width: 1,
      image_height: 2,
    });
    expect(parseCameraExif({}, 5, 6)).toMatchObject({ image_width: 5, image_height: 6 });
  });

  it("幅と高さが 0 以下なら次の候補に進み、なければ null", () => {
    expect(parseCameraExif({ ImageWidth: 0, PixelXDimension: 7 }, -1, -1)).toMatchObject({
      image_width: 7,
      image_height: null,
    });
    expect(parseCameraExif({ ImageWidth: -1 }, 0, null)).toMatchObject({
      image_width: null,
      image_height: null,
    });
  });
});
