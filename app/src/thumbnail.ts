// 送信済みの画像のサムネイル。仕様は docs/m5-app.md の 3 節。
// 観測のディレクトリとは別の Paths.document/thumbnails/ に置き、ディレクトリを消しても残す。
import { Directory, File, Paths } from "expo-file-system";
import { ImageManipulator, SaveFormat } from "expo-image-manipulator";

/** サムネイルの長い辺（ピクセル） */
export const THUMBNAIL_LONG_EDGE = 320;
const THUMBNAIL_QUALITY = 0.7;

function thumbnailsDir(): Directory {
  return new Directory(Paths.document, "thumbnails");
}

function thumbnailFile(id: string): File {
  return new File(thumbnailsDir(), `${id}.jpg`);
}

/** サムネイルがあればその uri、なければ null。 */
export function thumbnailUri(id: string): string | null {
  const file = thumbnailFile(id);
  return file.exists ? file.uri : null;
}

/**
 * srcUri の画像を、長い辺が 320px の JPEG（品質 0.7）にして destFile に置く。
 * すでに 320px 以下なら拡大しない。失敗したら例外を投げる。
 */
export async function makeThumbnail(srcUri: string, destFile: File): Promise<void> {
  const source = await ImageManipulator.manipulate(srcUri).renderAsync();
  const { width, height } = source;
  const context = ImageManipulator.manipulate(source);
  if (Math.max(width, height) > THUMBNAIL_LONG_EDGE) {
    context.resize(width >= height ? { width: THUMBNAIL_LONG_EDGE } : { height: THUMBNAIL_LONG_EDGE });
  }
  const rendered = await context.renderAsync();
  const result = await rendered.saveAsync({ format: SaveFormat.JPEG, compress: THUMBNAIL_QUALITY });

  const dir = destFile.parentDirectory;
  dir.create({ intermediates: true, idempotent: true });
  await new File(result.uri).move(destFile, { overwrite: true });
}

/** 観測 id のサムネイルを作る（Paths.document/thumbnails/{id}.jpg）。 */
export async function makeThumbnailFor(id: string, srcUri: string): Promise<void> {
  await makeThumbnail(srcUri, thumbnailFile(id));
}
