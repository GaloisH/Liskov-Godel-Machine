"""Skill data model and YAML loading (design doc §1–§5).

Interface layer = Interface; graph layer = state / nodes / edges / entry.
The statistics layer is not part of the skill definition; it lives in library.Stats, keyed by (name, version).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .expr import TRUE, parse
from .types import OUTCOME_T, OUTCOMES, Type, parse_type


class SkillFormatError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int = 0

    @staticmethod
    def parse(v) -> "Version":
        if isinstance(v, Version):
            return v
        m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?", str(v).strip())
        if not m:
            raise SkillFormatError(f"invalid version: {v!r} (expected something like \"1.0\")")
        return Version(int(m[1]), int(m[2] or 0))

    def __str__(self):
        return f"{self.major}.{self.minor}"


@dataclass
class Contract:
    pre: object = TRUE        # precondition A
    post: object = TRUE       # postcondition Q


@dataclass
class Node:
    id: str
    kind: str                                   # tool | agent | call | terminal
    reads: tuple = ()
    writes: tuple = ()
    contract: Contract = field(default_factory=Contract)   # agent contract; a tool's ensures also goes into post
    fn: str = ""                                # tool: name of the function to call
    prompt: str = ""                            # agent
    prior: tuple = (1.0, 1.0)                   # agent: Beta prior on reliability
    fault_model: dict = field(default_factory=dict)
    skill: str = ""                             # call: name of the callee skill
    min_version: Version = Version(0, 0)        # call: version lower bound
    bind_in: dict = field(default_factory=dict)     # callee input → caller variable
    bind_out: dict = field(default_factory=dict)    # callee output → caller variable
    outcome: str = ""                           # terminal

    @property
    def outcome_var(self) -> str:
        return f"{self.id}.outcome"


@dataclass
class Edge:
    src: str
    dst: str
    guard: object = TRUE
    counter: str | None = None      # loop counter on a back edge: incremented by the executor on each traversal
    label: str | None = None        # optional edge name (for drawing only), e.g. retry / re-check


@dataclass
class Interface:
    inputs: dict = field(default_factory=dict)       # X_in: name → type
    defaults: dict = field(default_factory=dict)     # defaults for optional inputs
    outputs: dict = field(default_factory=dict)      # X_out
    pre: object = TRUE
    post_safe: object = TRUE
    post_live: object = None
    inv: list = field(default_factory=list)
    budget: dict = field(default_factory=dict)       # only llm_calls is checked statically for now

    @property
    def types(self) -> dict:
        return {**self.inputs, **self.outputs, "outcome": OUTCOME_T}


@dataclass
class Skill:
    name: str
    version: Version
    interface: Interface
    state: dict                     # X: all blackboard variables → type (including inputs and outputs)
    nodes: dict                     # id → Node
    entry: str
    edges: list
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        return f"{self.name} v{self.version}"

    @property
    def types(self) -> dict:
        t = dict(self.state)
        for n in self.nodes.values():
            if n.kind == "call":
                t[n.outcome_var] = OUTCOME_T
        return t

    @property
    def counters(self) -> set:
        return {e.counter for e in self.edges if e.counter}

    def out_edges(self, v: str) -> list:
        return [e for e in self.edges if e.src == v]

    def in_edges(self, v: str) -> list:
        return [e for e in self.edges if e.dst == v]

    def calls(self) -> list:
        return [n for n in self.nodes.values() if n.kind == "call"]


_CALL = re.compile(r"^\s*(\w+)\s*(?:>=\s*v?([\d.]+))?\s*$")
_BETA = re.compile(r"^\s*Beta\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)\s*$")


def _prior(p):
    if isinstance(p, str):
        m = _BETA.match(p)
        if not m:
            raise SkillFormatError(f"invalid prior: {p!r} (expected something like Beta(1,1))")
        return float(m[1]), float(m[2])
    return float(p[0]), float(p[1])


def _node(nid: str, d: dict) -> Node:
    kind = d.get("kind")
    if kind not in ("tool", "agent", "call", "terminal"):
        raise SkillFormatError(f"node {nid}: unknown kind {kind!r}")
    n = Node(id=nid, kind=kind, reads=tuple(d.get("reads", ())), writes=tuple(d.get("writes", ())))
    c = d.get("contract") or {}
    n.contract = Contract(parse(c.get("pre")), parse(d.get("ensures", c.get("post"))))
    if kind == "tool":
        n.fn = d.get("fn", "")
        if not n.fn:
            raise SkillFormatError(f"tool node {nid}: missing fn")
    elif kind == "agent":
        n.prompt = str(d.get("prompt", ""))
        n.prior = _prior(d.get("prior", (1, 1)))
        n.fault_model = dict(d.get("fault_model", {}))
    elif kind == "call":
        m = _CALL.match(str(d.get("skill", "")))
        if not m:
            raise SkillFormatError(f"call node {nid}: skill should look like \"Debug >= 1.0\"")
        n.skill, n.min_version = m[1], Version.parse(m[2] or "0.0")
        n.bind_in, n.bind_out = dict(d.get("bind_in", {})), dict(d.get("bind_out", {}))
        n.reads = tuple(dict.fromkeys(n.bind_in.values()))
        n.writes = tuple(dict.fromkeys(n.bind_out.values())) + (n.outcome_var,)
    else:
        n.outcome = d.get("outcome", "")
        if n.outcome not in OUTCOMES:
            raise SkillFormatError(f"terminal {nid}: outcome must be one of {OUTCOMES}")
    return n


def load_skill(src) -> Skill:
    """Load one skill from a YAML file path, YAML text or a dict."""
    if isinstance(src, dict):
        d = src
    else:
        p = Path(str(src))
        text = p.read_text(encoding="utf-8") if "\n" not in str(src) and p.exists() else str(src)
        d = yaml.safe_load(text)
    try:
        i = d.get("interface") or {}
        inputs, defaults = {}, {}
        for k, spec in (i.get("inputs") or {}).items():
            if isinstance(spec, dict) and "type" in spec:
                inputs[k] = parse_type(spec["type"])
                if "default" in spec:
                    defaults[k] = spec["default"]
            else:
                inputs[k] = parse_type(spec)
        outputs = {k: parse_type(v) for k, v in (i.get("outputs") or {}).items()}
        state: dict[str, Type] = {}
        for src_map in (inputs, outputs, {k: parse_type(v) for k, v in (d.get("state") or {}).items()}):
            for k, t in src_map.items():
                if state.setdefault(k, t) != t:
                    raise SkillFormatError(f"conflicting type declarations for variable {k}: {state[k]} vs {t}")
        iface = Interface(
            inputs=inputs, defaults=defaults, outputs=outputs,
            pre=parse(i.get("pre")), post_safe=parse(i.get("post_safe")),
            post_live=parse(i.get("post_live", "outcome == done")),
            inv=[parse(x) for x in (i.get("inv") or [])], budget=dict(i.get("budget") or {}))
        nodes = {nid: _node(nid, nd) for nid, nd in d["nodes"].items()}
        edges = [Edge(e["src"], e["dst"], parse(e.get("guard")), e.get("counter"), e.get("label"))
                 for e in d.get("edges") or []]
        return Skill(name=d["skill"], version=Version.parse(d["version"]), interface=iface, state=state,
                     nodes=nodes, entry=d["entry"], edges=edges, raw=d)
    except KeyError as ex:
        raise SkillFormatError(f"skill definition is missing field {ex}") from ex


def load_skills(path) -> list[Skill]:
    """Load every *.yaml in a directory (one file may hold several skills separated by ---)."""
    out = []
    for p in sorted(Path(path).rglob("*.yaml")):
        for d in yaml.safe_load_all(p.read_text(encoding="utf-8")):
            if d:
                out.append(load_skill(d))
    return out
