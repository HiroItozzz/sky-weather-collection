// サーバーの URL と招待コードの保存。仕様は docs/m3-app-capture.md の 6 節。
import * as SecureStore from "expo-secure-store";

export type Settings = { serverUrl: string; inviteCode: string };

const KEY_SERVER_URL = "serverUrl";
const KEY_INVITE_CODE = "inviteCode";

/**
 * 前後の空白と末尾の `/` を取る。`http://` か `https://` で始まらなければ null。
 * `allowHttp` が false のときは `https://` だけを受け付ける。
 * ホストの後ろにパス・`?`・`#` があっても null（`/v1/v1/...` のような URL になるのを防ぐ）。
 */
export function normalizeServerUrl(input: string, options: { allowHttp: boolean }): string | null {
  const trimmed = input.trim().replace(/\/+$/, "");
  const pattern = options.allowHttp ? /^https?:\/\/[^/?#]+$/i : /^https:\/\/[^/?#]+$/i;
  if (!pattern.test(trimmed)) return null;
  return trimmed;
}

/** 保存してあった URL を読み直すときの検査。通らないものは未設定（空文字列）にする。 */
export function sanitizeStoredServerUrl(stored: string | null, allowHttp: boolean): string {
  if (stored === null) return "";
  return normalizeServerUrl(stored, { allowHttp }) ?? "";
}

/** 未設定の項目は空文字列で返す。リリースビルドでは、保存済みの http:// の URL も未設定として扱う。 */
export async function loadSettings(): Promise<Settings> {
  const [serverUrl, inviteCode] = await Promise.all([
    SecureStore.getItemAsync(KEY_SERVER_URL),
    SecureStore.getItemAsync(KEY_INVITE_CODE),
  ]);
  return { serverUrl: sanitizeStoredServerUrl(serverUrl, __DEV__), inviteCode: inviteCode ?? "" };
}

export async function saveSettings(settings: Settings): Promise<void> {
  await SecureStore.setItemAsync(KEY_SERVER_URL, settings.serverUrl);
  await SecureStore.setItemAsync(KEY_INVITE_CODE, settings.inviteCode);
}
