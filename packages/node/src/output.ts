/**
 * Paced stdout helper for the CLI (mirrors `_write_paced` in the Python SDK).
 *
 * Reveals human-readable output line-by-line at a controlled pace when attached
 * to an interactive terminal, but dumps instantly when stdout is piped/redirected
 * or when the banner is suppressed -- so scripted use, `--json`, and `--no-banner`
 * stay snappy and clean. Kept in its own module so the pacing logic is unit-testable
 * without importing the CLI entry point.
 */

/** Per-line delay (ms) when revealing output in an interactive terminal. */
export const LINE_DELAY_MS = 22;

/** Minimal stdout-like target so the writer can be exercised with a fake stream. */
export interface PacedStream {
  write(chunk: string): void;
  isTTY?: boolean;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Write `text`, revealing it line-by-line when `out` is an interactive terminal
 * (and the banner isn't suppressed); otherwise write it in one shot. Returns once
 * all output has been written.
 */
export async function writePaced(
  text: string,
  out: PacedStream = process.stdout,
  delayMs: number = LINE_DELAY_MS,
): Promise<void> {
  const animate = Boolean(out.isTTY) && !process.env.TRYAII_NO_BANNER;
  if (!animate) {
    out.write(text);
    return;
  }
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    out.write(i < lines.length - 1 ? lines[i] + '\n' : lines[i]);
    await sleep(delayMs);
  }
}
