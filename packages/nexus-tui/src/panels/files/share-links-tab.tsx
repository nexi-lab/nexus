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

export function ShareLinksTab(props: ShareLinksTabProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading share links..."
          : props.links.length === 0
            ? "No share links. Press 'n' to create one."
            : `${props.links.length} share links`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {props.links.map((link, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const statusBadge = link.status === "active" ? "\u25CF" : link.status === "revoked" ? "\u00D7" : "\u25CB";
          return (
            <box height={1} width="100%">
              <text>{`${prefix}${statusBadge} ${link.path}  ${link.permission_level}  ${link.access_count} views  ${link.expires_at ?? "no expiry"}`}</text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
