"""Tests for skillgraph: expressions and entailment, well-formedness W0–W8, execution and traces, cached replay, interface refinement and the library.

Run:  python -m pytest tests -q
"""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from examples import toy_world as W                                             # noqa: E402
from skillgraph import (TV, UNKNOWN, Executor, GateError, ScriptedBackend,      # noqa: E402
                        SkillFormatError, SkillLibrary, Version, check_refinement, check_skill,
                        llm_bound, load_skill, load_skills, parse, parse_type, show, skill_dot, trace_dot)
from skillgraph.expr import ExprError, evaluate                                 # noqa: E402
from skillgraph.logic import entails                                            # noqa: E402

SKILLS = ROOT / "examples" / "skills"
LOCATE = lambda view: {"F": ["date_utils.py"]}                                  # noqa: E731


def load(name):
    return load_skill(SKILLS / f"{name}.yaml")


def variant(skill, edit, version="9.0"):
    d = copy.deepcopy(skill.raw)
    d["version"] = version
    edit(d)
    return load_skill(d)


def patches(*ps):
    return [{"patch": p} for p in ps]


@pytest.fixture
def lib():
    lib = SkillLibrary()
    results = lib.add_all(load_skills(SKILLS))
    assert not [k for k, r in results.items() if isinstance(r, GateError)]
    return lib


def executor(lib, scripts):
    return Executor(lib, W.TOOLS, ScriptedBackend(scripts), W.PREDICATES)


# ---------------------------------------------------------------- types and expressions

def test_types():
    assert parse_type("bool").domain() == [False, True]
    assert parse_type("int[0..3]").domain() == [0, 1, 2, 3]
    assert parse_type("{low, mid, high}").values == ("low", "mid", "high")
    assert parse_type({"approve": None, "revise": None}).values == ("approve", "revise")   # unquoted in YAML
    assert len(parse_type("set{a, b}").domain()) == 4
    assert not parse_type("list[path]").decidable
    assert not parse_type("int[0..3]").contains(True) and not parse_type("int[0..3]").contains(4)


@pytest.mark.parametrize("src", [
    "rep && suite", "!(rep && suite) && k < 3", "a || b && c", "(a || b) && c", "a -> b -> c",
    "outcome == done -> rep(patch) && suite(patch)", "files(patch) ⊆ F && !touches_tests(patch)",
    "debug.outcome != done", "len(F) > 0", "x in {a, b}", "k + 1 <= 3", "!a == b",
])
def test_parse_roundtrip(src):
    e = parse(src)
    assert parse(show(e)) == e


def test_parse_errors():
    for bad in ["a &&", "a b", "(a", "a # b"]:
        with pytest.raises(ExprError):
            parse(bad)


def test_kleene():
    def ev(src, **env):
        return evaluate(parse(src), lambda n: env.get(n, n), lambda node, args: UNKNOWN)

    assert ev("a && sem(x)", a=False) is False             # false && ? = false
    assert ev("a && sem(x)", a=True) is UNKNOWN
    assert ev("a || sem(x)", a=True) is True
    assert ev("sem(x) -> a", a=True) is True
    assert ev("a -> sem(x)", a=False) is True
    assert ev("!sem(x)") is UNKNOWN
    assert ev("v == relevant", v="relevant") is True       # undeclared identifiers are enum literals
    assert ev("k + 1 < 3", k=1) is True


def test_entails():
    T = {"rep": parse_type("bool"), "suite": parse_type("bool"), "k": parse_type("int[0..3]"),
         "patch": parse_type("patch")}
    link = [parse("rep == ok_r(patch)"), parse("suite == ok_s(patch)")]
    assert entails(link + [parse("rep && suite")], parse("ok_r(patch) && ok_s(patch)"), T) is None
    cex = entails(link + [parse("rep")], parse("ok_r(patch) && ok_s(patch)"), T)
    assert cex["suite"] is False and cex["ok_s(patch)"] is False
    assert entails([parse("k < 2")], parse("k < 3"), T) is None
    assert entails([parse("k < 3")], parse("k < 2"), T) == {"k": 2}
    # semantic predicates are uninterpreted: only identical atoms are equal
    assert entails([parse("len(F) > 0")], parse("len(F) > 0"), T) is None
    assert entails([parse("len(F) > 0")], parse("len(F) > 1"), T) is not None


