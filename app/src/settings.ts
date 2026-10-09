// サーバーの URL と招待コードの保存。仕様は docs/m3-app-capture.md の 6 節。
import * as SecureStore from "expo-secure-store";

export type Settings = { serverUrl: string; inviteCode: string };

const KEY_SERVER_URL = "serverUrl";
const KEY_INVITE_CODE = "inviteCode";

/** 前後の空白と末尾の `/` を取る。`http://` か `https://` で始まらなければ null。 */
export function normalizeServerUrl(input: string): string | null {
  const trimmed = input.trim().replace(/\/+$/, "");
  if (!/^https?:\/\/./i.test(trimmed)) return null;
  return trimmed;
}

/** 未設定の項目は空文字列で返す。 */
export async function loadSettings(): Promise<Settings> {
  const [serverUrl, inviteCode] = await Promise.all([
    SecureStore.getItemAsync(KEY_SERVER_URL),
    SecureStore.getItemAsync(KEY_INVITE_CODE),
  ]);
  return { serverUrl: serverUrl ?? "", inviteCode: inviteCode ?? "" };
}

export async function saveSettings(settings: Settings): Promise<void> {
  await SecureStore.setItemAsync(KEY_SERVER_URL, settings.serverUrl);
  await SecureStore.setItemAsync(KEY_INVITE_CODE, settings.inviteCode);
}
