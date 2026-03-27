import { describe, it, expect } from "bun:test";
import {
  statusColor,
  connectionColor,
  brickStateColor,
  transactionStatusColor,
  httpStatusColor,
  agentPhaseColor,
  focusColor,
  palette,
} from "../../src/shared/theme.js";

describe("theme", () => {
  describe("statusColor", () => {
    it("has all required semantic keys", () => {
      expect(statusColor.healthy).toBe("green");
      expect(statusColor.success).toBe("green");
      expect(statusColor.warning).toBe("yellow");
      expect(statusColor.error).toBe("red");
      expect(statusColor.info).toBe("cyan");
      expect(statusColor.focus).toBe("yellow");
      expect(statusColor.dim).toBe("gray");
      expect(statusColor.identity).toBe("magenta");
      expect(statusColor.reference).toBe("blue");
    });

    it("success is semantically distinct from but same color as healthy", () => {
      expect(statusColor.success).toBe(statusColor.healthy);
    });
  });

  describe("connectionColor", () => {
    it("maps all connection statuses", () => {
      expect(connectionColor["connected"]).toBe("green");
      expect(connectionColor["connecting"]).toBe("yellow");
      expect(connectionColor["disconnected"]).toBe("gray");
      expect(connectionColor["error"]).toBe("red");
    });
  });

  describe("brickStateColor", () => {
    it("maps all brick states", () => {
      expect(brickStateColor["active"]).toBe("green");
      expect(brickStateColor["registered"]).toBe("cyan");
      expect(brickStateColor["starting"]).toBe("yellow");
      expect(brickStateColor["stopping"]).toBe("yellow");
      expect(brickStateColor["unmounted"]).toBe("gray");
      expect(brickStateColor["unregistered"]).toBe("gray");
      expect(brickStateColor["failed"]).toBe("red");
    });
  });

  describe("transactionStatusColor", () => {
    it("maps all transaction statuses", () => {
      expect(transactionStatusColor["active"]).toBe("yellow");
      expect(transactionStatusColor["committed"]).toBe("green");
      expect(transactionStatusColor["rolled_back"]).toBe("red");
      expect(transactionStatusColor["expired"]).toBe("gray");
    });
  });

  describe("httpStatusColor", () => {
    it("returns green for 2xx", () => {
      expect(httpStatusColor(200)).toBe("green");
      expect(httpStatusColor(201)).toBe("green");
      expect(httpStatusColor(299)).toBe("green");
    });

    it("returns yellow for 4xx", () => {
      expect(httpStatusColor(400)).toBe("yellow");
      expect(httpStatusColor(404)).toBe("yellow");
      expect(httpStatusColor(499)).toBe("yellow");
    });

    it("returns red for 5xx", () => {
      expect(httpStatusColor(500)).toBe("red");
      expect(httpStatusColor(503)).toBe("red");
    });

    it("returns dim for other codes", () => {
      expect(httpStatusColor(100)).toBe("gray");
      expect(httpStatusColor(301)).toBe("gray");
    });
  });

  describe("agentPhaseColor", () => {
    it("maps all agent phases", () => {
      expect(agentPhaseColor["ready"]).toBe("green");
      expect(agentPhaseColor["active"]).toBe("green");
      expect(agentPhaseColor["warming"]).toBe("yellow");
      expect(agentPhaseColor["evicting"]).toBe("yellow");
      expect(agentPhaseColor["evicted"]).toBe("gray");
      expect(agentPhaseColor["failed"]).toBe("red");
    });
  });

  describe("focusColor", () => {
    it("has active and inactive border colors", () => {
      expect(focusColor.activeBorder).toBe("yellow");
      expect(focusColor.inactiveBorder).toBe("gray");
    });

    it("has key highlight colors", () => {
      expect(focusColor.actionKey).toBe("yellow");
      expect(focusColor.navKey).toBe("gray");
    });

    it("derives from statusColor.focus", () => {
      expect(focusColor.activeBorder).toBe(statusColor.focus);
      expect(focusColor.selected).toBe(statusColor.focus);
      expect(focusColor.actionKey).toBe(statusColor.focus);
    });
  });

  describe("palette", () => {
    it("has all Industrial Warmth hex values", () => {
      expect(palette.accent).toBe("#D4A017");
      expect(palette.accentDim).toBe("#B8890F");
      expect(palette.success).toBe("#34D399");
      expect(palette.error).toBe("#F87171");
      expect(palette.errorDim).toBe("#FCA5A5");
      expect(palette.warning).toBe("#FBBF24");
      expect(palette.muted).toBe("#8B8B92");
      expect(palette.faint).toBe("#2A2A2E");
      expect(palette.bright).toBe("#EDEDEF");
      expect(palette.title).toBe("#EDEDEF");
    });
  });
});
