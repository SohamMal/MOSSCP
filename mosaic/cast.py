"""C/C++ source -> normalized structure tree.

A full C++ parser is neither necessary nor desirable here: submissions are
often broken, and templates/macros make exact parsing miserable. This is a
tolerant structural parser with a real precedence-climbing expression
parser, which is enough to normalize the rewrites copiers actually use:

  * `for (i = 0; i < n; i++) B`  becomes  `i = 0; while (i < n) { B; i += 1; }`
    so a for/while rewrite produces an identical tree.
  * `switch` becomes a nested if/else chain on equality, so switch<->if
    rewrites match.
  * `x++`, `x += e` become `x = x + e`.
  * `a > b` becomes `b < a`.
  * Declarations lose their types: `int x = 5;` is just an assignment.
  * Statement and function order inside a block does not matter (the
    BLOCK label is hashed order-insensitively).
"""

from __future__ import annotations

from .structure import Node
from .tokens import C_BUILTINS, KEYWORD, NAME, NUMBER, OP, STRING, Token

_TYPE_WORDS = frozenset(
    """int char float double void short long signed unsigned bool auto const
    volatile static extern register inline struct class union enum size_t
    ssize_t uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t
    wchar_t char16_t char32_t constexpr mutable thread_local typename""".split()
)

_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

_BIN_NAME = {
    "+": "Add", "-": "Sub", "*": "Mult", "/": "Div", "%": "Mod",
    "<<": "LShift", ">>": "RShift", "&": "BitAnd", "|": "BitOr", "^": "BitXor",
}
_CMP_NAME = {"==": "Eq", "!=": "NotEq", "<": "Lt", "<=": "LtE", ">": "Gt", ">=": "GtE"}
_FLIP = {"Gt": "Lt", "GtE": "LtE"}

# binary precedence, higher binds tighter
_PREC = {
    "||": 1, "&&": 2, "|": 3, "^": 4, "&": 5,
    "==": 6, "!=": 6,
    "<": 7, ">": 7, "<=": 7, ">=": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
}

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}

_MAX_DEPTH = 120


