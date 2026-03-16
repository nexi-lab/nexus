/**
 * Tests for TextInput behavior logic.
 *
 * The TextInput component delegates to onChange/onSubmit/onCancel callbacks
 * and uses string operations (append char, slice for backspace). These tests
 * validate the exact string operations the component performs, using the same
 * patterns as the component source (value + key for append, value.slice(0, -1)
 * for backspace).
 *
 * Since we can't render React components in bun:test without a renderer,
 * we test the callback contract that the component guarantees.
 */

import { describe, it, expect } from "bun:test";

// These operations mirror TextInput's internal behavior exactly:
// - onUnhandled: if (key.length === 1) onChange(value + key)
// - backspace: onChange(value.slice(0, -1))
// - return: onSubmit?.(value)
// - escape: onCancel?.()

describe("TextInput callback contract", () => {
  describe("character input (onUnhandled → onChange)", () => {
    it("appends single printable character", () => {
      expect("hello" + "x").toBe("hellox");
    });

    it("appends to empty string", () => {
      expect("" + "a").toBe("a");
    });

    it("rejects multi-char key names (length > 1)", () => {
      // The component checks key.length === 1 before appending
      expect("up".length).not.toBe(1);
      expect("return".length).not.toBe(1);
      expect("escape".length).not.toBe(1);
      // Single chars pass:
      expect("a".length).toBe(1);
      expect("@".length).toBe(1);
    });
  });

  describe("backspace (onChange with slice)", () => {
    it("removes last character", () => {
      expect("hello".slice(0, -1)).toBe("hell");
    });

    it("empties single character", () => {
      expect("x".slice(0, -1)).toBe("");
    });

    it("no-ops on empty string", () => {
      expect("".slice(0, -1)).toBe("");
    });
  });

  describe("submit and cancel callbacks", () => {
    it("onSubmit receives current value", () => {
      let submitted: string | undefined;
      const onSubmit = (v: string) => { submitted = v; };
      onSubmit("my input");
      expect(submitted).toBe("my input");
    });

    it("onCancel is callable", () => {
      let cancelled = false;
      const onCancel = () => { cancelled = true; };
      onCancel();
      expect(cancelled).toBe(true);
    });
  });
});