# ---------------------------------------------------------------- well-formedness

def test_example_skills_wellformed(lib):
    w8 = {s: lib.entry(*s).certificate.w8_proved for s in
          [("Debug", "1.0"), ("Debug", "1.1"), ("Coding", "1.0"), ("Coding", "1.1"),
           ("ReviewedPatch", "1.0"), ("WriteDocstring", "1.0")]}
    assert w8 == {("Debug", "1.0"): False, ("Debug", "1.1"): True, ("Coding", "1.0"): False,
                  ("Coding", "1.1"): True, ("ReviewedPatch", "1.0"): True, ("WriteDocstring", "1.0"): True}


def _fails(skill, lib=None):
    return check_skill(skill, lib.resolve if lib else None).failed()


@pytest.mark.parametrize("code, edit", [
    ("W0", lambda d: d["edges"][2].update(guard="passes_suite(patch, repo)")),       # semantic test on an edge
    ("W0", lambda d: d["edges"][2].update(guard="rep && sutie")),                    # misspelled variable
    ("W0", lambda d: d["nodes"]["write_patch"].update(writes=["patch", "k"])),       # node writes a counter
    ("W0", lambda d: d["nodes"]["write_patch"].update(writes=["patch", "issue"])),   # overwrites an input
    ("W0", lambda d: d["nodes"]["locate"]["contract"].update(post="len(patch) > 0")),  # contract references outside the read/write set
    ("W1", lambda d: d["edges"][3].update(guard="k < 2")),                           # overlap
    ("W1", lambda d: d["edges"].pop(4)),                                             # gap
    ("W2", lambda d: d["nodes"].update(orphan={"kind": "terminal", "outcome": "reject"})),
    ("W3", lambda d: d["edges"][3].pop("counter")),                                  # back edge without counter
    ("W3", lambda d: (d["edges"][3].update(guard="!(rep && suite)"),
                      d["edges"][4].update(guard="false"))),                         # counter can overflow
    ("W4", lambda d: d["nodes"]["locate"].update(reads=["issue", "repo", "patch"])),
    ("W5", lambda d: d["nodes"]["write_patch"]["contract"].update(post="files(patch) ⊆ F")),
    ("B", lambda d: d["interface"]["budget"].update(llm_calls=3)),
])
def test_illformed_variants(code, edit):
    assert code in _fails(variant(load("debug_v1_1"), edit))


def test_w4_done_must_write_outputs():
    # locate goes straight to done: output patch is unwritten
    assert "W4" in _fails(variant(load("debug_v1_1"), lambda d: d["edges"][0].update(dst="done")))


def test_w6_call_binding_and_pre(lib):
    coding = lib.get("Coding", "1.1")
    assert "W6" in _fails(variant(coding, lambda d: d["nodes"]["debug"].update(skill="Nope >= 1.0")), lib)
    assert "W6" in _fails(variant(coding, lambda d: d["nodes"]["debug"]["bind_in"].pop("repo")), lib)
    assert "W6" in _fails(variant(coding, lambda d: d["nodes"]["debug"]["bind_in"].update(repo="plan")), lib)
    # the caller cannot guarantee the callee's Pre: drop the failing_report half of the tool's ensures
    weak = variant(coding, lambda d: d["nodes"]["test"].update(
        ensures="all_pass == (passes_acceptance(patch, spec, repo) && passes_suite(patch, repo))"))
    r = check_skill(weak, lib.resolve)
    assert not r["W6"].ok and "Pre" in r["W6"].messages[0]