class _Parser:
    def __init__(self, toks: list[Token]) -> None:
        self.t = toks
        self.n = len(toks)
        self.i = 0
        self.depth = 0

    # --- helpers ---------------------------------------------------------
    def peek(self, off: int = 0) -> Token | None:
        j = self.i + off
        return self.t[j] if 0 <= j < self.n else None

    def val(self, off: int = 0) -> str:
        tk = self.peek(off)
        return tk.value if tk else ""

    def line(self) -> int:
        tk = self.peek()
        return tk.line if tk else (self.t[-1].line if self.t else 0)

    def node(self, label: str, line: int | None = None) -> Node:
        ln = self.line() if line is None else line
        return Node(label, [], ln, ln)

    def eat(self, value: str) -> bool:
        if self.val() == value:
            self.i += 1
            return True
        return False

    def skip_group(self) -> tuple[int, int]:
        """At an opener, return (first_inner, index_of_closer) and move past it."""
        assert self.val() in _OPEN
        closer = _OPEN[self.val()]
        start = self.i + 1
        d = 0
        j = self.i
        while j < self.n:
            v = self.t[j].value
            if v in _OPEN:
                d += 1
            elif v in _CLOSE:
                d -= 1
                if d == 0:
                    break
            j += 1
        self.i = min(j + 1, self.n)
        return start, j if j < self.n else self.n

    def find_at_depth0(self, stops: set[str]) -> int:
        """Index of the first token in *stops* at brace/paren depth 0, or -1."""
        d = 0
        j = self.i
        while j < self.n:
            v = self.t[j].value
            if d == 0 and v in stops:
                return j
            if v in _OPEN:
                d += 1
            elif v in _CLOSE:
                if d == 0:
                    return -1
                d -= 1
            j += 1
        return -1

    # --- statements ------------------------------------------------------
    def parse_block_body(self, stop_index: int) -> Node:
        """Parse statements until *stop_index* (exclusive)."""
        blk = self.node("BLOCK")
        guard = 0
        while self.i < stop_index and self.i < self.n:
            before = self.i
            st = self.statement(stop_index)
            if st is not None:
                blk.add(st)
            if self.i == before:
                self.i += 1
            guard += 1
            if guard > 100_000:
                break
        return blk

    def braced_block(self) -> Node:
        """At '{', parse the block."""
        if self.val() != "{":
            # single-statement body
            st = self.statement(self.n)
            blk = self.node("BLOCK")
            blk.add(st)
            return blk
        d = 0
        j = self.i
        while j < self.n:
            v = self.t[j].value
            if v in _OPEN:
                d += 1
            elif v in _CLOSE:
                d -= 1
                if d == 0:
                    break
            j += 1
        self.i += 1
        blk = self.parse_block_body(j)
        self.i = min(j + 1, self.n)
        return blk

    def paren_expr(self) -> Node | None:
        if self.val() != "(":
            return None
        start, close = self.skip_group()
        return _Sub(self.t, start, close).expression()

    def statement(self, stop_index: int) -> Node | None:
        if self.depth > _MAX_DEPTH:
            self.i = stop_index
            return None
        tk = self.peek()
        if tk is None:
            return None
        v = tk.value
        ln = tk.line

        if v == ";":
            self.i += 1
            return None
        if v == "}":
            self.i += 1
            return None
        if v == "{":
            self.depth += 1
            try:
                return self.braced_block()
            finally:
                self.depth -= 1

        if tk.kind == KEYWORD or v in ("case", "default"):
            if v == "if":
                return self.if_stmt()
            if v == "while":
                return self.while_stmt()
            if v == "for":
                return self.for_stmt()
            if v == "do":
                return self.do_stmt()
            if v == "switch":
                return self.switch_stmt()
            if v == "return":
                self.i += 1
                end = self.find_at_depth0({";"})
                r = self.node("RET", ln)
                if end > self.i:
                    r.add(_Sub(self.t, self.i, end).expression())
                self.i = (end + 1) if end >= 0 else self.n
                return r
            if v in ("break", "continue", "goto"):
                self.i += 1
                end = self.find_at_depth0({";"})
                self.i = (end + 1) if end >= 0 else self.i
                return self.node("BREAK" if v != "continue" else "CONT", ln)
            if v in ("typedef", "using", "namespace", "template", "friend", "operator"):
                return self.skip_to_decl_end(ln, make="IMPORT" if v in ("using", "namespace") else None)
            if v in ("struct", "class", "union", "enum"):
                brace = self.find_at_depth0({"{"})
                semi = self.find_at_depth0({";"})
                if brace != -1 and (semi == -1 or brace < semi):
                    self.i = brace
                    c = self.node("CLASS", ln)
                    self.depth += 1
                    try:
                        c.add(self.braced_block())
                    finally:
                        self.depth -= 1
                    if self.val() == ";":
                        self.i += 1
                    return c
            if v in ("public", "private", "protected") and self.val(1) == ":":
                self.i += 2
                return None
            if v == "else":  # stray else
                self.i += 1
                return None
            if v in ("try", "catch"):
                self.i += 1
                if self.val() == "(":
                    self.skip_group()
                t = self.node("TRY", ln)
                if self.val() == "{":
                    self.depth += 1
                    try:
                        t.add(self.braced_block())
                    finally:
                        self.depth -= 1
                return t
            if v == "throw":
                self.i += 1
                end = self.find_at_depth0({";"})
                r = self.node("RAISE", ln)
                if end > self.i:
                    r.add(_Sub(self.t, self.i, end).expression())
                self.i = (end + 1) if end >= 0 else self.n
                return r

        if v in ("case", "default"):
            # only reached outside a switch; treat the label as noise
            end = self.find_at_depth0({":"})
            self.i = (end + 1) if end >= 0 else self.i + 1
            return None

        # function definition or plain statement
        semi = self.find_at_depth0({";"})
        brace = self.find_at_depth0({"{"})
        if brace != -1 and (semi == -1 or brace < semi) and self.looks_like_function(brace):
            return self.function(brace, ln)

        end = semi if semi != -1 else min(stop_index, self.n)
        expr = _Sub(self.t, self.i, end).declaration_or_expression()
        self.i = (end + 1) if semi != -1 else end
        return expr

    def skip_to_decl_end(self, ln: int, make: str | None = None) -> Node | None:
        brace = self.find_at_depth0({"{"})
        semi = self.find_at_depth0({";"})
        if brace != -1 and (semi == -1 or brace < semi):
            self.i = brace
            self.depth += 1
            try:
                blk = self.braced_block()
            finally:
                self.depth -= 1
            if self.val() == ";":
                self.i += 1
            node = self.node(make or "CLASS", ln)
            node.add(blk)
            return node
        self.i = (semi + 1) if semi != -1 else self.n
        return self.node(make, ln) if make else None

    def looks_like_function(self, brace: int) -> bool:
        """A declarator ending in ')' before '{', with no '=' in between."""
        j = brace - 1
        saw_paren = False
        while j >= self.i:
            v = self.t[j].value
            if v == ")":
                saw_paren = True
                break
            if v in ("=", ",", ";"):
                return False
            if self.t[j].kind not in (NAME, KEYWORD) and v not in (":", "&", "*", ">"):
                return False
            j -= 1
        if not saw_paren:
            return False
        # ensure there is a matching '(' at this level with a name before it
        d = 0
        while j >= self.i:
            v = self.t[j].value
            if v == ")":
                d += 1
            elif v == "(":
                d -= 1
                if d == 0:
                    return j - 1 >= self.i and self.t[j - 1].kind in (NAME, KEYWORD)
            j -= 1
        return False

    def function(self, brace: int, ln: int) -> Node:
        # locate the parameter list closing just before the brace
        j = brace - 1
        while j > self.i and self.t[j].value != ")":
            j -= 1
        close = j
        d = 0
        while j >= self.i:
            v = self.t[j].value
            if v == ")":
                d += 1
            elif v == "(":
                d -= 1
                if d == 0:
                    break
            j -= 1
        open_paren = j
        nargs = _count_params(self.t, open_paren + 1, close)
        f = self.node(f"FUNC/{nargs}", ln)
        self.i = brace
        self.depth += 1
        try:
            f.add(self.braced_block())
        finally:
            self.depth -= 1
        return f

    def if_stmt(self) -> Node:
        ln = self.line()
        self.i += 1
        br = self.node("BRANCH", ln)
        br.add(self.paren_expr())
        self.depth += 1
        try:
            br.add(self.body())
            if self.val() == "else":
                self.i += 1
                br.add(self.body())
        finally:
            self.depth -= 1
        return br

    def while_stmt(self) -> Node:
        ln = self.line()
        self.i += 1
        lp = self.node("LOOP", ln)
        lp.add(self.paren_expr())
        self.depth += 1
        try:
            lp.add(self.body())
        finally:
            self.depth -= 1
        return lp

    def do_stmt(self) -> Node:
        ln = self.line()
        self.i += 1
        self.depth += 1
        try:
            body = self.body()
        finally:
            self.depth -= 1
        cond = None
        if self.val() == "while":
            self.i += 1
            cond = self.paren_expr()
        if self.val() == ";":
            self.i += 1
        lp = self.node("LOOP", ln)
        lp.add(cond)
        lp.add(body)
        return lp

    def for_stmt(self) -> Node:
        """Rewrite as init; LOOP(cond, BLOCK(body, step)) -- matches while."""
        ln = self.line()
        self.i += 1
        init = cond = step = None
        if self.val() == "(":
            start, close = self.skip_group()
            parts = _split_depth0(self.t, start, close, ";")
            if len(parts) >= 3:
                a, b, c = parts[0], parts[1], parts[2]
                init = _Sub(self.t, *a).declaration_or_expression() if a[0] < a[1] else None
                cond = _Sub(self.t, *b).expression() if b[0] < b[1] else None
                step = _Sub(self.t, *c).expression() if c[0] < c[1] else None
            elif len(parts) == 1 and parts[0][0] < parts[0][1]:
                # range-for: for (auto x : xs)
                colon = _split_depth0(self.t, parts[0][0], parts[0][1], ":")
                if len(colon) == 2:
                    cond = _Sub(self.t, *colon[1]).expression()
        self.depth += 1
        try:
            body = self.body()
        finally:
            self.depth -= 1
        if step is not None:
            body.add(step)
        lp = self.node("LOOP", ln)
        lp.add(cond)
        lp.add(body)
        if init is None:
            return lp
        wrapper = self.node("BLOCK", ln)
        wrapper.add(init)
        wrapper.add(lp)
        return wrapper

    def switch_stmt(self) -> Node:
        """Rewrite as a nested if/else chain on equality -- matches if/else."""
        ln = self.line()
        self.i += 1
        subject_range: tuple[int, int] | None = None
        if self.val() == "(":
            start, close = self.skip_group()
            subject_range = (start, close)
        if self.val() != "{":
            body = self.body()
            br = self.node("BRANCH", ln)
            br.add(_Sub(self.t, *subject_range).expression() if subject_range else None)
            br.add(body)
            return br
        d = 0
        j = self.i
        while j < self.n:
            v = self.t[j].value
            if v in _OPEN:
                d += 1
            elif v in _CLOSE:
                d -= 1
                if d == 0:
                    break
            j += 1
        inner_start, inner_end = self.i + 1, j
        self.i = min(j + 1, self.n)
        groups = _switch_groups(self.t, inner_start, inner_end)
        chain: Node | None = None
        default_block: Node | None = None
        built: list[tuple[list[tuple[int, int]], Node]] = []
        for labels, (bs, be) in groups:
            sub = _Sub(self.t, bs, be)
            blk = sub.parse_block_body(be)
            while blk.children and blk.children[-1].label == "BREAK":
                blk.children.pop()
            if not labels:
                default_block = blk
            else:
                built.append((labels, blk))
        chain = default_block
        for labels, blk in reversed(built):
            br = self.node("BRANCH", blk.line or ln)
            cond: Node | None
            eqs = []
            for lab in labels:
                c = self.node("CMP/Eq", blk.line or ln)
                c.add(_Sub(self.t, *subject_range).expression() if subject_range else None)
                c.add(_Sub(self.t, *lab).expression())
                eqs.append(c)
            if len(eqs) == 1:
                cond = eqs[0]
            else:
                cond = self.node("BOOL/Or", blk.line or ln)
                for e in eqs:
                    cond.add(e)
            br.add(cond)
            br.add(blk)
            if chain is not None:
                # `else if` wraps its nested branch in a block, so the
                # rewritten switch must do the same or the two shapes differ
                br.add(chain if chain.label == "BLOCK" else _as_block(chain))
            chain = br
        return chain if chain is not None else self.node("BRANCH", ln)

    def body(self) -> Node:
        if self.val() == "{":
            return self.braced_block()
        blk = self.node("BLOCK")
        st = self.statement(self.n)
        if st is not None:
            blk.add(st)
        return blk

    # --- expressions -----------------------------------------------------
    def declaration_or_expression(self) -> Node | None:
        _strip_decl(self)
        if self.i >= self.n:
            return None
        e = self.expression()
        if e is None:
            return None
        if e.label == "VAR":  # bare declaration: `int x;`
            return None
        if e.label in ("ASSIGN", "CALL", "LOOP", "BRANCH"):
            return e
        w = Node("EXPR", [e], e.line, e.end_line)
        return w

    def expression(self) -> Node | None:
        parts = []
        while True:
            e = self.assignment()
            if e is not None:
                parts.append(e)
            if not self.eat(","):
                break
            if self.i >= self.n:
                break
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        seq = Node("SEQ", parts, parts[0].line, parts[-1].end_line)
        return seq

    def assignment(self) -> Node | None:
        left = self.ternary()
        if left is None:
            return None
        v = self.val()
        if v in _ASSIGN_OPS:
            ln = self.line()
            self.i += 1
            right = self.assignment()
            asn = Node("ASSIGN", [], ln, ln)
            asn.add(left)
            if v == "=":
                asn.add(right)
            else:  # x op= e  ==  x = x op e
                b = Node(f"BIN/{_BIN_NAME.get(v[:-1], 'Add')}", [], ln, ln)
                b.add(left)
                b.add(right)
                asn.add(b)
            return asn
        return left

    def ternary(self) -> Node | None:
        cond = self.binary(1)
        if cond is None:
            return None
        if self.val() == "?":
            ln = self.line()
            self.i += 1
            then = self.assignment()
            if self.val() == ":":
                self.i += 1
            other = self.assignment()
            br = Node("BRANCH", [], ln, ln)
            br.add(cond)
            br.add(then)
            br.add(other)
            return br
        return cond

    def binary(self, min_prec: int) -> Node | None:
        left = self.unary()
        if left is None:
            return None
        while True:
            v = self.val()
            prec = _PREC.get(v)
            if prec is None or prec < min_prec:
                return left
            ln = self.line()
            self.i += 1
            right = self.binary(prec + 1)
            if v in _CMP_NAME:
                name = _CMP_NAME[v]
                if name in _FLIP:  # a > b  ==>  b < a
                    left, right = right, left
                    name = _FLIP[name]
                node = Node(f"CMP/{name}", [], ln, ln)
            elif v == "&&":
                node = Node("BOOL/And", [], ln, ln)
            elif v == "||":
                node = Node("BOOL/Or", [], ln, ln)
            else:
                node = Node(f"BIN/{_BIN_NAME.get(v, v)}", [], ln, ln)
            node.add(left)
            node.add(right)
            left = node

    def unary(self) -> Node | None:
        tk = self.peek()
        if tk is None:
            return None
        v, ln = tk.value, tk.line
        if v in ("++", "--"):
            self.i += 1
            operand = self.unary()
            return _incdec(operand, v, ln)
        if v in ("!", "~"):
            self.i += 1
            u = Node("UN/Not", [], ln, ln)
            u.add(self.unary())
            return u
        if v == "-":
            self.i += 1
            u = Node("UN/USub", [], ln, ln)
            u.add(self.unary())
            return u
        if v == "+":
            self.i += 1
            return self.unary()
        if v in ("*", "&"):  # deref / address-of are not semantic here
            self.i += 1
            return self.unary()
        if v == "sizeof":
            self.i += 1
            if self.val() == "(":
                self.skip_group()
            else:
                self.unary()
            return Node("CONST/num", [], ln, ln)
        if v in ("new", "delete"):
            self.i += 1
            inner = self.unary()
            c = Node("CALL", [], ln, ln)
            c.add(Node(f"B/{v}", [], ln, ln))
            args = Node("ARGS", [], ln, ln)
            args.add(inner)
            c.add(args)
            return c
        return self.postfix()

    def postfix(self) -> Node | None:
        e = self.primary()
        if e is None:
            return None
        while self.i < self.n:
            v = self.val()
            ln = self.line()
            if v == "(":
                start, close = self.skip_group()
                c = Node("CALL", [], e.line or ln, ln)
                c.add(e)
                args = Node("ARGS", [], ln, ln)
                for a, b in _split_depth0(self.t, start, close, ","):
                    if a < b:
                        args.add(_Sub(self.t, a, b).assignment())
                c.add(args)
                e = c
            elif v == "[":
                start, close = self.skip_group()
                s = Node("SUB", [], e.line or ln, ln)
                s.add(e)
                s.add(_Sub(self.t, start, close).expression())
                e = s
            elif v in (".", "->", "::"):
                self.i += 1
                name = self.val()
                self.i += 1
                lbl = f"ATTR/{name}" if name in C_BUILTINS else "ATTR"
                a = Node(lbl, [], e.line or ln, ln)
                a.add(e)
                e = a
            elif v in ("++", "--"):
                self.i += 1
                e = _incdec(e, v, ln)
            else:
                break
        return e

    def primary(self) -> Node | None:
        tk = self.peek()
        if tk is None:
            return None
        v, ln = tk.value, tk.line
        if v == "(":
            start, close = self.skip_group()
            inner = _Sub(self.t, start, close).expression()
            return inner
        if v == "{":
            start, close = self.skip_group()
            seq = Node("SEQ", [], ln, ln)
            for a, b in _split_depth0(self.t, start, close, ","):
                if a < b:
                    seq.add(_Sub(self.t, a, b).assignment())
            return seq
        if tk.kind == NUMBER:
            self.i += 1
            return Node("CONST/num", [], ln, ln)
        if tk.kind == STRING:
            self.i += 1
            kind = "str" if v.lstrip("uUL8R").startswith('"') else "num"
            return Node(f"CONST/{kind}", [], ln, ln)
        if tk.kind in (NAME, KEYWORD):
            self.i += 1
            if v in ("true", "false"):
                return Node("CONST/bool", [], ln, ln)
            if v in ("nullptr", "NULL"):
                return Node("CONST/none", [], ln, ln)
            # swallow template arguments: vector<int>(...)
            if self.val() == "<":
                save = self.i
                close = _match_angle(self.t, self.i, self.n)
                if close != -1 and self.t[min(close + 1, self.n - 1)].value in ("(", "{", "::"):
                    self.i = close + 1
                else:
                    self.i = save
            # call position only, for the same reason as in tokens.normalize
            if v in C_BUILTINS and self.val() == "(":
                return Node(f"B/{v}", [], ln, ln)
            return Node("VAR", [], ln, ln)
        self.i += 1
        return None


