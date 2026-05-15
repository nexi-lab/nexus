/**
 * tmux capture-pane test harness for TUI visual testing.
 *
 * Provides helpers to:
 * 1. Start the TUI in a tmux session with controlled dimensions
 * 2. Send keystrokes via tmux send-keys
 * 3. Capture pane content via tmux capture-pane -p
 * 4. Assert on expected strings/patterns
 *
 * Usage:
 * ```ts
 * const session = await TmuxSession.create({ width: 120, height: 40 });
 * try {
 *   await session.waitForText("NEXUS", 5000);       // Wait for TUI to render
 *   await session.sendKeys("5");                      // Navigate to Access panel
 *   await session.waitForText("[Manifests]", 3000);   // Verify tab rendered
 *   const content = await session.capturePane();
 *   expect(content).toContain("? Help");
 * } finally {
 *   await session.destroy();
 * }
 * ```
 *
 * @see Issue #3250, Issue 3A (tmux test harness)
 */

// Uses Bun.spawn directly for tmux commands (shell $ helper doesn't handle tmux args well)

const DEFAULT_WIDTH = 120;
const DEFAULT_HEIGHT = 40;
const TMUX_SESSION_PREFIX = "nexus-tui-test";
/**
 * Use a dedicated tmux socket so that sessions are not constrained by the
 * outer terminal's dimensions.  When a session has no attached client the
 * specified -x / -y values are respected exactly, which is critical for
 * tests that need a full-width terminal (≥ 80 cols) to render correctly.
 */
const TMUX_SOCKET = "/tmp/nexus-tui-test.sock";

let sessionCounter = 0;

export interface TmuxSessionOptions {
  /** Terminal width in columns. Default: 120 */
  width?: number;
  /** Terminal height in rows. Default: 40 */
  height?: number;
  /** Command to run inside tmux. Default: starts nexus-tui */
  command?: string;
  /** Nexus server URL. Default: http://localhost:2026 */
  url?: string;
  /** API key for auth. */
  apiKey?: string;
}

export class TmuxSession {
  readonly sessionName: string;
  private destroyed = false;

  private constructor(sessionName: string) {
    this.sessionName = sessionName;
  }

  /**
   * Create a new tmux session and start the TUI (or custom command).
   */
  static async create(options: TmuxSessionOptions = {}): Promise<TmuxSession> {
    const width = options.width ?? DEFAULT_WIDTH;
    const height = options.height ?? DEFAULT_HEIGHT;
    const sessionName = `${TMUX_SESSION_PREFIX}-${++sessionCounter}-${Date.now()}`;

    // Build TUI command — needs to cd into the package directory first
    const tuiCmd = options.command ?? buildTuiCommand(options);
    const pkgDir = new URL("../../", import.meta.url).pathname.replace(/\/$/, "");
    const fullCmd = `cd ${pkgDir} && ${tuiCmd}`;

    // Create detached tmux session with fixed dimensions.
    // Use a dedicated socket (-S) so this session is not constrained by the
    // outer terminal size (which would override -x/-y when a client is attached).
    const proc = Bun.spawn(
      ["tmux", "-S", TMUX_SOCKET, "new-session", "-d", "-s", sessionName, "-x", String(width), "-y", String(height), fullCmd],
      { stdout: "pipe", stderr: "pipe" },
    );
    await proc.exited;
    if (proc.exitCode !== 0) {
      const stderr = await new Response(proc.stderr).text();
      throw new Error(`tmux new-session failed: ${stderr}`);
    }

    const session = new TmuxSession(sessionName);

    // Wait for TUI to initialize
    await sleep(1000);

    return session;
  }

  /**
   * Capture the current pane content as a string.
   */
  async capturePane(): Promise<string> {
    if (this.destroyed) throw new Error("Session already destroyed");
    try {
      const proc = Bun.spawn(
        ["tmux", "-S", TMUX_SOCKET, "capture-pane", "-t", this.sessionName, "-p"],
        { stdout: "pipe", stderr: "pipe" },
      );
      const text = await new Response(proc.stdout).text();
      await proc.exited;
      return text;
    } catch {
      return "";
    }
  }

