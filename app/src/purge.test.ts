import { describe, expect, it } from "@jest/globals";
import { shouldPurgeSent } from "./purge";
import type { Status } from "./uploadQueue";

const SENT_AT = "2026-09-09T00:00:00.000Z";
const SENT_AT_MS = Date.parse(SENT_AT);
const DAY_MS = 24 * 60 * 60 * 1000;

function sent(extra: Partial<Status> = {}): Status {
  return { state: "sent", attempts: 0, sent_at: SENT_AT, ...extra };
}

describe("shouldPurgeSent", () => {
  it("30 日ちょうどで消す", () => {
    expect(shouldPurgeSent(sent(), SENT_AT_MS + 30 * DAY_MS)).toBe(true);
  });

  it("30 日に 1 ミリ秒足りなければ残す", () => {
    expect(shouldPurgeSent(sent(), SENT_AT_MS + 30 * DAY_MS - 1)).toBe(false);
  });

  it("30 日を過ぎていれば消す", () => {
    expect(shouldPurgeSent(sent(), SENT_AT_MS + 90 * DAY_MS)).toBe(true);
  });

  it("sent_at がない sent は残す", () => {
    expect(shouldPurgeSent(sent({ sent_at: undefined }), SENT_AT_MS + 90 * DAY_MS)).toBe(false);
  });

  it("sent_at が読めない sent は残す", () => {
    expect(shouldPurgeSent(sent({ sent_at: "きのう" }), SENT_AT_MS + 90 * DAY_MS)).toBe(false);
  });

  it("sent_at が未来でも消さない", () => {
    expect(shouldPurgeSent(sent(), SENT_AT_MS - DAY_MS)).toBe(false);
  });

  it("pending と rejected は古くても残す", () => {
    const now = SENT_AT_MS + 90 * DAY_MS;
    expect(shouldPurgeSent({ state: "pending", attempts: 1, sent_at: SENT_AT }, now)).toBe(false);
    expect(shouldPurgeSent({ state: "rejected", attempts: 1, sent_at: SENT_AT }, now)).toBe(false);
  });
});
