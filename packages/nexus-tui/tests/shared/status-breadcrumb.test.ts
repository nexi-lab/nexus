import { describe, expect, it } from "bun:test";
import { deriveStatusBreadcrumb } from "../../src/shared/status-breadcrumb.js";

describe("deriveStatusBreadcrumb", () => {
  it("renders panel and sub-tab for store-backed panels", () => {
    expect(
      deriveStatusBreadcrumb({
        connectionStatus: "connected",
        activePanel: "access",
        accessTab: "credentials",
      }),
    ).toBe("Access > Credentials");
  });

  it("renders panel only for panels without externally readable sub-tabs", () => {
    expect(
      deriveStatusBreadcrumb({
        connectionStatus: "connected",
        activePanel: "files",
      }),
    ).toBe("Files");
  });

  it("renders panel and sub-tab for infrastructure tabs", () => {
    expect(
      deriveStatusBreadcrumb({
        connectionStatus: "connected",
        activePanel: "infrastructure",
        eventsTab: "subscriptions",
      }),
    ).toBe("Events > Subscriptions");
  });

  it("returns null on pre-connection screens", () => {
    expect(
      deriveStatusBreadcrumb({
        connectionStatus: "connecting",
        activePanel: "files",
      }),
    ).toBeNull();
  });

  it("returns null when there is no active panel", () => {
    expect(
      deriveStatusBreadcrumb({
        connectionStatus: "connected",
        activePanel: null,
      }),
    ).toBeNull();
  });

  it("uses full breadcrumb labels rather than tab bar abbreviations", () => {
    expect(
      deriveStatusBreadcrumb({
        connectionStatus: "connected",
        activePanel: "zones",
        zoneTab: "bricks",
      }),
    ).toBe("Zones > Bricks");
  });
});
