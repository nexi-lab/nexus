/**
 * Animated loading spinner using braille characters.
 */

import { createSignal, onCleanup } from "solid-js";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const INTERVAL_MS = 80;

interface SpinnerProps {
  readonly label?: string;
}

export function Spinner(props: SpinnerProps) {
  const [frame, setFrame] = createSignal(0);

  const timer = setInterval(() => {
    setFrame((prev) => (prev + 1) % FRAMES.length);
  }, INTERVAL_MS);
  onCleanup(() => clearInterval(timer));

  const text = () => props.label ? `${FRAMES[frame()]} ${props.label}` : FRAMES[frame()]!;
  return <text>{text}</text>;
}
