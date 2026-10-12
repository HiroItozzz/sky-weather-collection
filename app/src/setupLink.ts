// 設定リンク（skyweather://setup?server=...&code=...）の読み取り。仕様は docs/m6-app-update-invite.md の 2 節。

export type SetupLink = { serverUrl: string; inviteCode: string };

const SCHEME_PREFIX = "skyweather://";
const MAX_CODE_LENGTH = 200;

const NOT_SETUP_LINK = "このQRコードは Sky Weather の設定用ではありません";
const URL_MUST_BE_HTTPS = "サーバーの URL は https:// で始まる必要があります";

/** クエリの文字列を、キーごとの値（URL デコード済み）にする。同じキーが複数あれば最初のものを使う。 */
function parseQuery(query: string): Map<string, string> | null {
  const values = new Map<string, string>();
  for (const pair of query.split("&")) {
    if (pair === "") continue;
    const separator = pair.indexOf("=");
    const rawKey = separator === -1 ? pair : pair.slice(0, separator);
    const rawValue = separator === -1 ? "" : pair.slice(separator + 1);
    try {
      const key = decodeURIComponent(rawKey);
      if (!values.has(key)) values.set(key, decodeURIComponent(rawValue));
    } catch {
      return null; // %XX の形が壊れている
    }
  }
  return values;
}

/**
 * 設定リンクから、サーバーの URL と招待コードを取り出す。読めなければ、利用者に見せる文言の error を返す。
 * URL の解析器（searchParams など）は React Native では一部しか使えないので、クエリは自分で分ける。
 */
export function parseSetupLink(text: string): SetupLink | { error: string } {
  const trimmed = text.trim();
  if (!trimmed.toLowerCase().startsWith(SCHEME_PREFIX)) return { error: NOT_SETUP_LINK };

  const rest = trimmed.slice(SCHEME_PREFIX.length).split("#")[0];
  const questionMark = rest.indexOf("?");
  const host = questionMark === -1 ? rest : rest.slice(0, questionMark);
  if (host.toLowerCase() !== "setup") return { error: NOT_SETUP_LINK };

  const query = parseQuery(questionMark === -1 ? "" : rest.slice(questionMark + 1));
  if (query === null) return { error: NOT_SETUP_LINK };
  const server = query.get("server");
  const code = query.get("code");
  if (server === undefined || code === undefined) {
    return { error: "このQRコードには、サーバーの URL と招待コードの両方が入っていません" };
  }

  const serverUrl = server.trim().replace(/\/+$/, "");
  if (!/^https:\/\//i.test(serverUrl)) return { error: URL_MUST_BE_HTTPS };
  try {
    new URL(serverUrl);
  } catch {
    return { error: "サーバーの URL を読み取れませんでした" };
  }

  const inviteCode = code.trim();
  if (inviteCode === "") return { error: "招待コードが空です" };
  if (inviteCode.length > MAX_CODE_LENGTH) {
    return { error: `招待コードが長すぎます（${MAX_CODE_LENGTH} 文字まで）` };
  }
  return { serverUrl, inviteCode };
}