def test_w4_call_outputs_only_on_done(lib):
    # only the call node writes patch: readable after the outcome == done edge, not after the others
    d = {"skill": "Wrapper", "version": "1.0",
         "interface": {"inputs": {"issue": "issue", "repo": "repo"}, "outputs": {"patch": "patch"},
                       "pre": "has_repro_test(issue)", "inv": ["!touches_tests(patch)"]},
         "nodes": {"fix": {"kind": "call", "skill": "Debug >= 1.0", "bind_in": {"issue": "issue", "repo": "repo"},
                           "bind_out": {"patch": "patch"}},
                   "done": {"kind": "terminal", "outcome": "done"},
                   "give_up": {"kind": "terminal", "outcome": "give_up"}},
         "entry": "fix",
         "edges": [{"src": "fix", "dst": "done", "guard": "fix.outcome == done"},
                   {"src": "fix", "dst": "give_up", "guard": "fix.outcome != done"}]}
    assert check_skill(load_skill(d), lib.resolve).ok
    d["edges"][0]["guard"], d["edges"][1]["guard"] = "fix.outcome != reject", "fix.outcome == reject"
    assert _fails(load_skill(d), lib) == ["W4"]
    # at run time: a callee that gives up hands back no partial results
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D1_NOFIX, W.D1_NOFIX, W.D1_NOFIX)})
    d["edges"][0]["guard"], d["edges"][1]["guard"] = "fix.outcome == done", "fix.outcome != done"
    tr = ex.run(load_skill(d), {"issue": W.ISSUE, "repo": W.REPO})
    assert tr.outcome == "give_up" and tr.steps[0].output == {"fix.outcome": "give_up"}


def test_w7_call_cycle(lib):
    # make Debug call Coding back: Coding → Debug → Coding
    def edit(d):
        d["nodes"]["again"] = {"kind": "call", "skill": "Coding >= 1.0", "bind_in": {}, "bind_out": {}}
        d["edges"][4]["dst"] = "again"
        d["edges"].append({"src": "again", "dst": "give_up", "guard": "true"})
    assert "W7" in _fails(variant(lib.get("Debug", "1.1"), edit, "1.2"), lib)


def test_budget_bound(lib):
    assert llm_bound(lib.get("Debug", "1.1"), lib.resolve) == 4        # locate + 3 × write_patch
    assert llm_bound(lib.get("Coding", "1.0"), lib.resolve) == 10      # 2 + Debug's budget of 8
    assert llm_bound(lib.get("Coding", "1.1"), lib.resolve) == 26      # 2 + 3 × 8


def test_format_errors():
    with pytest.raises(SkillFormatError):
        variant(load("debug_v1_1"), lambda d: d["nodes"]["done"].update(outcome="finished"))
    with pytest.raises(SkillFormatError):
        variant(load("debug_v1_1"), lambda d: d["nodes"]["test"].update(kind="magic"))
    with pytest.raises(SkillFormatError):
        variant(load("debug_v1_1"), lambda d: None, version="one")


# ---------------------------------------------------------------- execution, traces, post-hoc evaluation

def run_debug(lib, version, *ps, issue=W.ISSUE):
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(*ps)})
    return ex, ex.run(lib.get("Debug", version), {"issue": issue, "repo": W.REPO})


def test_debug_v10_delivers_wrong_patch(lib):
    ex, tr = run_debug(lib, "1.0", W.D1_NOFIX, W.D2_REGRESS)
    assert tr.outcome == "done" and tr.outputs["patch"] == W.D2_REGRESS
    assert [s.node for s in tr.steps] == ["locate", "write_patch", "test", "write_patch", "test", "done"]
    assert tr.steps[0].contract_post is TV.U           # relevant(...) is undecided at run time
    assert tr.steps[1].contract_post is TV.T           # computable predicates are decided at run time
    ex.posthoc(tr)
    assert all(s.contract_post is TV.T for s in tr.steps[:-1])           # every step keeps its contract
    assert tr.post_eval == {"post_safe": TV.F, "post_live": TV.T, "inv": TV.T}
    assert tr.llm_calls == 3
    assert '"post_safe": "⊥"' in tr.to_json()


def test_debug_v11_retries_instead(lib):
    ex, tr = run_debug(lib, "1.1", W.D1_NOFIX, W.D2_REGRESS, W.D3_DAY_ONLY)
    ex.posthoc(tr)
    assert tr.outcome == "done" and tr.post_eval["post_safe"] is TV.T
    ex, tr = run_debug(lib, "1.1", W.D1_NOFIX, W.D2_REGRESS, W.D2_REGRESS)
    ex.posthoc(tr)
    assert tr.outcome == "give_up" and tr.outputs == {}
    assert tr.post_eval["post_safe"] is TV.T and tr.post_eval["post_live"] is TV.F


