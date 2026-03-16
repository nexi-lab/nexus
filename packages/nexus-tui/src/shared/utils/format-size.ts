/**
 * Consistent byte-size formatting across all panels.
 *
 * Output: "0 B", "1.5 KB", "2.3 MB", "1.1 GB"
 * - Space before unit, standard suffixes
 * - Supports up to GB
 *
 * @see Issue #3102, Decision 5A
 */

const KB = 1024;
const MB = KB * 1024;
const GB = MB * 1024;

export function formatSize(bytes: number): string {
  if (bytes < KB) return `${bytes} B`;
  if (bytes < MB) return `${(bytes / KB).toFixed(1)} KB`;
  if (bytes < GB) return `${(bytes / MB).toFixed(1)} MB`;
  return `${(bytes / GB).toFixed(1)} GB`;
}
