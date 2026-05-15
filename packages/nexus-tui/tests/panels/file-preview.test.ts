/**
 * Tests for the extensionToLanguage pure function.
 *
 * Covers all 25 known extension mappings, multi-alias cases (e.g. .h → "c"),
 * and the unknown-extension fallback.
 */

import { describe, it, expect } from "bun:test";
import { extensionToLanguage } from "../../src/panels/files/file-preview.js";

describe("extensionToLanguage", () => {
  describe("TypeScript / JavaScript", () => {
    it("ts → typescript", () => expect(extensionToLanguage("ts")).toBe("typescript"));
    it("tsx → tsx",       () => expect(extensionToLanguage("tsx")).toBe("tsx"));
    it("js → javascript", () => expect(extensionToLanguage("js")).toBe("javascript"));
    it("jsx → jsx",       () => expect(extensionToLanguage("jsx")).toBe("jsx"));
  });

  describe("Systems languages", () => {
    it("rs → rust", () => expect(extensionToLanguage("rs")).toBe("rust"));
    it("go → go",   () => expect(extensionToLanguage("go")).toBe("go"));
    it("c → c",     () => expect(extensionToLanguage("c")).toBe("c"));
    it("cpp → cpp", () => expect(extensionToLanguage("cpp")).toBe("cpp"));
  });

  describe("C header aliases", () => {
    it("h → c   (C header alias)",   () => expect(extensionToLanguage("h")).toBe("c"));
    it("hpp → cpp (C++ header alias)", () => expect(extensionToLanguage("hpp")).toBe("cpp"));
  });

  describe("Scripting languages", () => {
    it("py → python", () => expect(extensionToLanguage("py")).toBe("python"));
    it("rb → ruby",   () => expect(extensionToLanguage("rb")).toBe("ruby"));
  });

  describe("JVM", () => {
    it("java → java", () => expect(extensionToLanguage("java")).toBe("java"));
  });

  describe("Data / config formats", () => {
    it("json → json",   () => expect(extensionToLanguage("json")).toBe("json"));
    it("yaml → yaml",   () => expect(extensionToLanguage("yaml")).toBe("yaml"));
    it("yml → yaml",    () => expect(extensionToLanguage("yml")).toBe("yaml"));
    it("toml → toml",   () => expect(extensionToLanguage("toml")).toBe("toml"));
    it("xml → xml",     () => expect(extensionToLanguage("xml")).toBe("xml"));
    it("proto → protobuf", () => expect(extensionToLanguage("proto")).toBe("protobuf"));
  });

  describe("Markup", () => {
    it("md → markdown", () => expect(extensionToLanguage("md")).toBe("markdown"));
    it("html → html",   () => expect(extensionToLanguage("html")).toBe("html"));
    it("css → css",     () => expect(extensionToLanguage("css")).toBe("css"));
  });

  describe("Shell / SQL", () => {
    it("sh → bash",   () => expect(extensionToLanguage("sh")).toBe("bash"));
    it("bash → bash", () => expect(extensionToLanguage("bash")).toBe("bash"));
    it("zsh → bash",  () => expect(extensionToLanguage("zsh")).toBe("bash"));
    it("sql → sql",   () => expect(extensionToLanguage("sql")).toBe("sql"));
  });

  describe("Unknown extensions → text fallback", () => {
    it("unknown ext → text", () => expect(extensionToLanguage("xyz")).toBe("text"));
    it("empty string → text", () => expect(extensionToLanguage("")).toBe("text"));
    it("exe → text",          () => expect(extensionToLanguage("exe")).toBe("text"));
    it("pdf → text",          () => expect(extensionToLanguage("pdf")).toBe("text"));
  });
});
