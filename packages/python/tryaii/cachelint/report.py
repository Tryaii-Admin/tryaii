"""Plain-text report renderer (78-column, deterministic).

Byte-identical to the Node SDK's report.ts for any normalized analyze()
result — the CLI cross-diff test compares the two directly. Numbers arrive
already normalized (SPEC.md §1.3), so str()/template interpolation agrees
across languages; thousands separators use the shared manual format.
"""

from __future__ import annotations

_W = 78
_SEV_TAG = {"high": "[HIGH]", "medium": "[MED ]", "low": "[low ]"}

_VERDICT_LABEL = {
    "CACHEABLE": "CACHEABLE",
    "CACHEABLE_WITH_ACTION": "CACHEABLE (action required)",
    "CACHEABLE_PREFIX": "CACHEABLE PREFIX (dynamic tail)",
    "BELOW_THRESHOLD": "BELOW THRESHOLD (won't cache)",
    "EFFECTIVELY_UNCACHEABLE": "EFFECTIVELY UNCACHEABLE",
    "UNKNOWN_THRESHOLD": "UNKNOWN THRESHOLD (structural analysis only)",
}


def _rule(char: str = "-") -> str:
    return char * _W


def _wrap(text: str, indent: int = 4, width: int = _W) -> list[str]:
    pad = " " * indent
    out, cur = [], ""
    for w in text.split():
        candidate = (cur + " " + w) if cur else w
        if len(pad) + len(candidate) > width and cur:
            out.append(pad + cur)
            cur = w
        else:
            cur = candidate
    if cur:
        out.append(pad + cur)
    return out


def _fmt_tokens(n) -> str:
    return f"~{n:,}" if isinstance(n, int) and not isinstance(n, bool) else "?"


