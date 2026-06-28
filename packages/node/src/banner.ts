/**
 * Animated startup banner for the TryAii-DRE CLI.
 *
 * Renders a blue -> red `TRYAII` wordmark to stderr so stdout stays clean for
 * piped / JSON output. Color and animation are suppressed automatically when:
 *
 *   - stderr is not a TTY (piped, redirected, or CI) -- nothing is printed
 *   - NO_COLOR is set, or TERM=dumb -- printed once, monochrome, no animation
 *   - TRYAII_NO_BANNER is set, or --no-banner is passed -- nothing is printed
 *
 * A 24-bit gradient is used when the terminal advertises truecolor
 * (COLORTERM=truecolor/24bit); otherwise a 256-color two-tone fallback is used.
 *
 * Kept intentionally in sync with the Python SDK banner (tryaii/cli/banner.py)
 * so both CLIs greet users identically.
 */

import { createRequire } from 'node:module';

/** Big "TRYAII" wordmark (ANSI Shadow style). All rows are the same width. */
const ART = [
  '████████╗██████╗ ██╗   ██╗ █████╗ ██╗██╗',
  '╚══██╔══╝██╔══██╗╚██╗ ██╔╝██╔══██╗██║██║',
  '   ██║   ██████╔╝ ╚████╔╝ ███████║██║██║',
  '   ██║   ██╔══██╗  ╚██╔╝  ██╔══██║██║██║',
  '   ██║   ██║  ██║   ██║   ██║  ██║██║██║',
  '   ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝',
];

// Gradient endpoints: vivid blue -> vivid red.
const BLUE: [number, number, number] = [56, 132, 255];
const RED: [number, number, number] = [244, 63, 94];

// 256-color fallback endpoints (DeepSkyBlue / IndianRed).
const BLUE_256 = '\x1b[38;5;39m';
const RED_256 = '\x1b[38;5;203m';

const RESET = '\x1b[0m';
const DIM = '\x1b[2m';

const FRAME_DELAY_MS = 33; // between revealed wordmark rows

function supportsColor(stream: NodeJS.WriteStream): boolean {
  if (process.env.NO_COLOR) return false;
  if (process.env.TERM === 'dumb') return false;
  return Boolean(stream.isTTY);
}

function truecolor(): boolean {
  const value = (process.env.COLORTERM ?? '').toLowerCase();
  return value === 'truecolor' || value === '24bit';
}

function lerp(start: number, end: number, t: number): number {
  return Math.round(start + (end - start) * t);
}

/** Color each character by its column position across the wordmark. */
function gradient(text: string, width: number): string {
  const span = Math.max(width - 1, 1);
  let out = '';
  for (let i = 0; i < text.length; i++) {
    const t = i / span;
    const r = lerp(BLUE[0], RED[0], t);
    const g = lerp(BLUE[1], RED[1], t);
    const b = lerp(BLUE[2], RED[2], t);
    out += `\x1b[38;2;${r};${g};${b}m${text[i]}`;
  }
  return out + RESET;
}

/** Fallback: blue first ~62%, red remainder (256-color). */
function twoTone(text: string): string {
  const split = Math.floor(text.length * 0.62);
  return `${BLUE_256}${text.slice(0, split)}${RED_256}${text.slice(split)}${RESET}`;
}

function packageVersion(): string {
  try {
    const require = createRequire(import.meta.url);
    const pkg = require('../package.json') as { version?: string };
    return pkg.version ?? '';
  } catch {
    return '';
  }
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Print the banner to stderr.
 *
 * Safe to call unconditionally: it self-suppresses for non-interactive streams
 * and honors NO_COLOR / TRYAII_NO_BANNER. `animate` can be forced off for tests.
 */
export async function showBanner(animate = true): Promise<void> {
  const stream = process.stderr;

  if (process.env.TRYAII_NO_BANNER) return;
  if (!stream.isTTY) return; // keep redirected / piped / CI output clean

  const color = supportsColor(stream);
  const width = Math.max(...ART.map((line) => line.length));
  const colorize: ((line: string, width: number) => string) | null = color
    ? truecolor()
      ? gradient
      : (line: string): string => twoTone(line)
    : null;
  const doAnimate = animate && color;

  try {
    for (const line of ART) {
      stream.write((colorize ? colorize(line, width) : line) + '\n');
      if (doAnimate) await sleep(FRAME_DELAY_MS);
    }

    const rule = '─'.repeat(width);
    const version = packageVersion();
    const ver = version ? `  ·  v${version}` : '';
    const ruleLine = colorize ? colorize(rule, width) : rule;
    const tagline = 'semantic, prompt-aware LLM routing  ·  benchmarks × cost × speed';

    if (color) {
      stream.write(ruleLine + '\n');
      stream.write(`  Diff Routing Engine${DIM}${ver}${RESET}\n`);
      stream.write(`${DIM}  ${tagline}${RESET}\n\n`);
    } else {
      stream.write(ruleLine + '\n');
      stream.write(`  Diff Routing Engine${ver}\n`);
      stream.write(`  ${tagline}\n\n`);
    }
  } catch {
    // Never let a cosmetic banner break the actual command.
  }
}
