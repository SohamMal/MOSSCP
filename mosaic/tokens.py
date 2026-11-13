"""Language-aware tokenizer and token normalization.

The tokenizer is deliberately lexical (no parsing): it must survive
syntactically broken student submissions. Structural analysis happens
later, in pyast.py / cast.py.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# Token kinds
NAME = "NAME"
KEYWORD = "KEYWORD"
NUMBER = "NUMBER"
STRING = "STRING"
OP = "OP"


@dataclass(slots=True)
class Token:
    kind: str
    value: str
    start: int  # char offset into source
    end: int  # char offset, exclusive
    line: int  # 1-based


PY_KEYWORDS = frozenset(
    """False None True and as assert async await break class continue def del elif
    else except finally for from global if import in is lambda nonlocal not or pass
    raise return try while with yield match case""".split()
)

C_KEYWORDS = frozenset(
    """alignas alignof asm auto bool break case catch char class const consteval
    constexpr const_cast continue decltype default delete do double dynamic_cast
    else enum explicit export extern false float for friend goto if inline int long
    mutable namespace new noexcept nullptr operator private protected public
    register reinterpret_cast return short signed sizeof static static_assert
    static_cast struct switch template this throw true try typedef typeid typename
    union unsigned using virtual void volatile wchar_t while""".split()
)

JAVA_KEYWORDS = frozenset(
    """abstract assert boolean break byte case catch char class const continue
    default do double else enum extends final finally float for goto if implements
    import instanceof int interface long native new package private protected
    public return short static strictfp super switch synchronized this throw throws
    transient try void volatile while true false null var record yield""".split()
)

JS_KEYWORDS = frozenset(
    """async await break case catch class const continue debugger default delete do
    else enum export extends false finally for function if implements import in
    instanceof interface let new null package private protected public return static
    super switch this throw true try typeof var void while with yield of""".split()
)

# Identifiers worth preserving: they carry real signal (a shared sequence of
# library calls is evidence) while local variable names do not.
PY_BUILTINS = frozenset(
    """print len range enumerate zip map filter sorted sum min max abs round int str
    float bool list dict set tuple open input append extend insert pop remove sort
    reverse keys values items get split join strip replace format startswith
    endswith find index count isinstance type super self cls staticmethod
    classmethod property Exception ValueError TypeError KeyError IndexError
    numpy np pandas pd torch math random os sys re json time collections itertools
    sqrt floor ceil log exp pow array zeros ones reshape shape dtype""".split()
)

C_BUILTINS = frozenset(
    """printf scanf sprintf sscanf fprintf fopen fclose fgets fputs puts putchar
    getchar malloc calloc realloc free memcpy memset memmove strlen strcpy strncpy
    strcmp strncmp strcat strtok atoi atof abs qsort exit rand srand time
    cout cin cerr endl std string vector map set unordered_map unordered_set pair
    push_back pop_back emplace_back begin end size empty insert erase find sort
    make_pair first second to_string stoi getline main NULL nullptr
    sqrt pow floor ceil fabs log exp sin cos tan""".split()
)

GENERIC_BUILTINS = frozenset(
    """print println printf main length size append push pop get set add remove
    toString String Integer Double System out console log Math sqrt abs max min""".split()
)


@dataclass(frozen=True, slots=True)
class LangSpec:
    name: str
    keywords: frozenset
    builtins: frozenset
    scanner: re.Pattern
    family: str  # "python" | "c" | "generic"


_PY_OPS = [
    "**=", "//=", ">>=", "<<=", "...", "!=", "==", ">=", "<=", "->", ":=", "**",
    "//", ">>", "<<", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@=",
]
_C_OPS = [
    "<<=", ">>=", "->*", "...", "::", "->", "++", "--", "<<", ">>", "<=", ">=",
    "==", "!=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", ".*",
]


def _ops_alt(ops: Sequence[str]) -> str:
    return "|".join(re.escape(o) for o in sorted(ops, key=len, reverse=True))


_PY_SCANNER = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<LCOM>\#[^\n]*)
    | (?P<STR>(?:[rRbBuUfF]{0,3})(?:\"\"\"(?s:.*?)\"\"\"|'''(?s:.*?)'''
                                  |\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'))
    | (?P<NUM>0[xXoObB][0-9a-fA-F_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?[jJ]?)
    | (?P<NAME>[A-Za-z_]\w*)
    | (?P<OP>"""
    + _ops_alt(_PY_OPS)
    + r"""|\S)
    """,
    re.VERBOSE,
)

