// 1 件の観測を fetch で PUT し、応答を送信の結果に分類する。仕様は docs/m3-app-capture.md の 4 節。

/** 端末では { uri, name, type }、テストとノードでは Blob。 */
export type ImagePart = { uri: string; name: string; type: string } | Blob;

export type Outcome =
  | { kind: "ok" }
  | { kind: "blocked"; reason: "auth" | "config"; error: string }
  | { kind: "rejected"; error: string }
  | { kind: "retry"; error: string; stopPass: boolean };

export interface Transport {
  put(
    baseUrl: string,
    token: string,
    id: string,
    metadataJson: string,
    image: ImagePart,
    imageSize: number,
  ): Promise<Outcome>;
}

const MIN_TIMEOUT_MS = 60_000;
/** 遅い回線を想定した送信速度（バイト/秒）。打ち切りの時間の見積もりに使う。 */
const ASSUMED_BYTES_PER_SECOND = 50 * 1024;
const MAX_DETAIL_LENGTH = 200;
const CLOCK_AHEAD_MESSAGE = "captured_at がサーバー時刻より";

/** 打ち切りの時間（ミリ秒）。max(60 秒, 画像の大きさ ÷ 50KB/秒)。 */
export function timeoutMs(imageSize: number): number {
  return Math.max(MIN_TIMEOUT_MS, (imageSize / ASSUMED_BYTES_PER_SECOND) * 1000);
}

function shorten(text: string): string {
  return text.length > MAX_DETAIL_LENGTH ? text.slice(0, MAX_DETAIL_LENGTH) : text;
}

/**
 * HTTP の状態コードと detail を、4.2 の表の分類にする。
 * detail は短くする前の本文を渡す（判定に使うため。error に入れるときにここで短くする）。
 */
export function classifyResponse(status: number, detail: string): Outcome {
  if (status === 200 || status === 201) return { kind: "ok" };
  const error = detail === "" ? `HTTP ${status}` : `HTTP ${status}: ${shorten(detail)}`;
  if (status === 401) return { kind: "blocked", reason: "auth", error };
  if (status === 403 || status === 404 || status === 405) {
    return { kind: "blocked", reason: "config", error };
  }
  if (status === 422 && detail.includes(CLOCK_AHEAD_MESSAGE)) {
    return { kind: "retry", error, stopPass: false };
  }
  if (status === 408 || status === 429 || status === 503) {
    return { kind: "retry", error, stopPass: true };
  }
  if (status >= 500) return { kind: "retry", error, stopPass: false };
  if (status >= 400 && status < 500) return { kind: "rejected", error };
  // 2xx（200 と 201 以外）や 3xx は想定していないので、成功とは見なさず送り直す
  return { kind: "retry", error, stopPass: false };
}

/** 応答の本文から detail を取り出す。JSON でなければ本文そのまま。短くはしない。 */
function extractDetail(body: string): string {
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // JSON でなければ本文をそのまま使う
  }
  return body.trim();
}

export function createFetchTransport(fetchImpl: typeof fetch = fetch): Transport {
  return {
    async put(baseUrl, token, id, metadataJson, image, imageSize) {
      const form = new FormData();
      form.append("metadata", metadataJson);
      if (typeof Blob !== "undefined" && image instanceof Blob) {
        form.append("image", image, "image.jpg");
      } else {
        // React Native の FormData は { uri, name, type } を受け取れるが、型定義は Blob だけなので付け替える
        form.append("image", image as unknown as Blob);
      }

      const limitMs = timeoutMs(imageSize);
      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, limitMs);

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
          const seconds = Math.ceil(limitMs / 1000);
          return { kind: "retry", error: `${seconds}秒たっても応答がありませんでした`, stopPass: true };
        }
        const message = e instanceof Error ? e.message : String(e);
        return { kind: "retry", error: shorten(`通信に失敗しました: ${message}`), stopPass: true };
      } finally {
        clearTimeout(timer);
      }
    },
  };
}
