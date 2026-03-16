/**
 * React error boundary that renders a user-friendly error message
 * instead of crashing the TUI.
 */

import React from "react";

interface ErrorBoundaryProps {
  readonly children: React.ReactNode;
  readonly fallback?: React.ReactNode;
}

interface ErrorBoundaryState {
  readonly error: Error | null;
}

class ErrorBoundaryClass extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override render(): React.ReactNode {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      return (
        <box flexDirection="column" padding={1}>
          <text>Something went wrong:</text>
          <text>{this.state.error.message}</text>
        </box>
      );
    }
    return this.props.children;
  }
}

/**
 * Function component wrapper for the class-based error boundary.
 *
 * OpenTUI v0.1.87 has a bug where JSX.ElementClass extends
 * React.ComponentClass (constructor) instead of the instance type,
 * preventing class components from being used directly as JSX elements.
 * This wrapper sidesteps that issue.
 */
export function ErrorBoundary(props: ErrorBoundaryProps): React.ReactNode {
  return React.createElement(ErrorBoundaryClass, props);
}
