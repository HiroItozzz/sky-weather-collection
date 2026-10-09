// takePictureAsync の EXIF から、サーバーに送る camera の項目を作る。仕様は docs/m3-app-capture.md の 5 節。

export type CameraMetadata = {
  focal_length_mm: number | null;
  focal_length_35mm: number | null;
  exposure_time_s: number | null;
  iso: number | null;
  f_number: number | null;
  white_balance: string | null;
  image_width: number | null;
  image_height: number | null;
};

export type ExifInput = Record<string, unknown>;

const FRACTION = /^([+-]?\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/;
const DECIMAL = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

/** 数値、または "1/100" や "2.8" の文字列を数値として読む。読めない値と有限でない値は null。 */
function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const text = value.trim();
  const fraction = FRACTION.exec(text);
  if (fraction !== null) {
    const denominator = Number(fraction[2]);
    if (denominator === 0) return null;
    const result = Number(fraction[1]) / denominator;
    return Number.isFinite(result) ? result : null;
  }
  if (DECIMAL.test(text)) {
    const result = Number(text);
    return Number.isFinite(result) ? result : null;
  }
  return null;
}

/** 画素の大きさ。整数に丸め、0 以下は null。 */
function toPixels(value: unknown): number | null {
  const n = toNumber(value);
  if (n === null) return null;
  const rounded = Math.round(n);
  return rounded > 0 ? rounded : null;
}

function firstPixels(...values: unknown[]): number | null {
  for (const v of values) {
    const n = toPixels(v);
    if (n !== null) return n;
  }
  return null;
}

function toWhiteBalance(value: unknown): string | null {
  const n = toNumber(value);
  if (n === 0) return "auto";
  if (n === 1) return "manual";
  return null;
}

/**
 * EXIF から camera の項目を作る。exif が null か undefined のときは、幅と高さ以外はすべて null。
 * 幅と高さは EXIF の ImageWidth / ImageLength、なければ PixelXDimension / PixelYDimension、
 * なければ引数の値（takePictureAsync の戻り値）。
 */
export function parseCameraExif(
  exif: ExifInput | null | undefined,
  width?: number | null,
  height?: number | null,
): CameraMetadata {
  const e: ExifInput = exif ?? {};

  const iso = toNumber(e.ISOSpeedRatings) ?? toNumber(e.PhotographicSensitivity);
  const focal35 = toNumber(e.FocalLengthIn35mmFilm);

  return {
    focal_length_mm: toNumber(e.FocalLength),
    focal_length_35mm: focal35 === 0 ? null : focal35,
    exposure_time_s: toNumber(e.ExposureTime),
    iso: iso === null ? null : Math.round(iso),
    f_number: toNumber(e.FNumber),
    white_balance: toWhiteBalance(e.WhiteBalance),
    image_width: firstPixels(e.ImageWidth, e.PixelXDimension, width),
    image_height: firstPixels(e.ImageLength, e.PixelYDimension, height),
  };
}
