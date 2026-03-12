/**
 * Path breadcrumb navigation bar.
 */

import React from "react";

interface BreadcrumbProps {
  readonly path: string;
  readonly onNavigate: (path: string) => void;
}

export function Breadcrumb({ path }: BreadcrumbProps): React.ReactNode {
  const segments = path.split("/").filter(Boolean);
  const display = segments.length === 0 ? "/" : `/ ${segments.join(" / ")}`;

  return (
    <box height={1} width="100%">
      <text>{display}</text>
    </box>
  );
}
