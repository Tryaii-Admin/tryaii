"""AST template introspection for the cache-lint warn hook.

Rules (verbatim from the cachelint design doc, and enforced here):
  * parse-never-execute — the caller's source is read as text and parsed
    with the ast module; nothing is ever evaluated or imported;
  * local-only — source never leaves the process; the only output is the
    warning text (which carries the file BASENAME, never the full path);
  * analysis cached per call site — one parse per module, one trace per
    (file, line), never per call.

Given the user frame that invoked chat()/stream(), this module traces the
prompt argument back to its template construction (f-string, .format(),
+-concatenation, single-assignment variable) and produces a template map:
static text segments and variable slots. The hook aligns the map against
the ACTUAL rendered prompt (the validation guard: any mismatch discards
the whole analysis) and overlays slot spans onto the canonical text to
classify each slot as inside or after the cacheable prefix.

Every public function is fail-open: any failure returns None and the hook
behaves exactly as if this module did not exist.
"""

from __future__ import annotations

import ast
import linecache
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional

_PKG_DIR = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep
_STDLIB_DIR = os.path.normcase(os.path.dirname(os.__file__)) + os.sep

_MAX_HOPS = 40
_MAX_DEPTH = 4
_MAX_SEGMENTS = 64
_MAX_FILE_BYTES = 1_000_000
_MODULE_CACHE_MAX = 64
_TRACE_CACHE_MAX = 256

_FAILED = object()   # cached-failure sentinel

_module_cache: dict = {}   # (normcase path, mtime_ns, size) -> ast.Module | _FAILED
_trace_cache: dict = {}    # (file key..., line) -> list[TemplateMap] | _FAILED


@dataclass
class CallSite:
    file: str
    line: int
    # Exact call span on 3.11+ (co_positions); None on 3.9/3.10.
    end_line: Optional[int] = None
    col: Optional[int] = None
    end_col: Optional[int] = None


@dataclass
class Segment:
    kind: str                  # "text" | "slot"
    text: str = ""             # kind == text
    expr: str = ""             # kind == slot: display expr (ast.unparse)
    has_call: bool = False     # slot expr contains a Call (tier-2 dynamic)
    resolved_from: Optional[tuple] = None   # (expr, line) when traced via a Name


@dataclass
class TemplateMap:
    target: str                # "prompt" | "system_message"
    segments: list
    var_line: Optional[int] = None   # Assign line when the template came from a Name


@dataclass
class OverlaySlot:
    expr: str
    start: int                 # canonical char offsets
    end: int
    in_prefix: bool
    has_call: bool
    resolved_from: Optional[tuple]


# ---------------------------------------------------------------------------
# Call-site capture
# ---------------------------------------------------------------------------

def capture_call_site() -> Optional[CallSite]:
    """Walk the stack to the first frame outside the tryaii package.

    Frames inside the package are skipped; a first-foreign frame that is
    synthetic (<stdin>, <string>, ...), stdlib (asyncio machinery — the
    create_task/gather dead-end), or an installed third-party package is a
    dead end -> None. Frame depths are never hardcoded.
    """
    try:
        frame = sys._getframe(1)
    except ValueError:  # pragma: no cover - _getframe is CPython-guaranteed
        return None
    hops = 0
    while frame is not None and hops < _MAX_HOPS:
        filename = frame.f_code.co_filename
        norm = os.path.normcase(os.path.abspath(filename)) if filename else ""
        if not norm.startswith(_PKG_DIR):
            if not filename or filename.startswith("<"):
                return None
            if norm.startswith(_STDLIB_DIR):
                return None      # asyncio / threading machinery: dead-end
            if f"{os.sep}site-packages{os.sep}" in norm or f"{os.sep}dist-packages{os.sep}" in norm:
                return None      # a wrapper library, not user code
            site = CallSite(file=filename, line=frame.f_lineno)
            code = frame.f_code
            if hasattr(code, "co_positions"):   # 3.11+: exact call span
                try:
                    positions = list(code.co_positions())
                    idx = frame.f_lasti // 2
                    if 0 <= idx < len(positions):
                        lineno, end_lineno, col, end_col = positions[idx]
                        if lineno is not None:
                            site.line = lineno
                            site.end_line = end_lineno
                            site.col = col
                            site.end_col = end_col
                except Exception:  # noqa: BLE001 - span precision is optional
                    pass
            return site
        frame = frame.f_back
        hops += 1
    return None


# ---------------------------------------------------------------------------
# Module parsing (cached)
# ---------------------------------------------------------------------------

