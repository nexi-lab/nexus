import type { JSX } from "solid-js";
/**
 * Share Links tab: displays a scrollable list of share links with
 * columns for path, permission, status, access count, and expiry.
 */

import type { ShareLink } from "../../stores/share-link-store.js";

interface ShareLinksTabProps {
  readonly links: readonly ShareLink[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function ShareLinksTab({ links, selectedIndex, loading }: ShareLinksTabProps): JSX.Element {
  if (loading) return <text>Loading share links...</text>;
  if (links.length === 0) return <text>{"No share links. Press 'n' to create one."}</text>;

  return (
    <scrollbox height="100%" width="100%">
      {links.map((link, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const statusBadge = link.status === "active" ? "\u25CF" : link.status === "revoked" ? "\u00D7" : "\u25CB";
        return (
          <box height={1} width="100%">
            <text>{`${prefix}${statusBadge} ${link.path}  ${link.permission_level}  ${link.access_count} views  ${link.expires_at ?? "no expiry"}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
