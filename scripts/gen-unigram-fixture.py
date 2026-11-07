#!/usr/bin/env python
"""Freeze the H4 router's Unigram token ids for the Node tokenizer (H4 router).

Writes shared/embeddings/unigram_parity.json: for a set of self-generated
strings (no dataset text), the ids HF `tokenizers` gives with the shipped
H4 tokenizer.json (``Tokenizer.encode(text, add_special_tokens=False)``,
truncation off). The Node tokenizer (packages/node/src/embeddings/unigram.ts)
must produce exactly these ids (packages/node/tests/unigram.test.ts).

The header holds the sha256 of the tokenizer.json bytes; the Node test refuses
to compare against a different file.

Cases (seeded, deterministic): English, Hebrew (common words written here and
words built from Hebrew vocab pieces), mixed Hebrew/English, code, numbers,
punctuation, full-width / compatibility characters, niqqud and cantillation,
bidi marks, emoji (ZWJ sequences, skin tones, flags, keycaps), combining marks,
whitespace variants, the added tokens [PAD]/[UNK], every code point the
normalizer's Replace classes touch, every code point where Node's NFKC
(newer Unicode) needs the guard to match Rust's NFKC, very long words, other
scripts (CJK, Arabic, Cyrillic, ...), and random code point soup.

Usage:
  python scripts/gen-unigram-fixture.py [--tokenizer PATH]           # write
  python scripts/gen-unigram-fixture.py [--tokenizer PATH] --check   # exit 1 on drift

Default tokenizer: packages/router-h4-model/tokenizer.json. Needs `tokenizers`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

try:
    from tokenizers import Tokenizer
    import tokenizers as _tk
    TOKENIZERS_VERSION = getattr(_tk, "__version__", "unknown")
except ImportError:  # pragma: no cover - broken installs without __init__
    from tokenizers.tokenizers import Tokenizer  # type: ignore
    TOKENIZERS_VERSION = "unknown"

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TOKENIZER = REPO / "packages" / "router-h4-model" / "tokenizer.json"
OUT = REPO / "shared" / "embeddings" / "unigram_parity.json"
SEED = 20261107

EN_WORDS = """the a an and or but if then else when while for with without of to in on at by from
into over under about after before between through during model router token prompt answer question
please write explain summarize translate code function class return value error test data file user
system server client request response cache price fast slow cheap best quality hello world good bad
small large simple complex reason math science history language english hebrew python javascript
typescript rust sql database query table index learn train evaluate score benchmark latency cost
budget policy route classify embed vector matrix compute memory network secure password email phone
address date time today tomorrow yesterday morning evening week month year number count list item
sort filter map reduce async await promise thread process kernel linux windows browser cloud deploy
docker build release version update install configure enable disable debug trace log metric alert
can could would should must may might will shall do does did is are was were be been being have has
I you he she it we they me him her us them my your his its our their this that these those what why
how where who which whose don't can't won't it's I'm you're they're O'Reilly e-mail state-of-the-art""".split()

HE_WORDS = """שלום עולם אני אתה את הוא היא אנחנו אתם הם הן של על עם בלי אל מן כי אם אבל או גם רק
כבר עוד לא כן מה למה איך איפה מתי מי זה זאת אלה היום מחר אתמול בוקר ערב לילה שבוע חודש שנה
ספר מחשב תוכנה קוד שאלה תשובה בבקשה תודה סליחה טוב רע גדול קטן חדש ישן מהר לאט זול יקר
לכתוב להסביר לתרגם לסכם ללמוד לבדוק לחשב לבנות לפתוח לסגור לשלוח לקבל מודל נתונים טבלה
שרת לקוח בקשה תגובה מחיר איכות שפה עברית אנגלית מתמטיקה מדע היסטוריה ירושלים תל־אביב חיפה
ישראל ממשלה חברה עבודה משפחה ילד ילדה בית רחוב עיר מדינה כסף שקל דולר אחוז מספר רשימה
הבינה המלאכותית למידת מכונה רשת עצבית תכנות פייתון בסיס נתונים שאילתה אבטחה סיסמה דואר
והילדים ובבית כשהלכנו שהמחשב לכשתגיע מהמערכת בתוכנית הכי""".split()

CODE = [
    "def f(x):\n    return x * 2\n",
    "for (let i = 0; i < n; i++) { sum += a[i]; }",
    "SELECT id, name FROM users WHERE age >= 18 ORDER BY name;",
    "const re = /^[a-z_]+\\d*$/gi;",
    "fn main() { println!(\"{}\", 1 + 2); }",
    "if (a && b || !c) { x = y ?? z; }",
    "#include <stdio.h>\nint main(void){printf(\"%d\\n\", 42);}",
    "<div class=\"box\" id='x'>שלום</div>",
    "{\"key\": [1, 2.5, -3e10, null, true], \"he\": \"ערך\"}",
    "x = {k: v for k, v in d.items() if v is not None}",
    "git commit -m \"fix: handle ~/path & $HOME | grep -v '#'\"",
    "a->b->c; *p++ = **q; arr[i][j] ^= mask << 3;",
    "@decorator\nclass Foo(Bar):\n\tpass\n",
    "`template ${value}` + 'single' + \"double\"",
    "C:\\Users\\name\\file.txt and /usr/local/bin/python3",
    "https://example.com/path?q=1&r=%20#frag",
    "user@example.co.il, admin+tag@sub.domain.org",
]

PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
UNI_PUNCT = "«»„“”‘’‹›–—―…·•‣※¡¿׳״־،؛؟。、「」『』【】〈〉《》！？：；（）［］｛｝"

COMPAT = list("ＡＢＣａｂｃ０１２３４５６７８９！＃＄％＆（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
              "①②③⑴⑵⒈⒉ⅠⅡⅢⅣⅰⅱⅲ½¼¾⅓²³¹⁴⁵ₐₑ₁₂ﬁﬂﬀﬃﬄﬅﬆ㎏㎞㎝㎡㏄㍱㍲㈱㈲㊤㊥ℌℍℕℙℚℝℤ℃℉™℠№Å"
              "ｶﾀｶﾅﾞﾊﾟ㋐㋑ﾭﾬ゙゚ﭏﬠﬡﬢﬣﬤﬥﬦﬧﬨ﬩שׁשׂוּאַאָ﷽ﷺﷻﺍﺎﺏﺐﻻﻼ︵︶︷︸﹙﹚ﹰﹱ𝐀𝐁𝐚𝑨𝒂𝓐𝔄𝕬𝖆𝗔𝘢𝙰𝟎𝟏𝟗𝟬"
              "ǅǆǄĳĲŀĿŉſµ˙˚˛˜˝΅·϶ẛ")

EMOJI = [
    "😀", "😂", "🥲", "👍", "👍🏽", "👍🏿", "❤️", "❤", "✨", "🔥", "🎉", "🇮🇱", "🇺🇸", "🏳️‍🌈",
    "👨‍👩‍👧‍👦", "👩‍💻", "🧑🏾‍🚀", "👁️‍🗨️", "#️⃣", "1️⃣", "©️", "™️", "🫠", "🪿", "🧬", "🤖",
    "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "🙂‍↔️", "🐦‍🔥", "⚕︎", "☺️",
]

BIDI = ["\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069", "\u061c"]

WS = ["\t", "\n", "\r", "\r\n", "\x0b", "\x0c", " ", "  ", "   ", "\u00a0", "\u0085", "\u1680",
      "\u2000", "\u2002", "\u2003", "\u2007", "\u2009", "\u200a", "\u2028", "\u2029", "\u202f",
      "\u205f", "\u3000", "\u200b", "\ufeff", "\u180e"]

NIQQUD = [chr(c) for c in range(0x05B0, 0x05BD)] + ["\u05BF", "\u05C1", "\u05C2", "\u05C4", "\u05C5", "\u05C7"]
CANTILLATION = [chr(c) for c in range(0x0591, 0x05B0)]
COMBINING = [chr(c) for c in range(0x0300, 0x0370)] + ["\u0483", "\u0610", "\u064b", "\u0670",
                                                      "\u093c", "\u0e31", "\u1ab0", "\u1dc0", "\u20d0", "\ufe20"]

OTHER_SCRIPTS = [
    "机器学习模型", "你好世界", "東京都", "こんにちは", "カタカナ", "한국어 문장", "مرحبا بالعالم",
    "الذكاء الاصطناعي", "Привет мир", "машинное обучение", "Γειά σου κόσμε", "สวัสดีครับ",
    "नमस्ते दुनिया", "ქართული", "Բարեւ", "ሰላም", "ᚠᚢᚦ", "𐌰𐌱𐌲", "ꦲꦏ꧀ꦱꦫ", "ᏣᎳᎩ", "ܫܠܡܐ",
    "Tiếng Việt có dấu", "Ελληνικά ΑΒΓ", "Œuvre façade naïve coöperate", "Straße ß ẞ",
]

ADDED = ["[UNK]", "[PAD]", "[UNK][PAD]", "[[UNK]]", "[UN[PAD]K]", "x[UNK]y", " [PAD] ", "[unk]",
         "[UNK", "UNK]", "[ UNK ]", "[UNK]\u0301", "שלום[UNK]עולם", "[PAD][PAD][PAD]"]

# Code points where Node 22 (Unicode 16) NFKC differs from tokenizers' Rust
# NFKC (unicode-normalization-alignments, older tables) in some context; the
# Node tokenizer guards them (unigram.ts NFKC_GUARD_RANGES).
NFKC_GAP = (
    "7FD 897-89F 8CA-8D3 9FE C3C D3B-D3C EBA 1715 1ABF-1ACE 1DF6-1DFA 32FF A7F2-A7F4 A82C AB69 "
    "105C9 105E4 10781-10785 10787-107B0 107B2-107BA 10D24-10D27 10D69-10D6D 10EAB-10EAC "
    "10EFD-10EFF 10F46-10F50 10F82-10F85 11070 1133B 11383 11385 1138E 11391 113C5 113C7-113C8 "
    "113CE-113D0 1145E 11839-1183A 11938 1193D-1193E 11943 119E0 11A34 11A47 11A99 11D42 "
    "11D44-11D45 11D97 11F41-11F42 16121-16128 1612F 16D68-16D6A 16FF0-16FF1 1CCD6-1CCF9 "
    "1E030-1E06D 1E08F 1E130-1E136 1E2AE 1E2EC-1E2EF 1E4EC-1E4EF 1E5EE-1E5EF 1F16C 1FBF0-1FBF9 "
    "105D2 105DA 11382 113C9 11384 113BB 1138B 113C2 11390 113B8 11935 11930 1611E 16129 1611F "
    "16120 16D67 16D63"
)


def expand_ranges(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split():
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo, 16), int(hi or lo, 16) + 1))
    return out


def regex_class_chars(tok_json: dict) -> list[str]:
    """Every code point listed in a [..] class of a Replace Regex in the normalizer."""
    chars: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "Replace" and "Regex" in node.get("pattern", {}):
                for cls in re.findall(r"\[([^\]]*)\]", node["pattern"]["Regex"]):
                    chars.extend(cls)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tok_json.get("normalizer"))
    return sorted(set(chars))


def hebrew_pieces(tok_json: dict) -> list[str]:
    heb = re.compile(r"^[\u05D0-\u05EA]+$")
    out = []
    for piece, _ in tok_json["model"]["vocab"]:
        p = piece.lstrip("\u2581")
        if p and heb.match(p):
            out.append(p)
    return sorted(set(out))


def build_cases(tok_json: dict) -> list[tuple[str, str]]:
    rng = random.Random(SEED)
    cases: list[tuple[str, str]] = []
    add = lambda cat, text: cases.append((cat, text))  # noqa: E731
    he_pieces = hebrew_pieces(tok_json)

    def he_word() -> str:
        if rng.random() < 0.5:
            return rng.choice(HE_WORDS)
        return "".join(rng.choice(he_pieces) for _ in range(rng.randint(1, 3)))

    def en_sentence(n: int) -> str:
        words = [rng.choice(EN_WORDS) for _ in range(n)]
        if rng.random() < 0.5:
            words[0] = words[0].capitalize()
        if rng.random() < 0.2:
            words = [w.upper() if rng.random() < 0.3 else w for w in words]
        return " ".join(words) + rng.choice(["", ".", "?", "!", "...", ":", " ;", ","])

    def he_sentence(n: int) -> str:
        return " ".join(he_word() for _ in range(n)) + rng.choice(["", ".", "?", "!", "׃", ","])

    # trivial / whitespace
    for t in ["", " ", "  ", "\t", "\n", "\r\n", "\u00a0", "\u3000", " \t\n ", "\u200b", "\ufeff",
              "\u2581", "\u2581\u2581", " \u2581 ", "a", "A", "א", ".", "..", "!", "[", "]"]:
        add("trivial", t)

    for _ in range(400):
        add("english", en_sentence(rng.randint(1, 25)))
    for _ in range(450):
        add("hebrew", he_sentence(rng.randint(1, 20)))
    for _ in range(250):
        parts = []
        for _ in range(rng.randint(2, 12)):
            parts.append(he_word() if rng.random() < 0.5 else rng.choice(EN_WORDS))
        add("mixed", " ".join(parts) + rng.choice(["", "?", ".", " (API)", " v2.0", " GPT-4o"]))
    for c in CODE:
        add("code", c)
    for _ in range(80):
        a, b = rng.sample(CODE, 2)
        add("code", a + rng.choice(["\n", " ", "\n\n", "\t"]) + b)
    for _ in range(150):
        k = rng.random()
        if k < 0.2:
            t = str(rng.randint(0, 10 ** rng.randint(1, 15)))
        elif k < 0.4:
            t = f"{rng.uniform(-1e6, 1e6):.{rng.randint(0, 6)}f}"
        elif k < 0.55:
            t = f"{rng.randint(1, 31)}/{rng.randint(1, 12)}/{rng.randint(1990, 2030)}"
        elif k < 0.7:
            t = f"{rng.randint(1, 999):,}{rng.choice(['', ',000', ',000,000'])}.{rng.randint(0, 99):02d}"
        elif k < 0.8:
            t = f"+972-{rng.randint(50, 59)}-{rng.randint(1000000, 9999999)}"
        elif k < 0.9:
            t = f"{rng.randint(0, 100)}% {rng.choice(['₪', '$', '€', '£', '¥'])}{rng.randint(1, 9999)}"
        else:
            t = f"{rng.choice(HE_WORDS)} {rng.randint(1, 2000)} {rng.choice(EN_WORDS)} 3.14e-{rng.randint(1, 9)}"
        add("numbers", t)
    for ch in PUNCT + UNI_PUNCT:
        add("punct", ch)
        add("punct", f"a{ch}b")
        add("punct", f"{ch}{ch}{ch} שלום {ch}")
    for _ in range(80):
        add("punct", "".join(rng.choice(PUNCT + UNI_PUNCT + " ") for _ in range(rng.randint(2, 30))))
    for ch in COMPAT:
        add("compat", ch)
        add("compat", f"x{ch}y {ch}ש")
    for _ in range(60):
        add("compat", "".join(rng.choice(COMPAT + [" ", "a", "א"]) for _ in range(rng.randint(3, 20))))
    for _ in range(150):
        w = he_word()
        out = ""
        for ch in w:
            out += ch
            if rng.random() < 0.7:
                out += rng.choice(NIQQUD)
            if rng.random() < 0.15:
                out += rng.choice(NIQQUD + CANTILLATION)
        add("niqqud", out + (" " + he_word() if rng.random() < 0.5 else ""))
    for b in BIDI:
        add("bidi", f"{b}שלום{b} world{b}")
        add("bidi", f"abc{b}{b}123")
    for _ in range(40):
        add("bidi", rng.choice(BIDI) + he_sentence(3) + rng.choice(BIDI) + " " + en_sentence(3) + rng.choice(BIDI))
    for e in EMOJI:
        add("emoji", e)
        add("emoji", f"hello {e} שלום{e}{e}")
    for _ in range(80):
        add("emoji", " ".join(rng.choice(EMOJI + EN_WORDS[:20] + HE_WORDS[:20]) for _ in range(rng.randint(2, 10))))
    for _ in range(150):
        base = rng.choice(["e", "a", "o", "n", "u", "A", "ס", "ﬁ", "i", "ı", "ж", "ع", "क"])
        marks = "".join(rng.choice(COMBINING) for _ in range(rng.randint(1, 4)))
        add("combining", f"{rng.choice(EN_WORDS)}{base}{marks}{rng.choice(EN_WORDS)} {base}{marks}")
    for w in WS:
        add("whitespace", f"{w}hello{w}world{w}")
        add("whitespace", f"שלום{w}{w}עולם")
        add("whitespace", w * 3 + "x")
    for _ in range(120):
        words = [rng.choice(EN_WORDS + HE_WORDS) for _ in range(rng.randint(1, 8))]
        add("whitespace", "".join(w + "".join(rng.choice(WS) for _ in range(rng.randint(1, 3))) for w in words))
    for t in OTHER_SCRIPTS:
        add("scripts", t)
        add("scripts", f"{t} {rng.choice(EN_WORDS)} {rng.choice(HE_WORDS)}")
    for _ in range(60):
        block = rng.choice([(0x4E00, 0x9FFF), (0x0600, 0x06FF), (0x0400, 0x04FF), (0x0E00, 0x0E7F),
                            (0x0900, 0x097F), (0xAC00, 0xD7A3), (0x3040, 0x30FF), (0x20000, 0x2A6DF)])
        add("scripts", "".join(chr(rng.randint(*block)) for _ in range(rng.randint(1, 15))))
    for t in ADDED:
        add("added", t)
    for _ in range(40):
        add("added", f"{en_sentence(3)}{rng.choice(['[UNK]', '[PAD]'])}{he_sentence(2)}")
    for ch in regex_class_chars(tok_json):
        add("replace-class", ch)
        add("replace-class", f"a{ch}b")
        add("replace-class", f"שלום{ch}{ch}עולם {ch}")
        add("replace-class", f"{ch} x {ch}")
    for cp in expand_ranges(NFKC_GAP):
        ch = chr(cp)
        add("nfkc-guard", f"{ch} a{ch}\u0316 a\u0301{ch}\u0301 e{ch}\u05b0")
    for decomposed in ["\U000105d2\u0307", "\U000105da\u0307", "\U00011382\U000113c9",
                       "\U0001611e\U0001611e", "\U00016d67\U00016d67", "\U00011935\U00011930"]:
        add("nfkc-guard", f"x{decomposed}y")
    for _ in range(25):
        n = rng.choice([200, 500, 1000, 3000, 5000])
        alphabet = rng.choice(["abcdefghijklmnopqrstuvwxyz", "אבגדהוזחטיכלמנסעפצקרשת", "0123456789", "aA1א-"])
        add("long", "".join(rng.choice(alphabet) for _ in range(n)))
    add("long", "a" * 4000)
    add("long", "א" * 4000)
    add("long", "😀" * 1000)
    add("long", " ".join(en_sentence(30) for _ in range(20)))
    add("long", " ".join(he_sentence(30) for _ in range(20)))
    add("long", "\u2581" * 50 + "x")
    for _ in range(200):
        chars = []
        for _ in range(rng.randint(1, 12)):
            cp = rng.choice([rng.randint(0x20, 0x2FF), rng.randint(0x300, 0x2FFF), rng.randint(0x3000, 0xFFFF),
                             rng.randint(0x10000, 0x1FFFF), rng.randint(0x0, 0x10FFFF), rng.randint(0x590, 0x5FF)])
            if 0xD800 <= cp <= 0xDFFF:
                cp = 0xFFFD
            chars.append(chr(cp))
        add("soup", "".join(chars))
    for c in list(range(0x0, 0x20)) + list(range(0x7F, 0xA0)):
        add("control", f"a{chr(c)}b{chr(c)}")
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", type=Path, default=DEFAULT_TOKENIZER)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    raw = args.tokenizer.read_bytes()
    tok_json = json.loads(raw.decode("utf-8"))
    if "Precompiled" in json.dumps(tok_json.get("normalizer")):
        print("tokenizer.json still has a Precompiled normalizer; use the shipped one", file=sys.stderr)
        return 2
    tok = Tokenizer.from_str(raw.decode("utf-8"))
    tok.no_truncation()
    tok.no_padding()

    cases = build_cases(tok_json)
    texts = [t for _, t in cases]
    encs = tok.encode_batch(texts, add_special_tokens=False)
    doc = {
        "description": "HF tokenizers ids for self-generated strings; scripts/gen-unigram-fixture.py",
        "tokenizer_sha256": hashlib.sha256(raw).hexdigest(),
        "tokenizers_version": TOKENIZERS_VERSION,
        "seed": SEED,
        "count": len(cases),
        "cases": [{"cat": c, "text": t, "ids": e.ids} for (c, t), e in zip(cases, encs)],
    }
    lines = ["{"]
    for k in ("description", "tokenizer_sha256", "tokenizers_version", "seed", "count"):
        lines.append(f"  {json.dumps(k)}: {json.dumps(doc[k], ensure_ascii=False)},")
    lines.append('  "cases": [')
    rows = [f"    {json.dumps(c, ensure_ascii=False)}" for c in doc["cases"]]
    lines.append(",\n".join(rows))
    lines.append("  ]")
    lines.append("}")
    text = "\n".join(lines) + "\n"

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is out of date; rerun scripts/gen-unigram-fixture.py", file=sys.stderr)
            return 1
        print(f"{OUT}: up to date ({len(cases)} cases)")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
