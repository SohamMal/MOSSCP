"""Python source -> normalized structure tree.

Normalization is where the detection power comes from. Everything that is
cheap for a copier to change is erased (names, literal values, docstrings,
annotations, `for` vs `while`, augmented assignment, comparison
direction, statement order within a block) while the shape of the
computation is kept.
"""

from __future__ import annotations

import ast

from .structure import Node
from .tokens import PY_BUILTINS

_FLIP = {"Gt": "Lt", "GtE": "LtE"}

#: Names bound somewhere in the current file. A parameter called `values`
#: or a local called `list` is a variable, not a library reference, so it
#: must still collapse to VAR -- otherwise renaming it would change the
#: tree and a renamed copy would stop matching.
_bound: frozenset[str] = frozenset()


def _collect_bound(tree: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return frozenset(names)


def _is_library(name: str) -> bool:
    return name in PY_BUILTINS and name not in _bound


def _n(label: str, node: ast.AST | None = None) -> Node:
    ln = getattr(node, "lineno", 0) or 0
    en = getattr(node, "end_lineno", 0) or ln
    return Node(label, [], ln, en)



def _advances(s: ast.stmt, idx: str) -> bool:
    """`i += 1` or `i = i + 1`."""
    if (
        isinstance(s, ast.AugAssign)
        and isinstance(s.target, ast.Name)
        and s.target.id == idx
        and isinstance(s.op, ast.Add)
    ):
        return True
    return (
        isinstance(s, ast.Assign)
        and len(s.targets) == 1
        and isinstance(s.targets[0], ast.Name)
        and s.targets[0].id == idx
        and isinstance(s.value, ast.BinOp)
        and isinstance(s.value.op, ast.Add)
        and isinstance(s.value.left, ast.Name)
        and s.value.left.id == idx
    )


def _counted_loop(s: ast.While) -> tuple[str, ast.expr, int] | None:
    """Recognize `i = 0; while i < len(seq): ... ; i += 1` as a for-loop.

    Hand-rolling an index loop is the standard way to disguise a Python
    `for`, so the two spellings must normalize to the same tree -- the
    same equivalence the C layer gets by rewriting `for` into `while`.
    """
    t = s.test
    if not (
        isinstance(t, ast.Compare)
        and len(t.ops) == 1
        and isinstance(t.ops[0], (ast.Lt, ast.LtE))
        and isinstance(t.left, ast.Name)
        and s.body
    ):
        return None
    idx = t.left.id
    # the advance may sit anywhere at the top level of the body, not only last
    bumps = [k for k, st in enumerate(s.body) if _advances(st, idx)]
    if len(bumps) != 1:
        return None
    bound = t.comparators[0]
    if (
        isinstance(bound, ast.Call)
        and isinstance(bound.func, ast.Name)
        and bound.func.id == "len"
        and bound.args
    ):
        return (idx, bound.args[0], bumps[0])
    return (idx, bound, bumps[0])


def _is_element_fetch(s: ast.stmt, idx: str) -> bool:
    """`item = seq[i]` -- the line a for-loop does not need."""
    return (
        isinstance(s, ast.Assign)
        and len(s.targets) == 1
        and isinstance(s.targets[0], ast.Name)
        and isinstance(s.value, ast.Subscript)
        and isinstance(s.value.slice, ast.Name)
        and s.value.slice.id == idx
    )


def _is_zero_init(s: ast.stmt, idx: str) -> bool:
    return (
        isinstance(s, ast.Assign)
        and len(s.targets) == 1
        and isinstance(s.targets[0], ast.Name)
        and s.targets[0].id == idx
        and isinstance(s.value, ast.Constant)
        and s.value.value == 0
    )


def _block(stmts: list[ast.stmt]) -> Node:
    b = _n("BLOCK")
    # drop the `i = 0` that only exists to drive a hand-rolled index loop
    skip: set[int] = set()
    for k, s in enumerate(stmts):
        if k == 0 or not isinstance(s, ast.While):
            continue
        info = _counted_loop(s)
        if info is not None and _is_zero_init(stmts[k - 1], info[0]):
            skip.add(k - 1)
    for k, s in enumerate(stmts):
        if k in skip:
            continue
        child = _stmt(s)
        if child is not None:
            b.add(child)
    return b


def _strip_doc(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _stmt(s: ast.stmt) -> Node | None:
    if isinstance(s, (ast.Pass, ast.Global, ast.Nonlocal)):
        return None

    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = s.args
        nargs = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs) + bool(a.vararg) + bool(a.kwarg)
        f = _n(f"FUNC/{nargs}", s)
        f.add(_block(_strip_doc(s.body)))
        return f

    if isinstance(s, ast.ClassDef):
        c = _n("CLASS", s)
        c.add(_block(_strip_doc(s.body)))
        return c

    if isinstance(s, (ast.For, ast.AsyncFor)):
        lp = _n("LOOP", s)
        lp.add(_expr(s.iter))
        lp.add(_block(s.body))
        if s.orelse:
            lp.add(_block(s.orelse))
        return lp

    if isinstance(s, ast.While):
        counted = _counted_loop(s)
        if counted is not None:
            idx, seq, bump = counted
            lp = _n("LOOP", s)
            lp.add(_expr(seq))
            inner = [
                st
                for k, st in enumerate(s.body)
                if k != bump and not _is_element_fetch(st, idx)
            ]
            lp.add(_block(inner))
            if s.orelse:
                lp.add(_block(s.orelse))
            return lp
        lp = _n("LOOP", s)
        lp.add(_expr(s.test))
        lp.add(_block(s.body))
        if s.orelse:
            lp.add(_block(s.orelse))
        return lp

    if isinstance(s, ast.If):
        br = _n("BRANCH", s)
        br.add(_expr(s.test))
        br.add(_block(s.body))
        if s.orelse:
            br.add(_block(s.orelse))
        return br

    if isinstance(s, ast.Assign):
        asn = _n("ASSIGN", s)
        for t in s.targets:
            asn.add(_expr(t))
        asn.add(_expr(s.value))
        return asn

    if isinstance(s, ast.AnnAssign):
        asn = _n("ASSIGN", s)
        asn.add(_expr(s.target))
        if s.value is not None:
            asn.add(_expr(s.value))
        return asn

    if isinstance(s, ast.AugAssign):
        # x += e  ==  x = x + e, so the two spellings hash alike
        asn = _n("ASSIGN", s)
        asn.add(_expr(s.target))
        b = _n(f"BIN/{type(s.op).__name__}", s)
        b.add(_expr(s.target))
        b.add(_expr(s.value))
        asn.add(b)
        return asn

    if isinstance(s, ast.Return):
        r = _n("RET", s)
        if s.value is not None:
            r.add(_expr(s.value))
        return r

    if isinstance(s, ast.Expr):
        e = _n("EXPR", s)
        e.add(_expr(s.value))
        return e

    if isinstance(s, (ast.Import, ast.ImportFrom)):
        return _n("IMPORT", s)

    if isinstance(s, ast.Break):
        return _n("BREAK", s)
    if isinstance(s, ast.Continue):
        return _n("CONT", s)

    if isinstance(s, ast.Raise):
        r = _n("RAISE", s)
        if s.exc is not None:
            r.add(_expr(s.exc))
        return r

    if isinstance(s, ast.Assert):
        a = _n("ASSERT", s)
        a.add(_expr(s.test))
        return a

    if isinstance(s, ast.Delete):
        return _n("DEL", s)

    if isinstance(s, (ast.With, ast.AsyncWith)):
        wi = _n("WITH", s)
        for item in s.items:
            wi.add(_expr(item.context_expr))
        wi.add(_block(s.body))
        return wi

    if isinstance(s, ast.Try):
        t = _n("TRY", s)
        t.add(_block(s.body))
        for h in s.handlers:
            t.add(_block(h.body))
        if s.orelse:
            t.add(_block(s.orelse))
        if s.finalbody:
            t.add(_block(s.finalbody))
        return t

    if isinstance(s, ast.Match):
        m = _n("BRANCH", s)
        m.add(_expr(s.subject))
        for case in s.cases:
            m.add(_block(case.body))
        return m

    generic = _n("STMT", s)
    for child in ast.iter_child_nodes(s):
        if isinstance(child, ast.expr):
            generic.add(_expr(child))
        elif isinstance(child, ast.stmt):
            sub = _stmt(child)
            if sub is not None:
                generic.add(sub)
    return generic


def _expr(e: ast.expr | None) -> Node | None:
    if e is None:
        return None

    if isinstance(e, ast.Name):
        return _n(f"B/{e.id}" if _is_library(e.id) else "VAR", e)

    if isinstance(e, ast.Constant):
        v = e.value
        if v is None:
            t = "none"
        elif isinstance(v, bool):
            t = "bool"
        elif isinstance(v, (int, float, complex)):
            t = "num"
        elif isinstance(v, str):
            t = "str"
        else:
            t = "other"
        return _n(f"CONST/{t}", e)

    if isinstance(e, ast.Attribute):
        a = _n(f"ATTR/{e.attr}" if e.attr in PY_BUILTINS else "ATTR", e)
        a.add(_expr(e.value))
        return a

    if isinstance(e, ast.Call):
        c = _n("CALL", e)
        c.add(_expr(e.func))
        args = _n("ARGS", e)
        for a in e.args:
            args.add(_expr(a))
        kw = _n("SET", e)  # keyword arguments are order-insensitive
        for k in e.keywords:
            kw.add(_expr(k.value))
        if kw.children:
            args.add(kw)
        c.add(args)
        return c

    if isinstance(e, ast.BinOp):
        b = _n(f"BIN/{type(e.op).__name__}", e)
        b.add(_expr(e.left))
        b.add(_expr(e.right))
        return b

    if isinstance(e, ast.UnaryOp):
        u = _n(f"UN/{type(e.op).__name__}", e)
        u.add(_expr(e.operand))
        return u

    if isinstance(e, ast.BoolOp):
        b = _n(f"BOOL/{type(e.op).__name__}", e)
        for v in e.values:
            b.add(_expr(v))
        return b

    if isinstance(e, ast.Compare):
        # canonicalize a > b into b < a
        if len(e.ops) == 1 and type(e.ops[0]).__name__ in _FLIP:
            op = _FLIP[type(e.ops[0]).__name__]
            c = _n(f"CMP/{op}", e)
            c.add(_expr(e.comparators[0]))
            c.add(_expr(e.left))
            return c
        c = _n("CMP/" + ",".join(type(o).__name__ for o in e.ops), e)
        c.add(_expr(e.left))
        for v in e.comparators:
            c.add(_expr(v))
        return c

    if isinstance(e, ast.Subscript):
        s = _n("SUB", e)
        s.add(_expr(e.value))
        if isinstance(e.slice, ast.expr):
            s.add(_expr(e.slice))
        return s

    if isinstance(e, (ast.List, ast.Tuple)):
        seq = _n("SEQ", e)
        for el in e.elts:
            seq.add(_expr(el))
        return seq

    if isinstance(e, ast.Set):
        seq = _n("SET", e)
        for el in e.elts:
            seq.add(_expr(el))
        return seq

    if isinstance(e, ast.Dict):
        m = _n("MAP", e)
        for k, v in zip(e.keys, e.values):
            pair = _n("PAIR", e)
            pair.add(_expr(k))
            pair.add(_expr(v))
            m.add(pair)
        return m

    if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        # a comprehension is a loop that accumulates
        lp = _n("LOOP", e)
        for gen in e.generators:
            lp.add(_expr(gen.iter))
            for cond in gen.ifs:
                br = _n("BRANCH", e)
                br.add(_expr(cond))
                lp.add(br)
        body = _n("BLOCK")
        if isinstance(e, ast.DictComp):
            body.add(_expr(e.key))
            body.add(_expr(e.value))
        else:
            body.add(_expr(e.elt))
        lp.add(body)
        return lp

    if isinstance(e, ast.Lambda):
        a = e.args
        nargs = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
        f = _n(f"FUNC/{nargs}", e)
        body = _n("BLOCK")
        r = _n("RET", e)
        r.add(_expr(e.body))
        body.add(r)
        f.add(body)
        return f

    if isinstance(e, ast.IfExp):
        br = _n("BRANCH", e)
        br.add(_expr(e.test))
        br.add(_expr(e.body))
        br.add(_expr(e.orelse))
        return br

    if isinstance(e, ast.JoinedStr):
        j = _n("CONST/str", e)
        return j

    if isinstance(e, ast.Starred):
        return _expr(e.value)

    if isinstance(e, (ast.Yield, ast.YieldFrom, ast.Await)):
        y = _n("RET", e)
        if getattr(e, "value", None) is not None:
            y.add(_expr(e.value))
        return y

    if isinstance(e, ast.Slice):
        s = _n("SLICE", e)
        for part in (e.lower, e.upper, e.step):
            s.add(_expr(part))
        return s

    g = _n("EXPR", e)
    for child in ast.iter_child_nodes(e):
        if isinstance(child, ast.expr):
            g.add(_expr(child))
    return g


def build(source: str) -> Node | None:
    """Return the normalized tree, or None if the file does not parse."""
    global _bound
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    _bound = _collect_bound(tree)
    root = Node("MODULE", [], 1, source.count("\n") + 1)
    for s in _strip_doc(tree.body):
        child = _stmt(s)
        if child is not None:
            root.add(child)
    return root
