// 送信済みの観測を端末から消す判定。仕様は docs/m5-app.md の 1.2 節。
import type { Status } from "./uploadQueue";

/** 送信済みのメタデータを端末に残す日数 */
export const PURGE_AFTER_DAYS = 30;
const PURGE_AFTER_MS = PURGE_AFTER_DAYS * 24 * 60 * 60 * 1000;

/**
 * 送信済み（sent）で、sent_at から 30 日以上たっていれば true。
 * sent_at がない・読めないものは、判断できないので false（残す）。
 */
export function shouldPurgeSent(status: Status, nowMs: number): boolean {
  if (status.state !== "sent") return false;
  if (typeof status.sent_at !== "string") return false;
  const sentAtMs = Date.parse(status.sent_at);
  if (Number.isNaN(sentAtMs)) return false;
  return nowMs - sentAtMs >= PURGE_AFTER_MS;
}
