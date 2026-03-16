/**
 * Consistent date/time formatting across all panels.
 *
 * Strategy:
 * - Relative time for < 24 hours ("2h ago", "5m ago")
 * - Absolute for >= 24 hours ("Mar 15, 14:30")
 * - Consistent 19-char max width for absolute timestamps
 *
 * @see Issue #3066, Phase E8
 */

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
] as const;

/**
 * Format a timestamp for display in the TUI.
 *
 * @param input - Unix epoch ms, ISO string, or Date object
 * @param now - Current time in ms (injectable for testing). Defaults to Date.now()
 * @returns Formatted time string, max 19 chars
 */
export function formatTimestamp(input: number | string | Date, now?: number): string {
  const date = input instanceof Date ? input : new Date(input);
  const ts = date.getTime();

  if (Number.isNaN(ts)) return "—";

  const currentTime = now ?? Date.now();
  const delta = currentTime - ts;

  // Future timestamps: show absolute
  if (delta < 0) {
    return formatAbsolute(date);
  }

  // < 1 minute
  if (delta < MINUTE) {
    const secs = Math.floor(delta / SECOND);
    return secs <= 1 ? "just now" : `${secs}s ago`;
  }

  // < 1 hour
  if (delta < HOUR) {
    const mins = Math.floor(delta / MINUTE);
    return `${mins}m ago`;
  }

  // < 24 hours
  if (delta < DAY) {
    const hours = Math.floor(delta / HOUR);
    return `${hours}h ago`;
  }

  return formatAbsolute(date);
}

function formatAbsolute(date: Date): string {
  const month = MONTHS[date.getMonth()]!;
  const day = date.getDate();
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${month} ${String(day).padStart(2, " ")}, ${hours}:${minutes}`;
}
