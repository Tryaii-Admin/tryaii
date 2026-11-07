// The prompt router of the tryaii SDK (H4, router id h4-he-en), as a
// data-only package. The same payload as the PyPI package tryaii-router-h4;
// tryaii checks its hash before it uses the files.
//
// SHA256 is the sha256 of SHA256SUMS (one "<sha256>  <file>" line per payload
// file, sorted by name); SIZE_BYTES is the sum of the payload file sizes.
import { fileURLToPath } from 'node:url';

export const MODEL_ID = 'h4-he-en';
export const SHA256 = 'b85b07c215479dce26cd283e3220e47924b08944f53b450e477e1d13a8cb9675';
export const SIZE_BYTES = 12892408;
export const FILES = Object.freeze([
  'classes.json',
  'head.safetensors',
  'meta.json',
  'tokenizer.json',
  'vectors.safetensors',
]);

/** Absolute path of the directory holding the payload files. */
export function modelDir() {
  return fileURLToPath(new URL('.', import.meta.url));
}

/** Alias of modelDir() (same shape as tryaii-minilm-int8's modelPath()). */
export function modelPath() {
  return modelDir();
}
