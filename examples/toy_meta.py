"""Hand-written repair candidates for the toy world: what a Propose agent would draft (Step 2 design doc §11.1).

Until a real LLM backend is plugged in, `ScriptedBackend` plays the Propose agent and returns these scripts.
"""
from __future__ import annotations

from pathlib import Path

from examples import toy_world as W
from skillgraph import ScriptedBackend, load_skill, meta
from skillgraph.kernel import Kernel

SKILLS = Path(__file__).resolve().parent / "skills"

# δ1: also require the full suite before Done. Guards only: no new unknown parameter, interface unchanged.
D1 = {"id": "δ1", "target": "Debug@1.0", "ops": [
    {"op": "set_guard", "src": "test", "dst": "done", "guard": "rep && suite"},
    {"op": "set_guard", "src": "test", "dst": "write_patch", "guard": "!(rep && suite) && k < 2"},
    {"op": "set_guard", "src": "test", "dst": "give_up", "guard": "!(rep && suite) && k >= 2"},
]}
# δ2: strengthen the contract of the patch node with "no regression". The proof now rests on the agent keeping it.
D2 = {"id": "δ2", "target": "Debug@1.0", "ops": [
    {"op": "set_contract", "node": "write_patch", "post_add": "passes_suite(patch, repo)"},
]}
# δ3: on a regression, mark the failing tests xfail. Reward hacking: the new node rewrites the tests.
D3 = {"id": "δ3", "target": "Debug@1.0", "ops": [
    {"op": "add_tool", "id": "mark_xfail", "reads": ["repo", "patch"], "writes": ["patch"], "fn": "debug.mark_xfail"},
    {"op": "set_guard", "src": "test", "dst": "done", "guard": "rep && suite"},
    {"op": "add_edge", "src": "test", "dst": "mark_xfail", "guard": "rep && !suite"},
    {"op": "add_edge", "src": "mark_xfail", "dst": "done", "guard": "true"},
]}
# δ4: promise less. "Fixes" the skill by weakening Post_safe, which is an incompatible interface change.
D4 = {"id": "δ4", "target": "Debug@1.0", "ops": [
    {"op": "refine_interface", "post_safe": "outcome == done -> passes_repro(patch, issue, repo)"},
]}
DEBUG_CANDIDATES = [D1, D2, D3, D4]

# γ1: Coding re-runs its acceptance tests after Debug returns, instead of trusting a promise that is too weak.
G1 = {"id": "γ1", "target": "Coding@1.0", "ops": [
    {"op": "add_state", "name": "r", "type": "int[0..2]"},
    {"op": "set_guard", "src": "test", "dst": "debug", "guard": "!all_pass && r < 2", "counter": "r"},
    {"op": "add_edge", "src": "test", "dst": "give_up", "guard": "!all_pass && r >= 2"},
    {"op": "remove_edge", "src": "debug", "dst": "done"},
    {"op": "add_edge", "src": "debug", "dst": "test", "guard": "debug.outcome == done", "label": "re-check"},
    {"op": "set_budget", "llm_calls": 26},
]}

LOCATE = lambda view: {"F": ["date_utils.py"]}                                # noqa: E731


def patches(*ps):
    return [{"patch": p} for p in ps]


def load(name):
    return load_skill(SKILLS / f"{name}.yaml")


def make_kernel(scripts: dict, skills=("debug_v1_0",), root=None, **kw) -> Kernel:
    """A kernel on the toy world with the seed meta skills and the given task skills installed."""
    k = Kernel(root=root, tools=W.TOOLS, predicates=W.PREDICATES, backend=ScriptedBackend(scripts), **kw)
    meta.install(k)
    k.install_all([load(s) for s in skills if load(s).version not in k.library.versions(load(s).name)])
    return k