def _file_key(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size > _MAX_FILE_BYTES:
        return None
    return (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size)


def _module_ast(path: str, key: tuple):
    cached = _module_cache.get(key)
    if cached is not None:
        return None if cached is _FAILED else cached
    if len(_module_cache) >= _MODULE_CACHE_MAX:
        _module_cache.pop(next(iter(_module_cache)))
    linecache.checkcache(path)
    lines = linecache.getlines(path)
    if not lines:
        _module_cache[key] = _FAILED
        return None
    try:
        tree = ast.parse("".join(lines))
    except (SyntaxError, ValueError):
        _module_cache[key] = _FAILED
        return None
    _module_cache[key] = tree
    return tree


# ---------------------------------------------------------------------------
# Call node + argument location
# ---------------------------------------------------------------------------

def _find_call(tree: ast.Module, site: CallSite) -> Optional[ast.Call]:
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if site.col is not None and site.end_line is not None:
            # 3.11+ exact span match
            if (node.lineno == site.line and node.col_offset == site.col
                    and end == site.end_line
                    and getattr(node, "end_col_offset", None) == site.end_col):
                return node
            continue
        if node.lineno <= site.line <= end:
            if best is None or (node.lineno, node.col_offset) >= (best.lineno, best.col_offset):
                best = node   # innermost by position
    return best


def _candidates(call: ast.Call) -> list:
    """(target, expr) pairs for the SDK's chat/stream signatures."""
    out = []
    if call.args:
        first = call.args[0]
        if not isinstance(first, ast.Starred):
            out.append(("prompt", first))
    for kw in call.keywords:
        if kw.arg in ("prompt", "system_message", "systemMessage"):
            target = "prompt" if kw.arg == "prompt" else "system_message"
            out.append((target, kw.value))
    return out


# ---------------------------------------------------------------------------
# Template tracing
# ---------------------------------------------------------------------------

def _scope_assigns(tree: ast.Module) -> dict:
    """name -> list of (Assign node, value) for bare-Name targets, whole file."""
    scope: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope.setdefault(target.id, []).append(node)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            target = node.target
            if isinstance(target, ast.Name):
                # counts as an extra binding -> disqualifies single-assignment
                scope.setdefault(target.id, []).append(node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.target, ast.Name):
                scope.setdefault(node.target.id, []).append(node)
    return scope


def _contains_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


_FORMAT_SLOT_RE = re.compile(r"\{([^{}]*)\}")


def _slot_meta(expr: ast.AST, scope: dict) -> Optional[tuple]:
    """(display_expr, has_call, resolved_from) for a slot's value expression.

    A bare Name is traced ONE extra hop through its single assignment (the
    design doc's `{DAY_OF_WEEK} <- datetime.now().strftime(...)` line), and
    tier-2 call detection looks through that hop too.
    """
    try:
        text = ast.unparse(expr)
    except Exception:  # noqa: BLE001
        return None
    has_call = _contains_call(expr)
    resolved_from = None
    if isinstance(expr, ast.Name):
        bindings = scope.get(expr.id, [])
        assigns = [b for b in bindings if isinstance(b, ast.Assign)]
        if len(bindings) == 1 and len(assigns) == 1:
            assign = assigns[0]
            try:
                resolved_from = (ast.unparse(assign.value), assign.lineno)
                has_call = has_call or _contains_call(assign.value)
            except Exception:  # noqa: BLE001
                resolved_from = None
    return text, has_call, resolved_from


def _segments(expr, scope: dict, use_line: int, depth: int = 0) -> Optional[list]:
    if depth > _MAX_DEPTH:
        return None

    if isinstance(expr, ast.JoinedStr):
        segs = []
        # Order comes purely from .values — child col_offsets are bogus pre-3.12.
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                segs.append(Segment(kind="text", text=value.value))
            elif isinstance(value, ast.FormattedValue):
                meta = _slot_meta(value.value, scope)
                if meta is None:
                    return None
                text, has_call, resolved_from = meta
                segs.append(Segment(kind="slot", expr=text, has_call=has_call,
                                    resolved_from=resolved_from))
            else:
                return None
        return segs if len(segs) <= _MAX_SEGMENTS else None

    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [Segment(kind="text", text=expr.value)]

    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _segments(expr.left, scope, use_line, depth + 1)
        right = _segments(expr.right, scope, use_line, depth + 1)
        if left is None or right is None:
            return None
        combined = left + right
        return combined if len(combined) <= _MAX_SEGMENTS else None

    if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "format"):
        base = _segments(expr.func.value, scope, use_line, depth + 1)
        if base is None or any(s.kind != "text" for s in base):
            return None
        kwargs = {kw.arg: kw.value for kw in expr.keywords if kw.arg}
        positional = list(expr.args)
        segs: list = []
        pos_index = 0
        for part in base:
            last = 0
            for m in _FORMAT_SLOT_RE.finditer(part.text):
                if m.start() > last:
                    segs.append(Segment(kind="text", text=part.text[last:m.start()]))
                name = m.group(1).split("!")[0].split(":")[0]
                value = None
                if name and not name.isdigit():
                    value = kwargs.get(name)
                elif positional:
                    idx = int(name) if name.isdigit() else pos_index
                    if idx < len(positional):
                        value = positional[idx]
                    pos_index += 1
                if value is not None:
                    meta = _slot_meta(value, scope)
                    if meta is None:
                        return None
                    text, has_call, resolved_from = meta
                    segs.append(Segment(kind="slot", expr=text, has_call=has_call,
                                        resolved_from=resolved_from))
                else:
                    segs.append(Segment(kind="slot", expr=name or "<positional>",
                                        has_call=False, resolved_from=None))
                last = m.end()
            if last < len(part.text):
                segs.append(Segment(kind="text", text=part.text[last:]))
        return segs if segs and len(segs) <= _MAX_SEGMENTS else None

    if isinstance(expr, ast.Name):
        bindings = scope.get(expr.id, [])
        assigns = [b for b in bindings if isinstance(b, ast.Assign)]
        if len(bindings) != 1 or len(assigns) != 1:
            return None   # unbound, reassigned, or loop-bound: cannot know which render fired
        assign = assigns[0]
        if assign.lineno >= use_line:
            return None
        return _segments(assign.value, scope, use_line, depth + 1)

    return None   # params, attributes, subscripts, everything else: UNKNOWN


