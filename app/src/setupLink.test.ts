import { describe, expect, it } from "@jest/globals";
import { parseSetupLink } from "./setupLink";

const NOT_SETUP = "このQRコードは Sky Weather の設定用ではありません";

describe("parseSetupLink", () => {
  it("正しいリンクから、サーバーの URL と招待コードを取り出す", () => {
    expect(parseSetupLink("skyweather://setup?server=https://example.com&code=abc123")).toEqual({
      serverUrl: "https://example.com",
      inviteCode: "abc123",
    });
  });

  it("スキームが違えばエラー", () => {
    expect(parseSetupLink("https://setup?server=https://example.com&code=abc")).toEqual({
      error: NOT_SETUP,
    });
    expect(parseSetupLink("otherapp://setup?server=https://example.com&code=abc")).toEqual({
      error: NOT_SETUP,
    });
    expect(parseSetupLink("hello")).toEqual({ error: NOT_SETUP });
    expect(parseSetupLink("")).toEqual({ error: NOT_SETUP });
  });

  it("setup 以外ならエラー", () => {
    expect(parseSetupLink("skyweather://other?server=https://example.com&code=abc")).toEqual({
      error: NOT_SETUP,
    });
    expect(parseSetupLink("skyweather://?server=https://example.com&code=abc")).toEqual({
      error: NOT_SETUP,
    });
  });

  it("http:// のサーバーはエラー", () => {
    const result = parseSetupLink("skyweather://setup?server=http://example.com&code=abc");
    expect(result).toEqual({ error: "サーバーの URL は https:// で始まる必要があります" });
  });

  it("server がなければエラー", () => {
    expect("error" in parseSetupLink("skyweather://setup?code=abc")).toBe(true);
    expect("error" in parseSetupLink("skyweather://setup")).toBe(true);
  });

  it("code がなければエラー", () => {
    expect("error" in parseSetupLink("skyweather://setup?server=https://example.com")).toBe(true);
  });

  it("空白だけの code はエラー", () => {
    expect(parseSetupLink("skyweather://setup?server=https://example.com&code=%20%20")).toEqual({
      error: "招待コードが空です",
    });
    expect(parseSetupLink("skyweather://setup?server=https://example.com&code=")).toEqual({
      error: "招待コードが空です",
    });
  });

  it("長すぎる code はエラー（200 文字まではよい）", () => {
    const link = (code: string) => `skyweather://setup?server=https://example.com&code=${code}`;
    expect("error" in parseSetupLink(link("a".repeat(201)))).toBe(true);
    expect(parseSetupLink(link("a".repeat(200)))).toEqual({
      serverUrl: "https://example.com",
      inviteCode: "a".repeat(200),
    });
  });

  it("エンコードされた値を戻す", () => {
    const link =
      "skyweather://setup?server=https%3A%2F%2Fexample.com%3A8443&code=a%2Bb%2Fc%3D%26d%20e";
    expect(parseSetupLink(link)).toEqual({
      serverUrl: "https://example.com:8443",
      inviteCode: "a+b/c=&d e",
    });
  });

  it("壊れたエンコードはエラー", () => {
    expect("error" in parseSetupLink("skyweather://setup?server=https://example.com&code=%E0%A4%A")).toBe(
      true,
    );
  });

  it("server の末尾の / を取る", () => {
    expect(parseSetupLink("skyweather://setup?server=https%3A%2F%2Fexample.com%2F&code=abc")).toEqual(
      { serverUrl: "https://example.com", inviteCode: "abc" },
    );
  });

  it("code の前後の空白を取る", () => {
    expect(parseSetupLink("skyweather://setup?server=https://example.com&code=%20abc%20")).toEqual({
      serverUrl: "https://example.com",
      inviteCode: "abc",
    });
  });
});
