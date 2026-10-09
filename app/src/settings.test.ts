import { describe, expect, it } from "@jest/globals";
import { normalizeServerUrl } from "./settings";

describe("normalizeServerUrl", () => {
  it("前後の空白と末尾の / を取る", () => {
    expect(normalizeServerUrl("  http://192.168.0.5:8000/ \n")).toBe("http://192.168.0.5:8000");
    expect(normalizeServerUrl("https://example.com//")).toBe("https://example.com");
  });

  it("パスは残す", () => {
    expect(normalizeServerUrl("https://example.com/api/")).toBe("https://example.com/api");
  });

  it("http:// か https:// で始まらなければ null", () => {
    expect(normalizeServerUrl("example.com")).toBeNull();
    expect(normalizeServerUrl("ftp://example.com")).toBeNull();
    expect(normalizeServerUrl("")).toBeNull();
    expect(normalizeServerUrl("http://")).toBeNull();
    expect(normalizeServerUrl("   ")).toBeNull();
  });
});
