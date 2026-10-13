/**
 * Routing text from a native `messages[]` payload (SPEC.md §2.1).
 *
 * Pure and deterministic: the same messages always give the same text and
 * hash. The wrapper routes on this text; the text itself is never stored —
 * only its sha256 (`prompt_hash`) and, separately, its embedding. Mirrors
 * monitor/extract.py.
 */

import { createHash } from 'node:crypto';

import { MAX_PROMPT_LENGTH } from '../router.js';

export const SYSTEM_ROLES = ['system', 'developer'];
export const USER_ROLE = 'user';

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * The text of one message's `content`: a string as is; a list of content
 * parts keeps only the parts whose `type` is "text" (joined with `\n`);
 * anything else (null, images, tool payloads) is empty.
 */
export function messageText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const part of content) {
      if (isRecord(part) && part.type === 'text' && typeof part.text === 'string') {
        parts.push(part.text);
      }
    }
    return parts.join('\n');
  }
  return '';
}

/** sha256 hex of the UTF-8 routing text (`""` hashes too). */
export function promptHash(text: string): string {
  return createHash('sha256').update(text, 'utf-8').digest('hex');
}

/** Truncate to `limit` code points (Python slices by code point). */
function truncateCodePoints(text: string, limit: number): string {
  if (text.length <= limit) return text; // UTF-16 units >= code points
  const points = Array.from(text);
  return points.length > limit ? points.slice(0, limit).join('') : text;
}

/**
 * `[text, prompt_hash]` for a chat-completions `messages` list.
 *
 * `text` = the FIRST system/developer message's text, a blank line, then
 * the LAST user message's text; the user text alone when there is no
 * system text; `""` when there is no user text at all (a call without a
 * user turn is not routed). Truncated to MAX_PROMPT_LENGTH code points.
 * Non-object messages are ignored.
 */
export function routingText(messages: unknown): [string, string] {
  let systemText: string | null = null;
  let userText: string | null = null;
  if (Array.isArray(messages)) {
    for (const msg of messages) {
      if (!isRecord(msg)) continue;
      const role = msg.role;
      if (typeof role === 'string' && SYSTEM_ROLES.includes(role) && systemText === null) {
        systemText = messageText(msg.content);
      } else if (role === USER_ROLE) {
        userText = messageText(msg.content);
      }
    }
  }
  let text: string;
  if (!userText) text = '';
  else if (systemText) text = systemText + '\n\n' + userText;
  else text = userText;
  text = truncateCodePoints(text, MAX_PROMPT_LENGTH);
  return [text, promptHash(text)];
}
