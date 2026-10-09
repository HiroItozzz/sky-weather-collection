// 1 件の観測を fetch で PUT し、応答を送信の結果に分類する。仕様は docs/m3-app-capture.md の 4 節。

/** 端末では { uri, name, type }、テストとノードでは Blob。 */
export type ImagePart = { uri: string; name: string; type: string } | Blob;

export type Outcome =
  | { kind: "ok" }
  | { kind: "auth" }
  | { kind: "rejected"; error: string }
  | { kind: "retry"; error: string };

export interface Transport {
  put(
    baseUrl: string,
    token: string,
    id: string,
    metadataJson: string,
    image: ImagePart,
  ): Promise<Outcome>;
}

export const TIMEOUT_MS = 60_000;
const MAX_DETAIL_LENGTH = 200;

function shorten(text: string): string {
  return text.length > MAX_DETAIL_LENGTH ? text.slice(0, MAX_DETAIL_LENGTH) : text;
}

/** HTTP の状態コードと detail を、4.2 の表の分類にする。 */
export function classifyResponse(status: number, detail: string): Outcome {
  if (status === 200 || status === 201) return { kind: "ok" };
  if (status === 401) return { kind: "auth" };
  const error = detail === "" ? `HTTP ${status}` : `HTTP ${status}: ${shorten(detail)}`;
  if (status === 408 || status === 429 || status >= 500) return { kind: "retry", error };
  if (status >= 400 && status < 500) return { kind: "rejected", error };
  // 2xx（200 と 201 以外）や 3xx は想定していないので、成功とは見なさず送り直す
  return { kind: "retry", error };
}

/** 応答の本文から detail を取り出す。JSON でなければ本文そのまま。 */
function extractDetail(body: string): string {
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      return shorten(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
  } catch {
    // JSON でなければ本文をそのまま使う
  }
  return shorten(body.trim());
}

export function createFetchTransport(fetchImpl: typeof fetch = fetch): Transport {
  return {
    async put(baseUrl, token, id, metadataJson, image) {
      const form = new FormData();
      form.append("metadata", metadataJson);
      if (typeof Blob !== "undefined" && image instanceof Blob) {
        form.append("image", image, "image.jpg");
      } else {
        // React Native の FormData は { uri, name, type } を受け取れるが、型定義は Blob だけなので付け替える
        form.append("image", image as unknown as Blob);
      }

      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, TIMEOUT_MS);

      try {
        const response = await fetchImpl(`${baseUrl}/v1/observations/${encodeURIComponent(id)}`, {
          method: "PUT",
          headers: { Authorization: `Bearer ${token}` },
          body: form,
          signal: controller.signal,
        });
        const body = await response.text().catch(() => "");
        return classifyResponse(response.status, extractDetail(body));
      } catch (e) {
        if (timedOut) {
          return { kind: "retry", error: "60秒たっても応答がありませんでした" };
        }
        const message = e instanceof Error ? e.message : String(e);
        return { kind: "retry", error: shorten(`通信に失敗しました: ${message}`) };
      } finally {
        clearTimeout(timer);
      }
    },
  };
}
