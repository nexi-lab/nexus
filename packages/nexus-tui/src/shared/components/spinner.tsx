/**
 * Animated loading spinner using braille characters.
 */

import React, { useState, useEffect } from "react";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const INTERVAL_MS = 80;

interface SpinnerProps {
  readonly label?: string;
}

export function Spinner({ label }: SpinnerProps): React.ReactNode {
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => {
      setFrame((prev) => (prev + 1) % FRAMES.length);
    }, INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  const text = label ? `${FRAMES[frame]} ${label}` : FRAMES[frame]!;
  return <text>{text}</text>;
}
