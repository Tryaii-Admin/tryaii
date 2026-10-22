/**
 * BERT WordPiece tokenizer, byte-for-byte the one the Python SDK runs
 * (HF `tokenizers`: BertNormalizer + BertPreTokenizer + WordPiece +
 * `[CLS] $A [SEP]`), read from the same `tokenizer.json` (TS-253).
 *
 * Python truncates to 256 tokens including `[CLS]`/`[SEP]` and never pads a
 * single text (`OnnxEmbeddingProvider` embeds one text per run); so does this.
 * Parity with the Python ids is frozen by shared/embeddings/tokenizer_parity.json.
 *
 * Unicode rules, as in the Rust implementation:
 * - clean text: drop U+0000, U+FFFD and "other" characters (categories Cc, Cf,
 *   Cn, Co) except tab/newline/carriage return; whitespace -> ' ';
 * - CJK ideographs (the BERT ranges) are surrounded by spaces;
 * - lowercase=true with strip_accents unset strips accents: NFD, then drop
 *   nonspacing marks (Mn); then lowercase;
 * - pre-tokenize: split on whitespace, and every punctuation character (ASCII
 *   punctuation or Unicode P*) is a token of its own;
 * - WordPiece: greedy longest match, continuation prefix `##`, a word longer
 *   than 100 characters or with no match becomes `[UNK]`.
 */

export interface TokenizerJson {
  model: {
    type: string;
    vocab: Record<string, number>;
    unk_token: string;
    continuing_subword_prefix: string;
    max_input_chars_per_word: number;
  };
  normalizer?: { type?: string; lowercase?: boolean; strip_accents?: boolean | null } | null;
}

export interface Encoding {
  ids: number[];
  attentionMask: number[];
}

const RE_WHITESPACE = /\p{White_Space}/u;
const RE_OTHER = /[\p{Cc}\p{Cf}\p{Cn}\p{Co}]/u;
const RE_PUNCT = /\p{P}/u;
const RE_MN = /\p{Mn}/gu;

function isWhitespace(ch: string): boolean {
  return ch === '\t' || ch === '\n' || ch === '\r' || ch === ' ' || RE_WHITESPACE.test(ch);
}

function isControl(ch: string): boolean {
  if (ch === '\t' || ch === '\n' || ch === '\r') return false;
  return RE_OTHER.test(ch);
}

function isPunctuation(ch: string): boolean {
  const cp = ch.codePointAt(0) as number;
  if ((cp >= 33 && cp <= 47) || (cp >= 58 && cp <= 64) || (cp >= 91 && cp <= 96) ||
      (cp >= 123 && cp <= 126)) {
    return true;
  }
  return RE_PUNCT.test(ch);
}

function isChineseChar(cp: number): boolean {
  return (
    (cp >= 0x4e00 && cp <= 0x9fff) ||
    (cp >= 0x3400 && cp <= 0x4dbf) ||
    (cp >= 0x20000 && cp <= 0x2a6df) ||
    (cp >= 0x2a700 && cp <= 0x2b73f) ||
    (cp >= 0x2b740 && cp <= 0x2b81f) ||
    (cp >= 0x2b920 && cp <= 0x2ceaf) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0x2f800 && cp <= 0x2fa1f)
  );
}

export class WordPieceTokenizer {
  readonly maxLength: number;
  private readonly vocab: Map<string, number>;
  private readonly unk: string;
  private readonly prefix: string;
  private readonly maxChars: number;
  private readonly lowercase: boolean;
  private readonly stripAccents: boolean;
  private readonly clsId: number;
  private readonly sepId: number;
  private readonly unkId: number;

  constructor(json: TokenizerJson, maxLength = 256) {
    if (json.model?.type !== 'WordPiece') {
      throw new Error(`unsupported tokenizer model: ${json.model?.type}`);
    }
    this.maxLength = maxLength;
    this.vocab = new Map(Object.entries(json.model.vocab));
    this.unk = json.model.unk_token;
    this.prefix = json.model.continuing_subword_prefix;
    this.maxChars = json.model.max_input_chars_per_word;
    this.lowercase = json.normalizer?.lowercase ?? true;
    const strip = json.normalizer?.strip_accents;
    this.stripAccents = strip === null || strip === undefined ? this.lowercase : strip;
    this.clsId = this.idOf('[CLS]');
    this.sepId = this.idOf('[SEP]');
    this.unkId = this.idOf(this.unk);
  }

  private idOf(token: string): number {
    const id = this.vocab.get(token);
    if (id === undefined) throw new Error(`token ${token} is not in the vocabulary`);
    return id;
  }

  /** BertNormalizer. */
  normalize(text: string): string {
    let out = '';
    for (const ch of text) {
      const cp = ch.codePointAt(0) as number;
      if (cp === 0 || cp === 0xfffd || isControl(ch)) continue;
      if (isWhitespace(ch)) {
        out += ' ';
      } else if (isChineseChar(cp)) {
        out += ` ${ch} `;
      } else {
        out += ch;
      }
    }
    if (this.stripAccents) out = out.normalize('NFD').replace(RE_MN, '');
    // Per character, like Rust's char::to_lowercase (no final-sigma context rule).
    if (this.lowercase) out = Array.from(out, (c) => c.toLowerCase()).join('');
    return out;
  }

  /** BertPreTokenizer: whitespace split, each punctuation char on its own. */
  preTokenize(text: string): string[] {
    const words: string[] = [];
    let current = '';
    for (const ch of text) {
      if (isWhitespace(ch)) {
        if (current) words.push(current);
        current = '';
      } else if (isPunctuation(ch)) {
        if (current) words.push(current);
        words.push(ch);
        current = '';
      } else {
        current += ch;
      }
    }
    if (current) words.push(current);
    return words;
  }

  /** WordPiece over one pre-token. */
  wordPiece(word: string): number[] {
    const chars = Array.from(word);
    if (chars.length > this.maxChars) return [this.unkId];
    const ids: number[] = [];
    let start = 0;
    while (start < chars.length) {
      let end = chars.length;
      let found: number | undefined;
      while (start < end) {
        let piece = chars.slice(start, end).join('');
        if (start > 0) piece = this.prefix + piece;
        const id = this.vocab.get(piece);
        if (id !== undefined) {
          found = id;
          break;
        }
        end -= 1;
      }
      if (found === undefined) return [this.unkId];
      ids.push(found);
      start = end;
    }
    return ids;
  }

  /** `[CLS] tokens [SEP]`, truncated to `maxLength` in total; no padding. */
  encode(text: string): Encoding {
    const body: number[] = [];
    const budget = this.maxLength - 2;
    for (const word of this.preTokenize(this.normalize(text))) {
      for (const id of this.wordPiece(word)) {
        if (body.length >= budget) break;
        body.push(id);
      }
      if (body.length >= budget) break;
    }
    const ids = [this.clsId, ...body, this.sepId];
    return { ids, attentionMask: ids.map(() => 1) };
  }
}
