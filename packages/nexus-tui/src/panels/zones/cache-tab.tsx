import React from "react";

interface CacheTabProps {
  readonly stats: unknown | null;
  readonly hotFiles: readonly unknown[];
  readonly loading: boolean;
}

export function CacheTab({ stats, hotFiles, loading }: CacheTabProps): React.ReactNode {
  if (loading) return <text>Loading cache stats...</text>;
  if (!stats) return <text>No cache data available.</text>;

  const s = stats as Record<string, unknown>;
  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%"><text>--- Cache Statistics ---</text></box>
      {Object.entries(s).map(([key, value]) => (
        <box key={key} height={1} width="100%">
          <text>{`  ${key}: ${JSON.stringify(value)}`}</text>
        </box>
      ))}
      {hotFiles.length > 0 && (
        <>
          <box height={1} width="100%"><text>--- Hot Files ---</text></box>
          {hotFiles.slice(0, 10).map((file, i) => (
            <box key={`hf-${i}`} height={1} width="100%">
              <text>{`  ${JSON.stringify(file)}`}</text>
            </box>
          ))}
        </>
      )}
    </box>
  );
}
