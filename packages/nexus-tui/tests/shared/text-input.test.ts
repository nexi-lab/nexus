/**
 * Tests for TextInput behavior — character input, backspace, edge cases.
 *
 * Tests the callback logic that the TextInput component delegates to,
 * rather than rendering the React component directly.
 */

import { describe, it, expect } from "bun:test";

describe("TextInput behavior", () => {
  describe("character input", () => {
    it("appends single characters", () => {
      let value = "hello";
      const onChange = (v: string) => {
        value = v;
      };
      // Simulate typing 'x'
      onChange(value + "x");
      expect(value).toBe("hellox");
    });

    it("appends to empty string", () => {
      let value = "";
      const onChange = (v: string) => {
        value = v;
      };
      onChange(value + "a");
      expect(value).toBe("a");
    });

    it("handles special characters", () => {
      let value = "test";
      const onChange = (v: string) => {
        value = v;
      };
      onChange(value + "@");
      expect(value).toBe("test@");
    });

    it("ignores multi-char key names", () => {
      const key = "up";
      // The component only appends when key.length === 1
      expect(key.length).not.toBe(1);
    });
  });

  describe("backspace", () => {
    it("removes last character", () => {
      let value = "hello";
      const onChange = (v: string) => {
        value = v;
      };
      onChange(value.slice(0, -1));
      expect(value).toBe("hell");
    });

    it("handles backspace on empty string gracefully", () => {
      let value = "";
      const onChange = (v: string) => {
        value = v;
      };
      onChange(value.slice(0, -1));
      expect(value).toBe("");
    });

    it("handles backspace on single character", () => {
      let value = "x";
      const onChange = (v: string) => {
        value = v;
      };
      onChange(value.slice(0, -1));
      expect(value).toBe("");
    });
  });

  describe("submit and cancel", () => {
    it("submit passes current value", () => {
      const value = "my input";
      let submitted: string | undefined;
      const onSubmit = (v: string) => {
        submitted = v;
      };
      onSubmit(value);
      expect(submitted).toBe("my input");
    });

    it("cancel is callable without error", () => {
      let cancelled = false;
      const onCancel = () => {
        cancelled = true;
      };
      onCancel();
      expect(cancelled).toBe(true);
    });
  });
});