def render(result: dict) -> str:
    lines: list[str] = []
    lines.append(_rule("="))
    lines.append(" CACHELINT - prompt-cache pre-flight report")
    lines.append(f"   knowledge base: {result['meta']['knowledge_base']}")
    lines.append(_rule("="))

    for item in result["items"]:
        lines.append("")
        header = f" INPUT #{item['index']} -- {item['provider']} / {item['model'] or '(no model)'}"
        if item.get("upstream"):
            header += f"  [upstream: {item['upstream']}]"
        lines.append(header)
        lines.append(_rule())

        v = item["verdict"]
        lines.append(f" Verdict   : {_VERDICT_LABEL.get(v['code'], v['code'])}")
        lines.extend(_wrap(v["summary"], indent=13))

        tr = item["token_report"]
        total = tr["total"]
        secs = "  ".join(f"{name}={_fmt_tokens(n)}" for name, n in tr["sections"].items())
        lines.append(f" Tokens    : total {_fmt_tokens(total['tokens'])}  ({total['method']})")
        if secs:
            lines.extend(_wrap(secs, indent=13))

        th = item["threshold"]
        if th["min_tokens"] is not None:
            meets = "meets" if th["meets"] else "BELOW"
            inc = f", +{th['increment']}-tok steps" if th.get("increment") else ""
            lines.append(
                f" Threshold : {th['min_tokens']:,} min ({th['tier']}{inc}) -> prompt {meets} the floor")
        else:
            lines.append(f" Threshold : unpublished ({th['tier']})")

        sr = item["system_report"]
        if sr["present"]:
            unit_ok = ""
            if th["min_tokens"] is not None:
                unit_ok = (" -> clears the floor alone" if sr["cached_unit_meets_threshold"]
                           else " -> below the floor alone")
            lines.append(f" SystemRpt : system {_fmt_tokens(sr['tokens'])} tok | cached unit "
                         f"(tools+system) {_fmt_tokens(sr['cached_unit_tokens'])} tok{unit_ok}"
                         + (f" | {sr['findings_in_system']} finding(s) inside system"
                            if sr["findings_in_system"] else ""))

        findings = item["findings"]
        if findings:
            lines.append(f" Findings  : {len(findings)}")
            for f in findings[:10]:
                tag = _SEV_TAG.get(f["severity"], f"[{f['severity']}]")
                lines.append(f"   {tag} {f['kind']}  @ {f['section']}+{f['section_offset']}"
                             f"  ({f['pct_into_prompt']:.0f}% into prompt)")
                lines.extend(_wrap(f'"{f["excerpt"]}"', indent=10))
                lines.extend(_wrap("-> " + f["why"], indent=10))
            if len(findings) > 10:
                lines.append(f"   ... and {len(findings) - 10} more")
        else:
            lines.append(" Findings  : none — no dynamic-content indicators detected")

        sp = item["stable_prefix"]
        lim = sp.get("limited_by")
        if lim:
            lines.append(f" StablePfx : {_fmt_tokens(sp['tokens'])} tok "
                         f"({sp['pct_of_prompt']}% of prompt), capped by {lim['kind']} "
                         f"at {lim['section']}+{lim['section_offset']}")
            if sp.get("if_first_fixed_tokens") is not None:
                lines.append(f"             fixing that first finding alone -> "
                             f"{_fmt_tokens(sp['if_first_fixed_tokens'])} tok")
        else:
            lines.append(f" StablePfx : {_fmt_tokens(sp['tokens'])} tok (entire prompt is stable)")

        en = item["enablement"]
        lines.append(f" Enablement: {en['mode'].upper()}")
        lines.extend(_wrap(en["detail"], indent=13))

        if item["recommendations"]:
            lines.append(" Recommendations:")
            for i, rec in enumerate(item["recommendations"], 1):
                lines.extend(_wrap(f"{i}. {rec}", indent=4))

        intel = item["provider_intel"]
        lines.append(" Intel     :")
        lines.extend(_wrap(f"TTL: {intel['ttl']}", indent=6))
        lines.extend(_wrap(f"Read discount: {intel['read_discount']}", indent=6))
        lines.extend(_wrap(f"Write cost: {intel['write_cost']}", indent=6))
        lines.extend(_wrap(f"Verify via: {intel['verify_field']}", indent=6))
        for g in intel["gotchas"]:
            lines.extend(_wrap(f"! {g}", indent=6))

        for w in item.get("warnings", []):
            lines.extend(_wrap(f"(warning) {w}", indent=2))

    for seq in result.get("sequences", []):
        g = seq["group"]
        lines.append("")
        if seq["count"] == 0:   # cross-group note
            lines.append(_rule("="))
            lines.extend(_wrap(seq["summary"], indent=1))
            continue
        lines.append(f" SEQUENCE -- {g['provider']} / {g['model']} "
                     f"({seq['count']} request(s): #{', #'.join(map(str, seq['indices']))})")
        lines.append(_rule())
        if seq.get("shared_prefix_all"):
            spa = seq["shared_prefix_all"]
            ok = "clears" if spa["meets_threshold"] else "does NOT clear"
            floor = f"{spa['min_tokens']:,}" if spa["min_tokens"] is not None else "unpublished"
            lines.append(f" Shared prefix across all: {_fmt_tokens(spa['tokens'])} tok "
                         f"-> {ok} the floor ({floor})")
        for p in seq["pairs"]:
            lines.append(f" #{p['from']} -> #{p['to']}  {p['expected']:<8} "
                         f"relation={p['relation']}  common prefix {_fmt_tokens(p['lcp_tokens'])} tok"
                         + (f" (cached {_fmt_tokens(p['expected_cached_tokens'])})"
                            if p["expected_cached_tokens"] else ""))
            d = p.get("diverged_at")
            if d:
                lines.append(f"      diverged at {d['section']}+{d['section_offset']} "
                             f"(char {d['offset']})")
                if p["likely_causes"]:
                    lines.extend(_wrap("likely cause: " + "; ".join(p["likely_causes"]), indent=6))
                lines.extend(_wrap(f'prev: "{d["context_prev"]}"', indent=6))
                lines.extend(_wrap(f'curr: "{d["context_curr"]}"', indent=6))
            t = p.get("ttl_check")
            if t:
                gap_line = f"gap between requests: {t['gap_seconds']}s"
                if t.get("note"):
                    gap_line += f" — {t['note']}"
                lines.extend(_wrap(gap_line, indent=6))
            for n in p["notes"]:
                lines.extend(_wrap("note: " + n, indent=6))
        lines.extend(_wrap("Summary: " + seq["summary"], indent=1))

    lines.append("")
    lines.append(_rule("="))
    lines.extend(_wrap("Caveat: " + result["meta"]["caveat"], indent=1))
    return "\n".join(lines)