_C_SCANNER = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<LCOM>//[^\n]*)
    | (?P<BCOM>/\*(?s:.*?)\*/)
    | (?P<PP>\#[ \t]*\w*(?:\\\n|[^\n])*)
    | (?P<STR>(?:u8|u|U|L)?\"(?:\\(?s:.)|[^\"\\])*\"|R\"\((?s:.*?)\)\")
    | (?P<CHR>(?:u8|u|U|L)?'(?:\\(?s:.)|[^'\\])*')
    | (?P<NUM>0[xXbB][0-9a-fA-F']+[uUlLfF]*|(?:\d[\d']*\.?[\d']*|\.\d[\d']*)(?:[eE][+-]?\d+)?[uUlLfF]*)
    | (?P<NAME>[A-Za-z_]\w*)
    | (?P<OP>"""
    + _ops_alt(_C_OPS)
    + r"""|\S)
    """,
    re.VERBOSE,
)

_GENERIC_SCANNER = _C_SCANNER

LANGS: dict[str, LangSpec] = {
    "python": LangSpec("python", PY_KEYWORDS, PY_BUILTINS, _PY_SCANNER, "python"),
    "c": LangSpec("c", C_KEYWORDS, C_BUILTINS, _C_SCANNER, "c"),
    "cpp": LangSpec("cpp", C_KEYWORDS, C_BUILTINS, _C_SCANNER, "c"),
    "java": LangSpec("java", JAVA_KEYWORDS, GENERIC_BUILTINS, _GENERIC_SCANNER, "generic"),
    "javascript": LangSpec("javascript", JS_KEYWORDS, GENERIC_BUILTINS, _GENERIC_SCANNER, "generic"),
    "generic": LangSpec("generic", frozenset(), GENERIC_BUILTINS, _GENERIC_SCANNER, "generic"),
}

EXT_MAP = {
    ".py": "python", ".pyw": "python",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".c++": "cpp",
    ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".ts": "javascript", ".tsx": "javascript",
    ".cs": "generic", ".go": "generic", ".rs": "generic", ".kt": "generic",
    ".swift": "generic", ".scala": "generic", ".php": "generic", ".m": "generic",
}


def lang_for_path(path: str) -> str:
    dot = path.rfind(".")
    if dot == -1:
        return "generic"
    return EXT_MAP.get(path[dot:].lower(), "generic")


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def tokenize(text: str, lang: str = "generic") -> list[Token]:
    """Lex *text*, dropping whitespace, comments and preprocessor lines."""
    spec = LANGS.get(lang, LANGS["generic"])
    starts = _line_starts(text)
    out: list[Token] = []
    for m in spec.scanner.finditer(text):
        kind = m.lastgroup
        if kind in ("WS", "LCOM", "BCOM", "PP"):
            continue
        val = m.group()
        if kind == "NAME":
            k = KEYWORD if val in spec.keywords else NAME
        elif kind == "NUM":
            k = NUMBER
        elif kind in ("STR", "CHR"):
            k = STRING
        else:
            k = OP
        line = bisect.bisect_right(starts, m.start())
        out.append(Token(k, val, m.start(), m.end(), line))
    return out


# --- normalization -------------------------------------------------------

#: Canonicalize constructs that are trivially interchangeable, so a
#: for/while rewrite or an `else if` reshuffle does not break a match.
_CANON = {
    "while": "LOOP", "for": "LOOP", "do": "LOOP",
    "elif": "else if",
    "unsigned": "int", "signed": "int", "long": "int", "short": "int",
    "double": "float", "size_t": "int",
    "True": "BOOL", "False": "BOOL", "true": "BOOL", "false": "BOOL",
    "++": "+=", "--": "-=",
}


def normalize(tokens: Iterable[Token], spec_lang: str = "generic", *, canon: bool = True) -> list[str]:
    """Map tokens to the alphabet used for fingerprinting.

    Local identifiers collapse to ``V`` (so renaming is free), literals to
    ``N``/``S``, while keywords and known library names survive.
    """
    spec = LANGS.get(spec_lang, LANGS["generic"])
    toks = list(tokens)
    out: list[str] = []
    for i, t in enumerate(toks):
        if t.kind == NAME:
            # A known library name only counts as such in call or member
            # position. Otherwise `values`, `size`, `end` and friends would
            # survive as themselves when used as ordinary variable names,
            # and renaming would stop being free.
            nxt = toks[i + 1].value if i + 1 < len(toks) else ""
            prv = toks[i - 1].value if i else ""
            library = t.value in spec.builtins and (
                nxt == "(" or prv in (".", "->", "::")
            )
            out.append(t.value if library else "V")
        elif t.kind == NUMBER:
            out.append("N")
        elif t.kind == STRING:
            out.append("S")
        else:
            v = t.value
            if canon:
                v = _CANON.get(v, v)
            out.append(v)
    return out
