/**
 * Hook that returns the current terminal column count, updating on resize.
 *
 * Falls back to 80 when stdout is not a TTY (CI, piped output).
 */

import { useState, useEffect } from "react";

export function useTerminalColumns(): number {
  const [columns, setColumns] = useState(process.stdout.columns ?? 80);

  useEffect(() => {
    const onResize = (): void => {
      setColumns(process.stdout.columns ?? 80);
    };
    process.stdout.on("resize", onResize);
    return () => {
      process.stdout.off("resize", onResize);
    };
  }, []);

  return columns;
}