def test_reject_when_pre_false(lib):
    _, tr = run_debug(lib, "1.1", issue=W.ISSUE_NO_REPRO)
    assert (tr.outcome, tr.status, tr.pre, tr.llm_calls) == ("reject", "pre_violated", TV.F, 0)


def test_runtime_invariant_blocks_cheating(lib):
    _, tr = run_debug(lib, "1.1", W.D_CHEAT)
    assert (tr.outcome, tr.status, tr.outputs) == ("give_up", "inv_violation", {})
    assert tr.steps[-1].contract_post is TV.F and tr.steps[-1].inv is TV.F


def test_kernel_aborts(lib):
    debug = lib.get("Debug", "1.1")
    inputs = {"issue": W.ISSUE, "repo": W.REPO}
    # the agent writes a value of the wrong type
    ex = executor(lib, {"locate": LOCATE, "write_patch": [{"patch": W.D4_CORRECT, "rep": True}]})
    assert ex.run(debug, inputs).status == "type_error"
    # a tool raises
    ex = Executor(lib, {**W.TOOLS, "debug.run_tests": lambda **kw: 1 / 0},
                  ScriptedBackend({"locate": LOCATE, "write_patch": patches(W.D4_CORRECT)}), W.PREDICATES)
    assert ex.run(debug, inputs).status == "tool_error"
    # missing input
    with pytest.raises(ValueError):
        ex.run(debug, {"issue": W.ISSUE})


def test_nested_trace_and_caller_flaw(lib):
    scripts = {"understand": [{"plan": "validate month and day"}], "implement": patches(W.D1_NOFIX),
               "locate": LOCATE, "write_patch": patches(W.D2_REGRESS, W.D3_DAY_ONLY, W.D4_CORRECT)}
    inputs = {"spec": W.SPEC, "repo": W.REPO}

    ex = executor(lib, copy.deepcopy(scripts) | {"locate": LOCATE})
    tr = ex.posthoc(ex.run(lib.get("Coding", "1.0"), inputs))
    call = next(s for s in tr.steps if s.kind == "call")
    assert call.sub_trace.skill == "Debug" and call.sub_trace.version == "1.1"     # ">= 1.0" picks up 1.1 automatically
    assert call.output["debug.outcome"] == "done" and call.cost["llm_calls"] == 3
    assert call.contract_post is TV.T                   # Debug keeps its own Post
    assert tr.post_eval["post_safe"] is TV.F            # but Coding's Post does not hold
    assert tr.llm_calls == 5

    ex = executor(lib, copy.deepcopy(scripts) | {"locate": LOCATE})
    tr = ex.posthoc(ex.run(lib.get("Coding", "1.1"), inputs))
    assert tr.outcome == "done" and tr.post_eval["post_safe"] is TV.T
    assert sum(s.kind == "call" for s in tr.steps) == 2 and tr.outputs["patch"] == W.D4_CORRECT


def test_semantic_branch_and_single_node(lib):
    ex = executor(lib, {
        "implement": patches(W.D_MESSY, W.D4_CORRECT),
        "review": [{"verdict": "revise", "feedback": "remove the print"}, {"verdict": "approve", "feedback": ""}],
        "write": [{"doc": "parse_date: parse a date"}]})
    tr = ex.posthoc(ex.run("ReviewedPatch", {"spec": W.SPEC, "repo": W.REPO}))
    assert tr.outcome == "done" and tr.post_eval["post_safe"] is TV.T
    assert [s.node for s in tr.steps].count("implement") == 2
    tr = ex.posthoc(ex.run("WriteDocstring", {"code": "def parse_date(s): ..."}))
    assert tr.outcome == "done" and tr.post_eval["post_safe"] is TV.T
    assert ex.run("WriteDocstring", {"code": ""}).outcome == "reject"

    # the reviewer never approves: GiveUp once the counter runs out
    ex = executor(lib, {"implement": patches(W.D_MESSY, W.D_MESSY, W.D_MESSY),
                        "review": lambda view: {"verdict": "revise", "feedback": "still not good"}})
    tr = ex.run("ReviewedPatch", {"spec": W.SPEC, "repo": W.REPO})
    assert tr.outcome == "give_up" and tr.llm_calls == 6


