import { describe, expect, it } from "@jest/globals";
import { normalizeServerUrl, sanitizeStoredServerUrl } from "./settings";

const DEV = { allowHttp: true };
const RELEASE = { allowHttp: false };

describe("normalizeServerUrl", () => {
  it("前後の空白と末尾の / を取る", () => {
    expect(normalizeServerUrl("  http://192.168.0.5:8000/ \n", DEV)).toBe("http://192.168.0.5:8000");
    expect(normalizeServerUrl("https://example.com//", DEV)).toBe("https://example.com");
  });

  it("ホストの後ろにパス、?、# があれば null", () => {
    expect(normalizeServerUrl("https://example.com/api/", DEV)).toBeNull();
    expect(normalizeServerUrl("http://192.168.0.5:8000/v1", DEV)).toBeNull();
    expect(normalizeServerUrl("http://192.168.0.5:8000?x=1", DEV)).toBeNull();
    expect(normalizeServerUrl("http://192.168.0.5:8000/?x=1", DEV)).toBeNull();
    expect(normalizeServerUrl("http://192.168.0.5:8000#top", DEV)).toBeNull();
    expect(normalizeServerUrl("http://192.168.0.5:8000/#top", DEV)).toBeNull();
  });

  it("http:// か https:// で始まらなければ null", () => {
    expect(normalizeServerUrl("example.com", DEV)).toBeNull();
    expect(normalizeServerUrl("ftp://example.com", DEV)).toBeNull();
    expect(normalizeServerUrl("", DEV)).toBeNull();
    expect(normalizeServerUrl("http://", DEV)).toBeNull();
    expect(normalizeServerUrl("   ", DEV)).toBeNull();
  });
});

describe("normalizeServerUrl（allowHttp が false）", () => {
  it("https:// だけを受け付ける", () => {
    expect(normalizeServerUrl("https://example.com/", RELEASE)).toBe("https://example.com");
    expect(normalizeServerUrl("HTTPS://example.com", RELEASE)).toBe("HTTPS://example.com");
  });

  it("http:// は null", () => {
    expect(normalizeServerUrl("http://example.com", RELEASE)).toBeNull();
    expect(normalizeServerUrl("http://192.168.0.5:8000", RELEASE)).toBeNull();
    expect(normalizeServerUrl("HTTP://example.com", RELEASE)).toBeNull();
  });

  it("パス付きなどは https:// でも null", () => {
    expect(normalizeServerUrl("https://example.com/api", RELEASE)).toBeNull();
    expect(normalizeServerUrl("https://", RELEASE)).toBeNull();
  });
});

describe("sanitizeStoredServerUrl", () => {
  it("保存済みの値が未設定なら空文字列", () => {
    expect(sanitizeStoredServerUrl(null, true)).toBe("");
    expect(sanitizeStoredServerUrl(null, false)).toBe("");
  });

  it("リリースビルドでは http:// の URL を空文字列にする", () => {
    expect(sanitizeStoredServerUrl("http://192.168.0.5:8000", false)).toBe("");
    expect(sanitizeStoredServerUrl("https://example.com", false)).toBe("https://example.com");
  });

  it("開発中は http:// の URL も残す", () => {
    expect(sanitizeStoredServerUrl("http://192.168.0.5:8000", true)).toBe("http://192.168.0.5:8000");
  });

  it("形の合わない URL は空文字列", () => {
    expect(sanitizeStoredServerUrl("example.com", true)).toBe("");
  });
});
