// The embedding model of the tryaii SDK, as a data-only package (TS-253).
// The same model.onnx as the PyPI package tryaii-minilm-int8; tryaii checks
// its sha256 before it runs it.
import { fileURLToPath } from 'node:url';

export const MODEL_ID = 'all-MiniLM-L6-v2-onnx-int8';
export const SHA256 = '8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6';
export const SIZE_BYTES = 22928619;

/** Absolute path of the installed model.onnx. */
export function modelPath() {
  return fileURLToPath(new URL('./model.onnx', import.meta.url));
}