# ---------------------------------------------------------------- cached replay

def test_replay_frontier(lib):
    ex, old = run_debug(lib, "1.0", W.D1_NOFIX, W.D2_REGRESS)
    rep = ex.replay(lib.get("Debug", "1.1"), old)
    assert not rep.exact and rep.frontier["node"] == "write_patch" and rep.frontier["t"] == 6
    assert [s.next for s in rep.steps[:5]] == ["write_patch", "test", "write_patch", "test", "write_patch"]
    assert rep.steps[4].output == {"rep": True, "suite": False}       # tool steps are recomputed
    assert rep.llm_calls == 3                                         # all three agent steps hit the cache and still count toward the budget
    with pytest.raises(ValueError):
        ex.posthoc(rep)


def test_replay_exact_when_no_new_agent_input(lib):
    ex, old = run_debug(lib, "1.1", W.D1_NOFIX, W.D4_CORRECT)
    same = ex.replay(lib.get("Debug", "1.1"), old)                    # same graph: the whole trace is reproduced exactly
    assert same.exact and same.outcome == "done" and same.outputs == old.outputs
    # only the loop bound changes: the first two attempts reuse the cache and stay exact
    tighter = variant(lib.get("Debug", "1.1"), lambda d: (
        d["state"].update(k="int[0..1]"),
        d["edges"][3].update(guard="!(rep && suite) && k < 1"),
        d["edges"][4].update(guard="!(rep && suite) && k >= 1")), "1.2")
    assert not check_skill(tighter).failed()
    rep = ex.replay(tighter, old)
    assert rep.exact and rep.outcome == "done"
    # prompt changed: the cache key changes, so the frontier is at this node
    reprompt = variant(lib.get("Debug", "1.1"), lambda d: d["nodes"]["write_patch"].update(prompt="new prompt"), "1.2")
    rep = ex.replay(reprompt, old)
    assert not rep.exact and (rep.frontier["node"], rep.frontier["t"]) == ("write_patch", 2)


def test_replay_through_call(lib):
    scripts = {"understand": [{"plan": "p"}], "implement": patches(W.D1_NOFIX),
               "locate": LOCATE, "write_patch": patches(W.D2_REGRESS, W.D3_DAY_ONLY)}
    ex = executor(lib, scripts)
    old = ex.run(lib.get("Coding", "1.0"), {"spec": W.SPEC, "repo": W.REPO})
    rep = ex.replay(lib.get("Coding", "1.1"), old)
    # the first Debug call comes entirely from the cache; after re-acceptance fails, the second Debug call has a new issue → frontier inside the sub-skill
    assert not rep.exact and rep.frontier["skill"] == "Debug" and rep.frontier["node"] == "locate"
    assert [s.node for s in rep.steps] == ["understand", "implement", "test", "debug", "test", "debug"]


# ---------------------------------------------------------------- interface refinement and the library

def test_refinement_rules(lib):
    v11 = lib.get("Debug", "1.1")
    assert check_refinement(lib.get("Debug", "1.0"), v11).compatible

    def bad(edit):
        return [name for name, ok, _ in check_refinement(v11, variant(v11, edit)).items if not ok]

    assert bad(lambda d: d["interface"].update(pre="has_repro_test(issue) && small(repo)")) == ["Pre may only weaken"]
    assert bad(lambda d: d["interface"].update(pre="true")) == []                       # weakening Pre is fine
    assert bad(lambda d: d["interface"].update(
        post_safe="outcome == done -> passes_repro(patch, issue, repo)")) == ["Post_safe may only strengthen"]
    assert bad(lambda d: d["interface"].update(post_safe=(
        "outcome == done -> passes_repro(patch, issue, repo) && passes_suite(patch, repo) && minimal(patch)"))) == []
    assert bad(lambda d: d["interface"].update(inv=[])) == ["Inv may only strengthen"]
    assert bad(lambda d: d["interface"]["inputs"].update(hint="text")) == ["X_in may not add required inputs"]
    assert bad(lambda d: d["interface"]["inputs"].update(hint={"type": "text", "default": ""})) == []
    assert bad(lambda d: d["interface"].update(outputs={"diff": "patch"})) == ["X_out may only grow"]
    assert check_refinement(v11, variant(v11, lambda d: d["interface"]["budget"].update(llm_calls=9))).budget_changed