  /**
   * Send keystrokes to the tmux session.
   *
   * @param keys - tmux key notation: "a", "Enter", "Escape", "C-b" (Ctrl+B), etc.
   */
  async sendKeys(keys: string): Promise<void> {
    if (this.destroyed) throw new Error("Session already destroyed");
    const proc = Bun.spawn(
      ["tmux", "-S", TMUX_SOCKET, "send-keys", "-t", this.sessionName, keys],
      { stdout: "pipe", stderr: "pipe" },
    );
    await proc.exited;
    // Small delay for TUI to process the keystroke
    await sleep(200);
  }

  /**
   * Send a sequence of keys with delays between them.
   */
  async sendKeySequence(keys: string[], delayMs = 200): Promise<void> {
    for (const key of keys) {
      await this.sendKeys(key);
      await sleep(delayMs);
    }
  }

  /**
   * Wait for specific text to appear in the pane content.
   *
   * @param text - Text to search for
   * @param timeoutMs - Maximum time to wait. Default: 5000ms
   * @param pollMs - Polling interval. Default: 250ms
   * @returns The full pane content when the text was found
   * @throws If text doesn't appear within timeout
   */
  async waitForText(text: string, timeoutMs = 5000, pollMs = 250): Promise<string> {
    const deadline = Date.now() + timeoutMs;

    while (Date.now() < deadline) {
      const content = await this.capturePane();
      if (content.includes(text)) return content;
      await sleep(pollMs);
    }

    // Final attempt — capture and throw with diagnostics
    const finalContent = await this.capturePane();
    throw new Error(
      `Timed out waiting for text "${text}" after ${timeoutMs}ms.\n` +
      `Pane content (${finalContent.length} chars):\n` +
      `---\n${finalContent}\n---`,
    );
  }

  /**
   * Wait for a regex pattern to match in the pane content.
   */
  async waitForPattern(pattern: RegExp, timeoutMs = 5000, pollMs = 250): Promise<string> {
    const deadline = Date.now() + timeoutMs;

    while (Date.now() < deadline) {
      const content = await this.capturePane();
      if (pattern.test(content)) return content;
      await sleep(pollMs);
    }

    const finalContent = await this.capturePane();
    throw new Error(
      `Timed out waiting for pattern ${pattern} after ${timeoutMs}ms.\n` +
      `Pane content:\n---\n${finalContent}\n---`,
    );
  }

  /**
   * Assert the pane contains specific text.
   */
  async assertContains(text: string): Promise<void> {
    const content = await this.capturePane();
    if (!content.includes(text)) {
      throw new Error(
        `Expected pane to contain "${text}" but it doesn't.\n` +
        `Pane content:\n---\n${content}\n---`,
      );
    }
  }

  /**
   * Assert the pane does NOT contain specific text.
   */
  async assertNotContains(text: string): Promise<void> {
    const content = await this.capturePane();
    if (content.includes(text)) {
      throw new Error(
        `Expected pane NOT to contain "${text}" but it does.\n` +
        `Pane content:\n---\n${content}\n---`,
      );
    }
  }

  /**
   * Destroy the tmux session and clean up.
   */
  async destroy(): Promise<void> {
    if (this.destroyed) return;
    this.destroyed = true;
    try {
      const proc = Bun.spawn(
        ["tmux", "-S", TMUX_SOCKET, "kill-session", "-t", this.sessionName],
        { stdout: "pipe", stderr: "pipe" },
      );
      await proc.exited;
    } catch {
      // Session may have already exited
    }
  }
}

function buildTuiCommand(options: TmuxSessionOptions): string {
  const parts = ["bun", "run", "src/index.tsx"];
  if (options.url) parts.push("--url", options.url);
  if (options.apiKey) parts.push("--api-key", options.apiKey);
  return parts.join(" ");
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Helper to clean up any leftover test sessions.
 * Call in afterAll() or test teardown.
 */
export async function cleanupTestSessions(): Promise<void> {
  try {
    const proc = Bun.spawn(
      ["tmux", "-S", TMUX_SOCKET, "list-sessions", "-F", "#S"],
      { stdout: "pipe", stderr: "pipe" },
    );
    const output = await new Response(proc.stdout).text();
    await proc.exited;

    const testSessions = output
      .split("\n")
      .filter((s) => s.startsWith(TMUX_SESSION_PREFIX));
    for (const name of testSessions) {
      if (name.trim()) {
        const kill = Bun.spawn(
          ["tmux", "-S", TMUX_SOCKET, "kill-session", "-t", name.trim()],
          { stdout: "pipe", stderr: "pipe" },
        );
        await kill.exited;
      }
    }
  } catch {
    // No tmux server running — nothing to clean up
  }
}