class _Sub(_Parser):
    """A parser restricted to a token slice."""

    def __init__(self, toks: list[Token], start: int, end: int) -> None:
        super().__init__(toks)
        self.i = start
        self.n = min(end, len(toks))


def _as_block(node: Node) -> Node:
    return Node("BLOCK", [node], node.line, node.end_line)


def _incdec(operand: Node | None, op: str, ln: int) -> Node:
    """x++ / ++x  ==>  x = x + 1."""
    asn = Node("ASSIGN", [], ln, ln)
    asn.add(operand)
    b = Node(f"BIN/{'Add' if op == '++' else 'Sub'}", [], ln, ln)
    b.add(operand)
    b.add(Node("CONST/num", [], ln, ln))
    asn.add(b)
    return asn


def _match_angle(toks: list[Token], i: int, n: int) -> int:
    d = 0
    j = i
    while j < n and j - i < 64:
        v = toks[j].value
        if v == "<":
            d += 1
        elif v == ">":
            d -= 1
            if d == 0:
                return j
        elif v in (";", "{", ")"):
            return -1
        j += 1
    return -1


def _split_depth0(toks: list[Token], start: int, end: int, sep: str) -> list[tuple[int, int]]:
    parts: list[tuple[int, int]] = []
    d = 0
    s = start
    j = start
    while j < end:
        v = toks[j].value
        if v in _OPEN:
            d += 1
        elif v in _CLOSE:
            d -= 1
        elif v == sep and d == 0:
            parts.append((s, j))
            s = j + 1
        j += 1
    parts.append((s, end))
    return parts