def test_library_versioning(lib):
    v11 = lib.get("Debug", "1.1")

    def weaken(d):
        d["interface"]["post_safe"] = "outcome == done -> passes_repro(patch, issue, repo)"

    with pytest.raises(GateError, match="major version"):
        lib.add(variant(v11, weaken, "1.2"))
    assert lib.versions("Debug") == [Version(1, 0), Version(1, 1)]
    cert = lib.add(variant(v11, weaken, "2.0"))
    assert not cert.refines.compatible
    assert lib.resolve("Debug", Version(1, 0)).version == Version(1, 1)      # never crosses major versions
    assert lib.resolve("Debug").version == Version(2, 0)
    assert lib.resolve("Debug", Version(3, 0)) is None
    with pytest.raises(GateError, match="already in the library"):
        lib.add(v11)
    with pytest.raises(GateError, match="well-formedness"):
        lib.add(variant(v11, lambda d: d["edges"].pop(4), "1.2"))
    # after the upgrade callers need no change and still pass the check
    assert check_skill(lib.get("Coding", "1.1"), lib.resolve).ok


def test_budget_change_rechecks_callers(lib):
    # Debug's budget grows from 8 to 9: the rest of the interface is compatible, but Coding's budget no longer suffices → cannot be admitted as a minor version
    bigger = variant(lib.get("Debug", "1.1"), lambda d: d["interface"]["budget"].update(llm_calls=9), "1.2")
    with pytest.raises(GateError, match="budget"):
        lib.add(bigger)
    assert lib.versions("Debug") == [Version(1, 0), Version(1, 1)]           # rolled back
    smaller = variant(lib.get("Debug", "1.1"), lambda d: d["interface"]["budget"].update(llm_calls=5), "1.2")
    lib.add(smaller)
    assert lib.resolve("Debug", Version(1, 0)).version == Version(1, 2)


def test_stats_posterior(lib):
    ex, tr = run_debug(lib, "1.0", W.D1_NOFIX, W.D2_REGRESS)
    lib.record(tr)                                       # no post-hoc evaluation: locate's contract is ?, not counted
    st = lib.stats("Debug", "1.0")
    assert (st.reliability["locate"].a, st.reliability["write_patch"].a) == (1, 3)
    lib.record(ex.posthoc(tr))
    assert (st.reliability["locate"].a, st.post_safe.b, st.outcomes) == (2, 2, {"done": 2})
    assert len(st.history) == 2 and st.fault_models["write_patch"]["nofix"] == "1-a"
    # nested run: the sub-trace is recorded in the callee's own statistics layer
    ex = executor(lib, {"understand": [{"plan": "p"}], "implement": patches(W.D1_NOFIX),
                        "locate": LOCATE, "write_patch": patches(W.D3_DAY_ONLY)})
    lib.record(ex.posthoc(ex.run(lib.get("Coding", "1.0"), {"spec": W.SPEC, "repo": W.REPO})))
    assert lib.stats("Coding", "1.0").reliability["debug"].a == 2
    assert lib.stats("Debug", "1.1").outcomes == {"done": 1}


def test_viz_dot(lib):
    coding = lib.get("Coding", "1.1")
    d = skill_dot(coding)
    assert '"n:debug"' in d and "→ Debug ≥ 1.0" in d and "peripheries=2" in d
    assert "re-check" in d and "retry" in d and "not&#160;" in d and "&#160;and&#160;" in d     # edge names, readable guards
    ex = executor(lib, {"understand": [{"plan": "p"}], "implement": patches(W.D1_NOFIX),
                        "locate": LOCATE, "write_patch": patches(W.D3_DAY_ONLY)})
    tr = ex.run(lib.get("Coding", "1.0"), {"spec": W.SPEC, "repo": W.REPO})
    t = trace_dot(lib.get("Coding", "1.0"), tr, "run", {4: "run__t4_debug.svg"})
    assert "run__t4_debug.svg" in t and "rank=same" in t and "Finished:&#160;done" in t
