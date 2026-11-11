"""边条件与契约共用的表达式语言：解析、打印、Kleene 三值求值（设计文档 §3.1、§4）。

语法（优先级从低到高）:
    e := e -> e | e || e | e && e | !e | a (== != < <= > >= in ⊆) a | a (+|-) a
    a := 整数 | true | false | 标识符 | f(e, ...) | {e, ...} | (e)

标识符如果是已声明的变量就取变量值，否则当作枚举字面量（如 `verdict == relevant`）。
`f(...)` 是谓词：可计算谓词由程序算，语义谓词交给 judge，判不出时得到 UNKNOWN。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Lit:
    value: object


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple


@dataclass(frozen=True)
class SetLit:
    items: tuple


@dataclass(frozen=True)
class Not:
    x: object


@dataclass(frozen=True)
class Bin:
    op: str
    l: object
    r: object


LOGIC = {"->", "||", "&&"}
CMP = {"==", "!=", "<", "<=", ">", ">=", "in", "⊆"}
ARITH = {"+", "-"}
_PREC = {"->": 1, "||": 2, "&&": 3, **{o: 5 for o in CMP}, "+": 6, "-": 6}
TRUE, FALSE = Lit(True), Lit(False)


class _Unknown:
    """三值逻辑里的 ?（判不出）。"""
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "?"

    def __bool__(self):
        raise ExprError("UNKNOWN 不能直接当布尔值用")


UNKNOWN = _Unknown()

# ---------------------------------------------------------------- 解析

_TOK = re.compile(
    r"\s*(?:(?P<int>\d+)|(?P<id>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
    r"|(?P<op>&&|\|\||->|==|!=|<=|>=|⊆|[!<>+\-(),{}]))")
_WORDS = {"and": "&&", "or": "||", "not": "!", "subset": "⊆", "in": "in"}


def _tokenize(src: str):
    src, pos, out = src.strip(), 0, []
    while pos < len(src):
        m = _TOK.match(src, pos)
        if not m:
            raise ExprError(f"无法解析表达式 {src!r}（位置 {pos}）")
        pos = m.end()
        if m["int"]:
            out.append(("int", int(m["int"])))
        elif m["id"]:
            w = m["id"]
            out.append(("op", _WORDS[w]) if w in _WORDS else ("id", w))
        else:
            out.append(("op", m["op"]))
    return out


class _Parser:
    def __init__(self, src):
        self.src, self.toks, self.i = src, _tokenize(src), 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def op(self, *ops):
        k, v = self.peek()
        if k == "op" and v in ops:
            self.i += 1
            return v
        return None

    def expect(self, o):
        if not self.op(o):
            raise ExprError(f"表达式 {self.src!r}: 期望 {o!r}")

    def parse(self):
        e = self.implies()
        if self.i != len(self.toks):
            raise ExprError(f"表达式 {self.src!r}: 多余的记号 {self.peek()[1]!r}")
        return e

    def implies(self):
        l = self.or_()
        return Bin("->", l, self.implies()) if self.op("->") else l

    def or_(self):
        l = self.and_()
        while self.op("||"):
            l = Bin("||", l, self.and_())
        return l

    def and_(self):
        l = self.not_()
        while self.op("&&"):
            l = Bin("&&", l, self.not_())
        return l

    def not_(self):
        return Not(self.not_()) if self.op("!") else self.cmp()

    def cmp(self):
        l = self.add()
        o = self.op(*CMP)
        return Bin(o, l, self.add()) if o else l

    def add(self):
        l = self.prim()
        while o := self.op("+", "-"):
            l = Bin(o, l, self.prim())
        return l

    def args(self, close):
        items = []
        if not self.op(close):
            items.append(self.implies())
            while self.op(","):
                items.append(self.implies())
            self.expect(close)
        return tuple(items)

    def prim(self):
        k, v = self.peek()
        self.i += 1
        if k == "int":
            return Lit(v)
        if k == "id":
            if v in ("true", "false"):
                return Lit(v == "true")
            return Call(v, self.args(")")) if self.op("(") else Var(v)
        if (k, v) == ("op", "("):
            e = self.implies()
            self.expect(")")
            return e
        if (k, v) == ("op", "{"):
            return SetLit(self.args("}"))
        if (k, v) == ("op", "-"):
            return Bin("-", Lit(0), self.prim())
        raise ExprError(f"表达式 {self.src!r}: 意外的记号 {v!r}")


@lru_cache(maxsize=None)
def _parse(src: str):
    return _Parser(src).parse()


def parse(src):
    """字符串 → 表达式。也接受 YAML 里没加引号的 true / false，以及已经解析好的表达式。"""
    if isinstance(src, (Lit, Var, Call, SetLit, Not, Bin)):
        return src
    if src is None or src is True:
        return TRUE
    if src is False:
        return FALSE
    return _parse(str(src))


# ---------------------------------------------------------------- 打印与遍历

def show(e, p: int = 0) -> str:
    if isinstance(e, Lit):
        return ("true" if e.value else "false") if isinstance(e.value, bool) else str(e.value)
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Call):
        return f"{e.fn}({', '.join(show(a) for a in e.args)})"
    if isinstance(e, SetLit):
        return "{" + ", ".join(show(a) for a in e.items) + "}"
    if isinstance(e, Not):
        s, q = "!" + show(e.x, 4), 4
    else:
        q = _PREC[e.op]
        lp, rp = (q + 1, q) if e.op == "->" else (q, q + 1) if e.op in LOGIC | ARITH else (q + 1, q + 1)
        s = f"{show(e.l, lp)} {e.op} {show(e.r, rp)}"
    return f"({s})" if q < p else s


def _walk(e):
    yield e
    if isinstance(e, Call):
        kids = e.args
    elif isinstance(e, SetLit):
        kids = e.items
    elif isinstance(e, Not):
        kids = (e.x,)
    elif isinstance(e, Bin):
        kids = (e.l, e.r)
    else:
        kids = ()
    for k in kids:
        yield from _walk(k)


def vars_of(e) -> set[str]:
    """表达式里出现的所有标识符（包括枚举字面量，由调用方按声明区分）。"""
    return {n.name for n in _walk(e) if isinstance(n, Var)}


def calls_of(e) -> set[str]:
    return {n.fn for n in _walk(e) if isinstance(n, Call)}


def subst(e, m: dict):
    """把变量替换成表达式（用于 Skill 调用的变量绑定）。"""
    if isinstance(e, Var):
        return m.get(e.name, e)
    if isinstance(e, Call):
        return Call(e.fn, tuple(subst(a, m) for a in e.args))
    if isinstance(e, SetLit):
        return SetLit(tuple(subst(a, m) for a in e.items))
    if isinstance(e, Not):
        return Not(subst(e.x, m))
    if isinstance(e, Bin):
        return Bin(e.op, subst(e.l, m), subst(e.r, m))
    return e


def conjuncts(e) -> list:
    """拆成合取项；`P -> (A && B)` 也拆成 `P -> A`、`P -> B`。"""
    if isinstance(e, Bin) and e.op == "&&":
        return conjuncts(e.l) + conjuncts(e.r)
    if isinstance(e, Bin) and e.op == "->":
        return [Bin("->", e.l, c) for c in conjuncts(e.r)]
    return [] if e == TRUE else [e]


def conj(es) -> object:
    es = list(es)
    if not es:
        return TRUE
    out = es[0]
    for x in es[1:]:
        out = Bin("&&", out, x)
    return out


# ---------------------------------------------------------------- 三值求值

def _as_bool(v, e):
    if v is UNKNOWN or isinstance(v, bool):
        return v
    raise ExprError(f"{show(e)} 不是布尔值: {v!r}")


def apply_op(op: str, l, r):
    """比较与算术运算（两边都已是具体值）。"""
    try:
        if op == "==":
            return l == r
        if op == "!=":
            return l != r
        if op == "<":
            return l < r
        if op == "<=":
            return l <= r
        if op == ">":
            return l > r
        if op == ">=":
            return l >= r
        if op == "in":
            return l in r
        if op == "⊆":
            return frozenset(l) <= frozenset(r)
        if op == "+":
            return l + r
        if op == "-":
            return l - r
    except TypeError as ex:
        raise ExprError(f"运算 {op} 的类型不匹配: {l!r}, {r!r}") from ex
    raise ExprError(f"未知运算符 {op}")


def evaluate(e, var, call):
    """Kleene 三值求值。

    var(name)        -> 变量值 / 枚举字面量 / UNKNOWN
    call(node, args) -> 谓词的值 / UNKNOWN
    """
    def ev(x):
        return evaluate(x, var, call)

    if isinstance(e, Lit):
        return e.value
    if isinstance(e, Var):
        return var(e.name)
    if isinstance(e, SetLit):
        items = [ev(i) for i in e.items]
        return UNKNOWN if any(i is UNKNOWN for i in items) else frozenset(items)
    if isinstance(e, Call):
        return call(e, [ev(a) for a in e.args])
    if isinstance(e, Not):
        v = _as_bool(ev(e.x), e.x)
        return UNKNOWN if v is UNKNOWN else not v
    if e.op in LOGIC:
        l = _as_bool(ev(e.l), e.l)
        if e.op == "->":
            l = UNKNOWN if l is UNKNOWN else not l
        short = e.op != "&&"            # || 和 -> 遇到 true 短路，&& 遇到 false 短路
        if l is short:
            return short
        r = _as_bool(ev(e.r), e.r)
        if r is short:
            return short
        return UNKNOWN if l is UNKNOWN or r is UNKNOWN else not short
    l, r = ev(e.l), ev(e.r)
    if l is UNKNOWN or r is UNKNOWN:
        return UNKNOWN
    return apply_op(e.op, l, r)