def analyze_call_site(site: CallSite, _messages) -> Optional[list]:
    """Trace every candidate prompt argument at the call site. Fail-open."""
    try:
        key = _file_key(site.file)
        if key is None:
            return None
        trace_key = key + (site.line,)
        cached = _trace_cache.get(trace_key)
        if cached is not None:
            return None if cached is _FAILED else cached
        if len(_trace_cache) >= _TRACE_CACHE_MAX:
            _trace_cache.pop(next(iter(_trace_cache)))

        tree = _module_ast(site.file, key)
        if tree is None:
            _trace_cache[trace_key] = _FAILED
            return None
        call = _find_call(tree, site)
        if call is None:
            _trace_cache[trace_key] = _FAILED
            return None
        scope = _scope_assigns(tree)

        maps = []
        for target, expr in _candidates(call):
            var_line = None
            if isinstance(expr, ast.Name):
                bindings = [b for b in scope.get(expr.id, []) if isinstance(b, ast.Assign)]
                if len(bindings) == 1:
                    var_line = bindings[0].lineno
            segs = _segments(expr, scope, call.lineno)
            if segs is None:
                continue
            if any(s.kind == "slot" for s in segs):
                maps.append(TemplateMap(target=target, segments=segs, var_line=var_line))
        result = maps or None
        _trace_cache[trace_key] = result if result is not None else _FAILED
        return result
    except Exception:  # noqa: BLE001 - fail-open, always
        return None


def align(segments: list, rendered: str) -> Optional[list]:
    """Match static text segments against the rendered string; slot spans out.

    The validation guard: requires a FULL match with at least one non-empty
    text segment — truncation, stale source, wrong node, conditional
    templates and bundler rewrites all fail here and discard the analysis.
    """
    parts = [s for s in segments if not (s.kind == "text" and s.text == "")]
    if not any(s.kind == "text" for s in parts):
        return None
    slot_count = sum(1 for s in parts if s.kind == "slot")
    pattern = "^"
    slot_seen = 0
    for seg in parts:
        if seg.kind == "text":
            pattern += re.escape(seg.text)
        else:
            slot_seen += 1
            pattern += "([\\s\\S]*)" if slot_seen == slot_count else "([\\s\\S]*?)"
    pattern += "$"
    m = re.match(pattern, rendered)
    if m is None:
        return None
    return [m.span(i + 1) for i in range(slot_count)]


def overlay(segments: list, spans: list, content_start: int,
            boundary: int) -> list:
    """Map slot spans (message-relative) to canonical offsets + prefix flags."""
    slots = []
    slot_iter = iter(spans)
    for seg in segments:
        if seg.kind != "slot":
            continue
        start, end = next(slot_iter)
        slots.append(OverlaySlot(
            expr=seg.expr,
            start=content_start + start,
            end=content_start + end,
            in_prefix=(content_start + start) < boundary,
            has_call=seg.has_call,
            resolved_from=seg.resolved_from,
        ))
    return slots
