"""Edit scripts: the only language in which a change to a skill can be proposed (Step 2 design doc §5.3).

An edit script targets exactly one skill version (P2, locality) and is a list of primitive operations:

    target: Debug@1.0
    id: δ1                      # optional label
    ops:
      - {op: set_guard, src: test, dst: done, guard: "rep && suite"}

The semantics of the operations belong to the trusted kernel: `kernel.submit` re-applies the script itself, so a
meta skill can only say *what* to change, never hand over an already-edited skill.

Operations
    set_guard         src, dst, guard [, counter]
    add_edge          src, dst [, guard, counter, label]            (rewire)
    remove_edge       src, dst                                      (rewire)
    add_tool          id, fn [, reads, writes, ensures, state]
    add_agent         id, prompt [, reads, writes, contract, prior, state]
    add_call          id, skill [, bind_in, bind_out, state]
    remove_node       id          (aliases: remove_tool, remove_agent; incident edges are removed too)
    set_prompt        node, prompt
    set_contract      node [, pre, post, post_add]                  (for a tool, post / post_add edit `ensures`)
    set_call_version  node, skill                                   (e.g. "Debug >= 1.1")
    add_state         name, type
    set_budget        llm_calls
    set_loop_bound    counter, hi
    refine_interface  [pre, post_safe, post_live, inv, inputs, outputs]

`split` / `merge` from the design document are not primitive here: they are written as add / remove / rewire.
"""
from __future__ import annotations

import copy

from ..model import Skill, SkillFormatError, Version, load_skill
from ..types import parse_type

REQUIRED = {
    "set_guard": ("src", "dst", "guard"),
    "add_edge": ("src", "dst"),
    "remove_edge": ("src", "dst"),
    "add_tool": ("id", "fn"),
    "add_agent": ("id", "prompt"),
    "add_call": ("id", "skill"),
    "remove_node": ("id",),
    "remove_tool": ("id",),
    "remove_agent": ("id",),
    "set_prompt": ("node", "prompt"),
    "set_contract": ("node",),
    "set_call_version": ("node", "skill"),
    "add_state": ("name", "type"),
    "set_budget": ("llm_calls",),
    "set_loop_bound": ("counter", "hi"),
    "refine_interface": (),
}
# operations that introduce a reliability parameter with no observations yet (design doc §5.3, last column)
NEW_PARAMETER = {"set_prompt", "set_contract", "add_agent", "add_call", "set_call_version"}


class EditError(ValueError):
    pass


def parse_ref(ref) -> tuple[str, Version]:
    """"Debug@1.0" → ("Debug", Version(1, 0))."""
    if not isinstance(ref, str) or ref.count("@") != 1:
        raise EditError(f"invalid skill reference: {ref!r} (expected something like \"Debug@1.0\")")
    name, v = ref.split("@")
    try:
        return name, Version.parse(v)
    except SkillFormatError as ex:
        raise EditError(str(ex)) from ex


def ref_of(skill: Skill) -> str:
    return f"{skill.name}@{skill.version}"


def validate(script) -> None:
    """Shape check only: a single target, known operations, required fields present."""
    if not isinstance(script, dict):
        raise EditError("an edit script must be a mapping")
    parse_ref(script.get("target"))
    ops = script.get("ops")
    if not isinstance(ops, list) or not ops:
        raise EditError("an edit script needs a non-empty list of ops")
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in REQUIRED:
            raise EditError(f"op #{i}: unknown operation {op.get('op') if isinstance(op, dict) else op!r}")
        missing = [k for k in REQUIRED[op["op"]] if k not in op]
        if missing:
            raise EditError(f"op #{i} ({op['op']}): missing {missing}")


def op_names(script) -> list:
    return [op.get("op") for op in script.get("ops", []) if isinstance(op, dict)]


def new_parameters(script) -> int:
    return sum(1 for o in op_names(script) if o in NEW_PARAMETER)


def _edge(d, op):
    hits = [e for e in d.get("edges") or [] if e.get("src") == op["src"] and e.get("dst") == op["dst"]]
    if len(hits) != 1:
        raise EditError(f"{op['op']}: expected exactly one edge {op['src']} → {op['dst']}, found {len(hits)}")
    return hits[0]


def _node(d, nid, kinds=None):
    n = d["nodes"].get(nid)
    if n is None:
        raise EditError(f"node {nid} does not exist")
    if kinds and n.get("kind") not in kinds:
        raise EditError(f"node {nid} is a {n.get('kind')} node, expected one of {kinds}")
    return n