def _count_params(toks: list[Token], start: int, end: int) -> int:
    if start >= end:
        return 0
    parts = _split_depth0(toks, start, end, ",")
    n = 0
    for a, b in parts:
        chunk = [t.value for t in toks[a:b]]
        if not chunk or chunk == ["void"]:
            continue
        n += 1
    return n


def _strip_decl(p: _Parser) -> None:
    """Drop leading type specifiers so `int x = 5;` is just an assignment."""
    moved = True
    while moved and p.i < p.n:
        moved = False
        v = p.val()
        if v in _TYPE_WORDS:
            p.i += 1
            moved = True
            continue
        tk = p.peek()
        if tk is not None and tk.kind == NAME:
            nxt = p.peek(1)
            if nxt is not None and nxt.kind in (NAME, KEYWORD) and nxt.value not in _TYPE_WORDS:
                p.i += 1
                moved = True
                continue
            if nxt is not None and nxt.value == "<":
                close = _match_angle(p.t, p.i + 1, p.n)
                if close != -1:
                    after = p.t[close + 1] if close + 1 < p.n else None
                    if after is not None and (after.kind == NAME or after.value in ("*", "&")):
                        p.i = close + 1
                        moved = True
                        continue
        if v in ("*", "&") and p.i > 0:
            prev = p.t[p.i - 1].value
            if prev in _TYPE_WORDS or (p.t[p.i - 1].kind == NAME and p.i >= 1):
                nxt = p.peek(1)
                if nxt is not None and nxt.kind == NAME:
                    p.i += 1
                    moved = True
                    continue


