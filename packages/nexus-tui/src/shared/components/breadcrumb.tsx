/**
 * Path breadcrumb navigation bar.
 */

interface BreadcrumbProps {
  readonly path: string;
  readonly onNavigate: (path: string) => void;
}

export function Breadcrumb(props: BreadcrumbProps) {
  const segments = props.path.split("/").filter(Boolean);
  const display = segments.length === 0 ? "/" : `/ ${segments.join(" / ")}`;

  return (
    <box height={1} width="100%">
      <text>{display}</text>
    </box>
  );
}