def _add_state(d, decl):
    state = d.setdefault("state", {}) or {}
    d["state"] = state
    for k, t in (decl or {}).items():
        if k in state and parse_type(state[k]) != parse_type(t):
            raise EditError(f"state variable {k} is already declared with a different type")
        state[k] = t


def _and(old, new):
    return new if old in (None, True, "true", "") else f"({old}) && ({new})"


def _apply_op(d, op):
    kind = op["op"]
    if kind == "set_guard":
        e = _edge(d, op)
        e["guard"] = op["guard"]
        if "counter" in op:
            e["counter"] = op["counter"]
    elif kind == "add_edge":
        e = {"src": op["src"], "dst": op["dst"], "guard": op.get("guard", "true")}
        e.update({k: op[k] for k in ("counter", "label") if k in op})
        d.setdefault("edges", []).append(e)
    elif kind == "remove_edge":
        d["edges"].remove(_edge(d, op))
    elif kind in ("add_tool", "add_agent", "add_call"):
        if op["id"] in d["nodes"]:
            raise EditError(f"{kind}: node {op['id']} already exists")
        keep = {"add_tool": ("fn", "reads", "writes", "ensures"),
                "add_agent": ("prompt", "reads", "writes", "contract", "prior", "fault_model"),
                "add_call": ("skill", "bind_in", "bind_out")}[kind]
        d["nodes"][op["id"]] = {"kind": kind[4:], **{k: copy.deepcopy(op[k]) for k in keep if k in op}}
        _add_state(d, op.get("state"))
    elif kind in ("remove_node", "remove_tool", "remove_agent"):
        _node(d, op["id"], {"remove_tool": ("tool",), "remove_agent": ("agent",)}.get(kind))
        if d.get("entry") == op["id"]:
            raise EditError(f"{kind}: cannot remove the entry node {op['id']}")
        del d["nodes"][op["id"]]
        d["edges"] = [e for e in d.get("edges") or [] if op["id"] not in (e.get("src"), e.get("dst"))]
    elif kind == "set_prompt":
        _node(d, op["node"], ("agent",))["prompt"] = op["prompt"]
    elif kind == "set_contract":
        n = _node(d, op["node"], ("agent", "tool"))
        if n["kind"] == "tool":
            if "pre" in op:
                raise EditError("set_contract: a tool node has no precondition")
            if "post" in op:
                n["ensures"] = op["post"]
            if "post_add" in op:
                n["ensures"] = _and(n.get("ensures"), op["post_add"])
        else:
            c = dict(n.get("contract") or {})
            c.update({k: op[k] for k in ("pre", "post") if k in op})
            if "post_add" in op:
                c["post"] = _and(c.get("post"), op["post_add"])
            n["contract"] = c
    elif kind == "set_call_version":
        _node(d, op["node"], ("call",))["skill"] = op["skill"]
    elif kind == "add_state":
        _add_state(d, {op["name"]: op["type"]})
    elif kind == "set_budget":
        d.setdefault("interface", {}).setdefault("budget", {})["llm_calls"] = op["llm_calls"]
    elif kind == "set_loop_bound":
        old = parse_type((d.get("state") or {}).get(op["counter"], ""))
        if old.kind != "int":
            raise EditError(f"set_loop_bound: {op['counter']} is not a bounded integer")
        d["state"][op["counter"]] = f"int[{old.lo}..{int(op['hi'])}]"
    elif kind == "refine_interface":
        i = d.setdefault("interface", {})
        for k in ("pre", "post_safe", "post_live", "inv"):
            if k in op:
                i[k] = copy.deepcopy(op[k])
        for k in ("inputs", "outputs"):
            if k in op:
                i[k] = {**(i.get(k) or {}), **op[k]}


def apply(skill: Skill, script: dict, version) -> Skill:
    """Apply an edit script to `skill` and return the edited skill under the given version. Pure: `skill` is untouched."""
    validate(script)
    name, ver = parse_ref(script["target"])
    if (name, ver) != (skill.name, skill.version):
        raise EditError(f"the script targets {script['target']}, not {ref_of(skill)}")
    if not skill.raw:
        raise EditError(f"{ref_of(skill)} has no source definition to edit")
    d = copy.deepcopy(skill.raw)
    try:
        for op in script["ops"]:
            _apply_op(d, op)
        d["version"] = str(Version.parse(version))
        return load_skill(d)
    except EditError:
        raise
    except (SkillFormatError, KeyError, TypeError, ValueError, AttributeError) as ex:
        raise EditError(f"the edited skill does not load: {ex}") from ex