def build(tokens: list[Token]) -> Node | None:
    if not tokens:
        return None
    p = _Parser(tokens)
    root = Node("MODULE", [], 1, tokens[-1].line)
    guard = 0
    while p.i < p.n:
        before = p.i
        st = p.statement(p.n)
        if st is not None:
            root.add(st)
        if p.i == before:
            p.i += 1
        guard += 1
        if guard > 200_000:
            break
    return root


def _find_colon(toks: list[Token], start: int, end: int) -> int:
    d = 0
    j = start
    while j < end:
        v = toks[j].value
        if v in _OPEN:
            d += 1
        elif v in _CLOSE:
            d -= 1
        elif v == ":" and d == 0:
            return j
        elif v == ";" and d == 0:
            return j
        j += 1
    return end


def _switch_groups(
    toks: list[Token], start: int, end: int
) -> list[tuple[list[tuple[int, int]], tuple[int, int]]]:
    """Split a switch body into (case-label ranges, statement range) groups.

    Fallthrough labels (`case 1: case 2: stmts`) collapse into one group
    with several labels, which later becomes a single `||` condition.
    """
    marks: list[tuple[tuple[int, int] | None, int, int]] = []  # (label, kw_pos, body_start)
    d = 0
    j = start
    while j < end:
        v = toks[j].value
        if v in _OPEN:
            d += 1
        elif v in _CLOSE:
            d -= 1
        elif d == 0 and v == "case":
            colon = _find_colon(toks, j + 1, end)
            marks.append(((j + 1, colon), j, min(colon + 1, end)))
            j = min(colon + 1, end)
            continue
        elif d == 0 and v == "default":
            colon = _find_colon(toks, j + 1, end)
            marks.append((None, j, min(colon + 1, end)))
            j = min(colon + 1, end)
            continue
        j += 1

    groups: list[tuple[list[tuple[int, int]], tuple[int, int]]] = []
    pending: list[tuple[int, int]] = []
    for idx, (lab, _kw, body_start) in enumerate(marks):
        body_end = marks[idx + 1][1] if idx + 1 < len(marks) else end
        if body_start >= body_end:  # empty: falls through to the next label
            if lab is not None:
                pending.append(lab)
            else:
                pending.append((body_start, body_start))
            continue
        if lab is None:
            groups.append(([], (body_start, body_end)))
        else:
            labs = [p for p in pending if p[0] < p[1]] + [lab]
            groups.append((labs, (body_start, body_end)))
        pending = []
    return groups
