// タイムラインと記録の前回の取得結果の保存（Paths.document/cache/）。仕様は docs/m5-app.md の 6.5 節。
// 位置は含まれない。
import { Directory, File, Paths } from "expo-file-system";
import { parseObservationPage, parseStats, type ObservationPage, type Stats } from "./api";

function cacheFile(name: string): File {
  return new File(new Directory(Paths.document, "cache"), name);
}

function save(name: string, data: unknown): void {
  try {
    const file = cacheFile(name);
    file.parentDirectory.create({ intermediates: true, idempotent: true });
    file.write(JSON.stringify(data));
  } catch (e) {
    console.warn("取得した内容の保存に失敗しました", e);
  }
}

/** なければ、または読めなければ、形が合わなければ null。 */
async function load<T>(name: string, parse: (value: unknown) => T | null): Promise<T | null> {
  try {
    const file = cacheFile(name);
    if (!file.exists) return null;
    return parse(JSON.parse(await file.text()));
  } catch {
    return null;
  }
}

export function saveTimelineCache(page: ObservationPage): void {
  save("timeline.json", page);
}

export function loadTimelineCache(): Promise<ObservationPage | null> {
  return load("timeline.json", parseObservationPage);
}

export function saveStatsCache(stats: Stats): void {
  save("stats.json", stats);
}

export function loadStatsCache(): Promise<Stats | null> {
  return load("stats.json", parseStats);
}
