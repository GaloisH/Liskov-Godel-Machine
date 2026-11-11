"""Render skill graphs and executions as SVG (Graphviz).

Two kinds of figures, both with English labels:

* **skill graph**  ``render_skill(skill, "out/Debug_v1.1.svg")`` — nodes (tool / agent / call /
  terminal) with a kind badge and their read / write sets; edges with a name (``retry``, ``done``,
  or the YAML ``label``) above the guard.
* **execution**    ``render_trace(skill, trace, "out/run1.svg", resolve=lib.get)`` — the same skill
  graph with one run drawn on top: taken edges are highlighted and carry numbered step badges,
  untaken parts are greyed out, and every visited node gets a note with the values it wrote at each
  step (changed values bold, unchanged ones grey, failed steps on a red background). Sub-skill
  calls are rendered as separate SVGs next to the parent (``run1__t4_debug.svg``).

Every blackboard variable keeps one colour across a figure — in read / write sets, guards and notes —
so data flow can be followed by colour. ``skill_dot`` / ``trace_dot`` return the DOT source;
rendering needs the Graphviz ``dot`` binary.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .expr import TRUE, show
from .trace import TV, Trace

FONT = "Arial,Microsoft YaHei,Segoe UI Symbol"   # fallbacks: CJK values, ✓ ✗ ━ ╌
MONO = "Consolas,Microsoft YaHei"

_KIND = {  # badge text, node fill, accent colour
    "tool": ("TOOL", "#eaf2fc", "#3b7dd8"),
    "agent": ("AGENT", "#fdf1e7", "#d8803b"),
    "call": ("CALL", "#edf7ea", "#4a9a3e"),
    "terminal": ("END", "#f2f2f2", "#777777"),
}
_TERM = {"done": ("#e3f4e1", "#2e7d32"), "give_up": ("#fbe5e3", "#c0392b"), "reject": ("#fbe5e3", "#c0392b")}
_MUTED = ("#f7f7f7", "#cccccc", "#aaaaaa")          # fill, border, text for parts a run did not touch
_TAKEN = "#1f4e8c"
_NOTE = ("#fffbe6", "#d4b106")
_OK, _BAD, _GREY, _TEXT = "#2e7d32", "#c0392b", "#888888", "#222222"
_FAIL_BG = "#fde2e1"

# one colour per variable (avoids pure red / green, which mean failure / success)
_PALETTE = ["#1864ab", "#862e9c", "#d9480f", "#0b7285", "#5c940d", "#a61e4d",
            "#e67700", "#364fc7", "#087f5b", "#5f3dc4", "#c2255c", "#495057"]

# words instead of ∧ ∨ ⇒: plainer to read, and Arial has no glyphs for them (spacing breaks in the SVG)
_OPS = [("->", "implies"), ("&&", "and"), ("||", "or"), ("!=", "≠"), (">=", "≥"), ("<=", "≤"), ("==", "="),
        ("!(", "not ("), ("!", "not ")]

_STOP = {
    "pre_violated": "precondition not met",
    "inv_violation": "invariant violated",
    "budget_exceeded": "LLM-call budget exceeded",
    "stuck": "no unique enabled edge",
    "type_error": "type error",
    "tool_error": "tool error",
    "frontier": "replay frontier",
}
_EVAL = {"post_safe": "safety", "post_live": "liveness", "inv": "invariant"}


# ---------------------------------------------------------------- text helpers

def _h(text) -> str:
    """Escape text for a Graphviz HTML-like label."""
    s = " ".join(str(text).split())
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return s.replace(" ", "&#160;")     # SVG drops leading / trailing spaces of each text run


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _font(text: str, size=None, color=None, bold=False, italic=False, face=None) -> str:
    if not text:
        return ""
    s = f"<I>{text}</I>" if italic else text
    s = f"<B>{s}</B>" if bold else s
    attrs = (f' POINT-SIZE="{size}"' if size else "") + (f' COLOR="{color}"' if color else "") \
        + (f' FACE="{face}"' if face else "")
    return f"<FONT{attrs}>{s}</FONT>" if attrs else s


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _pretty(expr) -> str:
    """Expression → readable text (not / and / or / implies, ≥ ≤ ≠ =)."""
    s = show(expr)
    for a, b in _OPS:
        s = s.replace(a, b)
    return s


def _summary(v, n: int = 36) -> str:
    """Short human-readable rendering of a blackboard value."""
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict):
        if isinstance(v.get("text"), str):                       # issue / spec: show the text
            return _clip('"' + " ".join(v["text"].split()) + '"', n)
        if v and all(isinstance(k, str) and ("." in k or "/" in k) for k in v):    # path → content
            files = list(v)
            return _clip(f"{len(files)} file{'s' if len(files) != 1 else ''}: " + ", ".join(files), n)
        return _clip("{" + ", ".join(map(str, v)) + "}", n)
    if isinstance(v, (set, frozenset)):
        return _clip("{" + ", ".join(_summary(x, 16) for x in sorted(v, key=str)) + "}", n)
    if isinstance(v, (list, tuple)):
        return _clip("[" + ", ".join(_summary(x, 16) for x in v) + "]", n)
    if isinstance(v, str):
        return _clip('"' + " ".join(v.split()) + '"', n)
    return _clip(repr(v), n)


def _badge(text: str, bg: str, fg: str = "#ffffff", size: float = 7.5) -> str:
    return (f'<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2" BGCOLOR="{bg}" STYLE="rounded">'
            f'<TR><TD>{_font(f"&#160;{text}&#160;", size, fg, bold=True)}</TD></TR></TABLE>')


def _step(t: int, size: float = 11) -> str:
    """Step number as a circled digit (①…⑳), plain '(t)' beyond 20."""
    text = chr(0x2460 + t - 1) if 1 <= t <= 20 else f"({t})"
    return _font(text, size, _TAKEN, bold=True)


def _steps(ts, size: float = 11) -> str:
    return "&#160;".join(_step(t, size) for t in ts)


def _badges(items) -> str:
    """A row of badges: items = [(text, bg), ...]."""
    # empty cells around the badges absorb extra space, so badges keep their natural size
    cells = "".join(f"<TD>{_badge(t, bg)}</TD>" for t, bg in items)
    return (f'<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="2" CELLPADDING="0"><TR><TD></TD>{cells}<TD></TD></TR>'
            f'<TR><TD COLSPAN="{len(items) + 2}"></TD></TR></TABLE>')


def _table(rows, spacing: int = 1, padding: int = 1) -> str:
    """rows = [(align, content)]; content may be text or a nested table."""
    cells = "".join(f'<TR><TD ALIGN="{a}" BALIGN="{a}">{c}</TD></TR>' for a, c in rows if c)
    return (f'<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="{spacing}" CELLPADDING="{padding}">'
            f"{cells}</TABLE>")


# ---------------------------------------------------------------- variable colours

class _Style:
    """Per-figure styling state: a fixed colour for every variable of the skill."""

    def __init__(self, skill):
        names = list(skill.interface.inputs) + list(skill.types) + list(skill.interface.outputs)
        names += sorted(skill.counters)
        self.color = {}
        for n in dict.fromkeys(names):
            self.color[n] = _PALETTE[len(self.color) % len(_PALETTE)]
        self._re = re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in sorted(self.color, key=len,
                                                                                    reverse=True))
                              + r")(?![\w.])") if self.color else None

    def var(self, name: str, muted=False, size=9, bold=False) -> str:
        return _font(_h(name), size, _MUTED[2] if muted else self.color.get(name, _TEXT), bold, face=MONO)

    def expr(self, text: str, muted=False) -> str:
        """Escaped expression text with variable names coloured."""
        s = _h(text)
        if muted or self._re is None:
            return s
        return self._re.sub(lambda m: f'<FONT COLOR="{self.color[m[1]]}">{m[1]}</FONT>', s)

    def var_list(self, arrow: str, names, muted=False, width: int = 34) -> str:
        """'← a, b, c' with coloured names, wrapped into lines."""
        lines, cur, length = [], [], 0
        for n in names:
            if cur and length + len(n) + 2 > width:
                lines.append(cur)
                cur, length = [], 0
            cur.append(n)
            length += len(n) + 2
        if cur:
            lines.append(cur)
        head = _font(arrow, 9, _MUTED[2] if muted else _GREY, bold=True)
        pad = "&#160;" * 4
        return "<BR/>".join((head if i == 0 else pad) + "&#160;" + ",&#160;".join(self.var(n, muted) for n in line)
                            for i, line in enumerate(lines))


# ---------------------------------------------------------------- nodes and edges

def _node_stmt(node, st: _Style, visited: bool | None) -> str:
    """visited=None → plain skill graph; True / False → execution view."""
    muted = visited is False
    badge_text, fill, accent = _KIND[node.kind]
    border = accent
    if node.kind == "terminal":
        fill, border = _TERM.get(node.outcome, (fill, accent))
    style = "rounded,filled" if node.kind != "tool" else "filled"
    if muted:
        fill, border = _MUTED[0], _MUTED[1]
        style += ",dashed"

    title = _font(_h(node.id), 13, _MUTED[2] if muted else _TEXT, bold=True)
    kind = _badges([(badge_text, _MUTED[1] if muted else accent)])
    rows = [("CENTER", title), ("CENTER", kind)]
    sub = _MUTED[2] if muted else _GREY
    if node.kind == "tool":
        rows.append(("CENTER", _font(_h(node.fn), 8.5, sub, face=MONO)))
    elif node.kind == "call":
        rows.append(("CENTER", _font(f"→ {_h(node.skill)} ≥ {node.min_version}", 9, sub)))
    if node.kind != "terminal":
        if node.reads:
            rows.append(("LEFT", st.var_list("←", node.reads, muted)))
        if node.writes:
            rows.append(("LEFT", st.var_list("→", node.writes, muted)))
    extra = " peripheries=2" if node.kind == "call" else ""
    pen = 2.4 if visited else 1.3
    return (f"  {_nid(node.id)} [shape=box style={_q(style)} fillcolor={_q(fill)} color={_q(border)} "
            f"penwidth={pen}{extra} label=<{_table(rows)}>];")


def _nid(name: str) -> str:
    return _q("n:" + name)


def _edge_name(edge, skill) -> str:
    """Explicit YAML label, else inferred: edges into a terminal take its outcome, counted edges are retries."""
    if edge.label:
        return edge.label
    dst = skill.nodes.get(edge.dst)
    if dst is not None and dst.kind == "terminal":
        return dst.outcome.replace("_", " ")
    if edge.counter:
        return "retry"
    return ""


def _edge_label(edge, skill, st: _Style, steps=None, muted=False) -> str:
    rows = []
    if steps:
        rows.append(("CENTER", _steps(steps)))
    name = _edge_name(edge, skill)
    if name:
        rows.append(("CENTER", _font(_h(name), 10, _MUTED[2] if muted else _TEXT, bold=True)))
    guard = []
    if edge.guard != TRUE:
        guard.append(st.expr(_pretty(edge.guard), muted))
    if edge.counter:
        guard.append(st.var(edge.counter, muted, 8.5) + _font("&#160;+1", 8.5, _MUTED[2] if muted else _GREY))
    if guard:
        rows.append(("CENTER", _font("<BR/>".join(guard), 8.5, _MUTED[2] if muted else "#555555")))
    return f"<{_table(rows, spacing=0)}>" if rows else ""


# ---------------------------------------------------------------- figure frame

def _legend(items) -> str:
    """items = [html, ...] laid out in one row."""
    cells = "".join(f'<TD ALIGN="LEFT">{x}</TD><TD WIDTH="10"></TD>' for x in items)
    return f'<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1"><TR>{cells}</TR></TABLE>'


def _skill_legend() -> str:
    items = [_badges([(_KIND[k][0], _KIND[k][2]) for k in ("agent", "tool", "call", "terminal")])]
    items += [_font("double border = calls a sub-skill", 8.5, _GREY),
              _font("← reads&#160;&#160;→ writes", 8.5, _GREY),
              _font("edge: <B>name</B> / guard", 8.5, _GREY)]
    return _legend(items)


def _trace_legend() -> str:
    items = [_font("━━ taken", 8.5, _TAKEN, bold=True), _font("╌╌ not taken", 8.5, _MUTED[2]),
             _step(3, 10) + _font("&#160;step", 8.5, _GREY),
             _font("✓ held", 8.5, _OK), _font("✗ broken", 8.5, _BAD), _font("? unchecked", 8.5, _GREY),
             _font("<B>bold</B> = new value", 8.5, _TEXT), _font("grey = unchanged", 8.5, _GREY)]
    return _legend(items)


def _header(title: str, subtitle: str, sentence: str, legend: str) -> list[str]:
    rows = [("LEFT", _font(_h(title), 16, _TEXT, bold=True) + (_font("&#160;&#160;" + _h(subtitle), 11, _GREY)
                                                               if subtitle else "")),
            ("LEFT", sentence), ("LEFT", legend)]
    return [
        "digraph G {",
        f'  graph [rankdir=TB fontname={_q(FONT)} labelloc=t labeljust=l nodesep=0.5 ranksep=0.6 '
        f'pad=0.3 label=<{_table(rows, spacing=3)}>];',
        f'  node [fontname={_q(FONT)} fontsize=11 margin="0.16,0.08"];',
        f'  edge [fontname={_q(FONT)} fontsize=9 color="#666666" arrowsize=0.7];',
        '  "start" [shape=circle label="" width=0.18 style=filled fillcolor="#444444" color="#444444"];',
    ]


# ---------------------------------------------------------------- skill graph

def skill_dot(skill) -> str:
    """DOT source of a skill graph."""
    st = _Style(skill)
    ins = ",&#160;".join(st.var(k, size=10) for k in skill.interface.inputs)
    outs = ",&#160;".join(st.var(k, size=10) for k in skill.interface.outputs)
    sentence = _font("inputs&#160;", 10, _GREY) + (ins or _font("none", 10, _GREY)) + \
        _font("&#160;&#160;→&#160;&#160;outputs&#160;", 10, _GREY) + (outs or _font("none", 10, _GREY))
    lines = _header(f"{skill.name} v{skill.version}", "skill graph", sentence, _skill_legend())
    for node in skill.nodes.values():
        lines.append(_node_stmt(node, st, None))
    lines.append(f'  "start" -> {_nid(skill.entry)};')
    for e in skill.edges:
        label = _edge_label(e, skill, st)
        lines.append(f"  {_nid(e.src)} -> {_nid(e.dst)}" + (f" [label={label}]" if label else "") + ";")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------- execution

def _run_sentence(trace: Trace) -> str:
    if trace.status == "frontier":
        head, color = "Stopped at replay frontier", "#b08800"
    elif trace.outcome == "done":
        head, color = "Finished: done", _OK
    elif trace.outcome == "reject":
        head, color = "Rejected — " + _STOP.get(trace.status, trace.status), _BAD
    elif trace.outcome == "give_up":
        why = _STOP.get(trace.status) if trace.status != "ok" else "retries exhausted"
        head, color = "Gave up — " + why, _BAD
    else:
        head, color = f"Ended: {trace.outcome or trace.status}", _GREY
    bits = [_font(_h(head), 11, color, bold=True)]
    n = trace.llm_calls
    bits.append(_font(f"{n} LLM call{'s' if n != 1 else ''}", 10, "#555555"))
    for k, v in (trace.post_eval or {}).items():
        if v is not None:
            mark, c = {TV.T: ("✓", _OK), TV.F: ("✗", _BAD), TV.U: ("?", _GREY)}[v]
            bits.append(_font(f"{_EVAL.get(k, k)}&#160;", 10, "#555555") + _font(mark, 10, c, bold=True))
    return _font("&#160;&#160;·&#160;&#160;", 10, _GREY).join(bits)


def trace_dot(skill, trace: Trace, name: str = "", sub_files: dict | None = None) -> str:
    """DOT source of one run drawn on top of ``skill``'s graph.

    ``sub_files`` maps a call step's ``t`` to the file name of its sub-run figure.
    """
    st = _Style(skill)
    sub_files = sub_files or {}
    steps = trace.steps
    visited = {s.node for s in steps}

    # which skill edge each transition used, and the step it led into
    taken: dict[int, list[int]] = {}
    for prev, cur in zip(steps, steps[1:]):
        for i, e in enumerate(skill.edges):
            if e.src == prev.node and e.dst == cur.node:
                taken.setdefault(i, []).append(cur.t)
                break

    lines = _header(f"{skill.name} v{skill.version}", f"run '{name}'" if name else "run",
                    _run_sentence(trace), _trace_legend())
    for node in skill.nodes.values():
        lines.append(_node_stmt(node, st, node.id in visited))

    # control edges
    if steps:
        start_label = f"<{_table([('CENTER', _step(1))], spacing=0)}>"
        lines.append(f'  "start" -> {_nid(skill.entry)} [color={_q(_TAKEN)} penwidth=2.4 label={start_label}];')
    else:
        lines.append(f'  "start" -> {_nid(skill.entry)} [color={_q(_MUTED[1])} style=dashed];')
    for i, e in enumerate(skill.edges):
        if i in taken:
            attrs = f"color={_q(_TAKEN)} penwidth=2.4 label={_edge_label(e, skill, st, taken[i])}"
        else:
            label = _edge_label(e, skill, st, muted=True)
            attrs = f"color={_q(_MUTED[1])} style=dashed" + (f" label={label}" if label else "")
        lines.append(f"  {_nid(e.src)} -> {_nid(e.dst)} [{attrs}];")

    # notes: inputs at the start, writes at each visited node, outputs at the terminal
    pre = None if trace.pre is None else (("✓ Pre met", _OK) if trace.pre is TV.T else
                                         ("✗ Pre not met", _BAD) if trace.pre is TV.F else ("? Pre", _GREY))
    rows = [(_font("inputs", 9, _TAKEN, bold=True), pre,
             [_assign(st, k, v, True) for k, v in trace.inputs.items()], [], trace.pre is TV.F)]
    lines += _note('"start"', "start", rows)

    state = dict(trace.inputs)
    per_node: dict[str, list] = {}
    for s in steps:
        per_node.setdefault(s.node, []).append(s)
    for nid, ss in per_node.items():
        rows = []
        for s in ss:
            badge = _step(s.t)
            if s.kind == "terminal":
                outs = [_assign(st, k, v, True) for k, v in trace.outputs.items()]
                ok = trace.outcome == "done"
                rows.append((badge, None, [_font("outcome = " + _h(trace.outcome), 9, _OK if ok else _BAD, True)]
                             + outs, [], False))
                continue
            vals = []
            for k, v in s.output.items():
                changed = k not in state or state[k] != v
                state[k] = v
                vals.append(_assign(st, k, v, changed))
            if not vals:
                vals.append(_font("no output", 9, _GREY, italic=True))
            notes = []
            if s.sub_trace is not None:
                sub = s.sub_trace
                notes.append(_font(f"sub-run: {_h(sub_files.get(s.t, '?'))} → {_h(sub.outcome or sub.status)}",
                                   8.5, "#555555", italic=True))
            failed = s.contract_post is TV.F or s.inv is TV.F
            node = skill.nodes[s.node]
            if s.contract_post is TV.F:
                what = "callee Post not met" if s.kind == "call" else "contract: " + _pretty(node.contract.post)
                notes.append(_font(_h(what), 8.5, _BAD))
            if s.inv is TV.F:
                notes += [_font("invariant: " + _h(_pretty(i)), 8.5, _BAD) for i in skill.interface.inv]
            rows.append((badge, _step_mark(s), vals, notes, failed))
        lines += _note(_nid(nid), nid, rows)

    # a run that did not reach a terminal ends in a stop marker
    if not steps or steps[-1].kind != "terminal":
        frontier = trace.status == "frontier"
        text = _STOP.get(trace.status, trace.status)
        if frontier and trace.frontier:
            text = f"{trace.frontier.get('skill', '?')}.{trace.frontier.get('node', '?')} needs a new agent output"
        fill, border = ("#fff6d6", "#c79a00") if frontier else ("#fbe5e3", _BAD)
        label = _table([("CENTER", _font("STOPPED", 11, border, bold=True)), ("CENTER", _font(_h(text), 10))])
        lines.append(f'  "stop" [shape=octagon style=filled fillcolor={_q(fill)} color={_q(border)} '
                     f'penwidth=1.6 label=<{label}>];')
        src = _nid(steps[-1].node) if steps else '"start"'
        lines.append(f'  {src} -> "stop" [style=dashed color={_q(border)} penwidth=1.6];')

    lines.append("}")
    return "\n".join(lines)


def _assign(st: _Style, k: str, v, changed: bool) -> str:
    """'name = value' with the name in its variable colour; unchanged values are greyed out."""
    if isinstance(v, bool) and changed:
        val = _font("true" if v else "false", 9, _OK if v else _BAD, bold=True)
    else:
        val = _font(_h(_summary(v)), 9, _TEXT if changed else _MUTED[2], bold=changed)
    name = st.var(k, muted=not changed)
    return name + _font("&#160;=&#160;", 9, _GREY) + val


def _step_mark(s):
    if s.inv is TV.F:
        return "✗ invariant", _BAD
    if s.contract_post is None:
        return None
    return {TV.T: ("✓ held", _OK), TV.F: ("✗ broken", _BAD), TV.U: ("? unchecked", _GREY)}[s.contract_post]


def _note(anchor_id: str, key: str, rows) -> list[str]:
    """A note beside ``anchor_id`` (same rank).

    rows = [(step_html, (mark, colour) | None, [value_html], [extra_html], failed)].
    """
    note_id = _q("note:" + key)
    cells = []
    for step, mark, vals, extra, failed in rows:
        bg = f' BGCOLOR="{_FAIL_BG}"' if failed else ""
        mark_html = _font(_h(mark[0]), 8.5, mark[1], bold=True) if mark else ""
        body = "<BR/>".join(vals + extra)
        cells.append(f'<TR><TD ALIGN="LEFT" VALIGN="TOP"{bg}>{step}</TD>'
                     f'<TD ALIGN="LEFT" VALIGN="TOP"{bg}>{mark_html}</TD>'
                     f'<TD ALIGN="LEFT" BALIGN="LEFT" VALIGN="TOP"{bg}>{body}</TD></TR>')
    table = f'<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="1" CELLPADDING="3">{"".join(cells)}</TABLE>'
    return [
        f"  {note_id} [shape=note style=filled fillcolor={_q(_NOTE[0])} color={_q(_NOTE[1])} "
        f'margin="0.06,0.03" label=<{table}>];',
        f"  {{ rank=same; {anchor_id}; {note_id}; }}",
        f"  {anchor_id} -> {note_id} [dir=none style=dotted color={_q(_NOTE[1])}];",
    ]


# ---------------------------------------------------------------- rendering

def _dot_binary() -> str:
    exe = shutil.which("dot")
    if exe is None:
        raise RuntimeError("Graphviz 'dot' not found on PATH (e.g. conda install -c conda-forge graphviz)")
    return exe


def to_svg(dot: str, path) -> Path:
    """Render DOT source to an SVG file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([_dot_binary(), "-Tsvg", "-o", str(path)], input=dot.encode("utf-8"),
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"dot failed for {path.name}: {proc.stderr.decode('utf-8', 'replace')}")
    return path


def render_skill(skill, path) -> Path:
    """Write the skill graph of ``skill`` to ``path`` (SVG)."""
    return to_svg(skill_dot(skill), path)


def render_trace(skill, trace: Trace, path, resolve=None, _depth: int = 0) -> list[Path]:
    """Write one run (and, recursively, its sub-runs) as SVGs; returns the written paths.

    ``resolve(name, version) -> Skill`` is needed when the run contains sub-skill calls.
    """
    path = Path(path)
    subs = {s.t: f"{path.stem}__t{s.t}_{s.node}.svg" for s in trace.steps if s.sub_trace is not None}
    out = [to_svg(trace_dot(skill, trace, path.stem, subs), path)]
    if _depth >= 8:                       # guard against runaway recursion
        return out
    for s in trace.steps:
        if s.sub_trace is None:
            continue
        if resolve is None:
            raise ValueError(f"{trace.skill} step t{s.t} calls a sub-skill; pass resolve= to render it")
        child = resolve(s.sub_trace.skill, s.sub_trace.version)
        out += render_trace(child, s.sub_trace, path.with_name(subs[s.t]), resolve, _depth + 1)
    return out
