import type { ConnectionStatus } from "../stores/global-store.js";

export type AnnouncementLevel = "info" | "success" | "error";

export function normalizeAnnouncementMessage(message: string): string {
  return message.replace(/\s+/g, " ").trim();
}

export function formatPanelAnnouncement(label: string): string {
  return normalizeAnnouncementMessage(`Panel ${label}`);
}

export function formatConnectionAnnouncement(
  status: ConnectionStatus,
  error?: string | null,
): string {
  switch (status) {
    case "connected":
      return "Connected";
    case "connecting":
      return "Connecting";
    case "disconnected":
      return "Disconnected";
    case "error":
      return normalizeAnnouncementMessage(`Connection error${error ? `: ${error}` : ""}`);
  }
}

export function formatDirectoryAnnouncement(path: string, count: number): string {
  const noun = count === 1 ? "item" : "items";
  return normalizeAnnouncementMessage(`${count} ${noun} in ${path}`);
}

export function formatSelectionAnnouncement(name: string, isDirectory: boolean): string {
  return normalizeAnnouncementMessage(`Selected ${isDirectory ? "folder" : "file"} ${name}`);
}

export function formatErrorAnnouncement(message: string): string {
  return normalizeAnnouncementMessage(`Error: ${message}`);
}

export function formatSuccessAnnouncement(message: string): string {
  return normalizeAnnouncementMessage(message);
}
