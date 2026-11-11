"""Tests for the kernel and the seed meta skills (Step 2 design doc, implementation steps 1–5).

Run:  python -m pytest tests -q
"""
import copy
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from examples import toy_meta as M                                              # noqa: E402
from examples import toy_world as W                                             # noqa: E402
from skillgraph import TV, Predicates, ScriptedBackend, Trace, check_skill, meta   # noqa: E402
from skillgraph.kernel import (EditError, Kernel, Splits, apply, parse_ref,     # noqa: E402
                               virtual_trace, w8_counterexample)
from skillgraph.meta.tools import _blame                                        # noqa: E402

CODING = {"understand": lambda v: {"plan": "validate month and day"},
          "implement": lambda v: {"patch": W.D1_NOFIX}, "locate": M.LOCATE}
ISSUE = {"issue": W.ISSUE, "repo": W.REPO}
SPEC = {"spec": W.SPEC, "repo": W.REPO}
# sends every run to give_up: makes W8 trivially provable, and must never be admitted
GIVE_UP = {"id": "δ5", "target": "Debug@1.0", "ops": [
    {"op": "set_guard", "src": "test", "dst": "done", "guard": "false"},
    {"op": "set_guard", "src": "test", "dst": "write_patch", "guard": "k < 2"},
    {"op": "set_guard", "src": "test", "dst": "give_up", "guard": "k >= 2"}]}


def draft(*cand_lists):
    return [{"cands": list(c)} for c in cand_lists]


def ticket(k, kind, target=None):
    return next(t for t in k.tickets.values() if t.kind == kind and target in (None, t.target_hint))


def verdicts(rec):
    return {c["id"]: c["verdict"] for c in rec["candidates"]}


# ---------------------------------------------------------------- edit scripts

def test_edit_script_gives_debug_v1_1():
    v10, v11 = M.load("debug_v1_0"), M.load("debug_v1_1")
    new = apply(v10, M.D1, "1.1")
    assert [(e.src, e.dst, e.guard) for e in new.edges] == [(e.src, e.dst, e.guard) for e in v11.edges]
    assert new.version == v11.version and v10.edges[2].guard != new.edges[2].guard      # the original is untouched


def test_edit_operations():
    v10 = M.load("debug_v1_0")
    s = apply(v10, {"target": "Debug@1.0", "ops": [
        {"op": "add_tool", "id": "lint", "fn": "x.lint", "reads": ["patch"], "writes": ["clean"], "state": {"clean": "bool"}},
        {"op": "remove_edge", "src": "write_patch", "dst": "test"},
        {"op": "add_edge", "src": "write_patch", "dst": "lint"},
        {"op": "add_edge", "src": "lint", "dst": "test"},
        {"op": "set_prompt", "node": "locate", "prompt": "new prompt"},
        {"op": "set_contract", "node": "test", "post_add": "rep || !rep"},
        {"op": "set_loop_bound", "counter": "k", "hi": 4},
        {"op": "set_budget", "llm_calls": 12}]}, "1.1")
    assert s.nodes["lint"].kind == "tool" and s.state["clean"].kind == "bool"
    assert s.nodes["locate"].prompt == "new prompt" and s.state["k"].hi == 4 and s.interface.budget == {"llm_calls": 12}
    assert "lint" not in apply(s, {"target": "Debug@1.1", "ops": [{"op": "remove_node", "id": "lint"}]}, "1.2").nodes


@pytest.mark.parametrize("script", [
    {"target": "Debug@1.0", "ops": []},
    {"target": "Debug", "ops": [{"op": "set_budget", "llm_calls": 1}]},
    {"target": "Debug@1.0", "ops": [{"op": "delete_tests"}]},
    {"target": "Debug@1.0", "ops": [{"op": "set_guard", "src": "test", "dst": "done"}]},
    {"target": "Debug@1.0", "ops": [{"op": "set_guard", "src": "test", "dst": "nowhere", "guard": "rep"}]},
    {"target": "Coding@1.0", "ops": [{"op": "set_budget", "llm_calls": 1}]},                 # not the skill at hand
    {"target": "Debug@1.0", "ops": [{"op": "remove_node", "id": "locate"}]},                 # the entry node
    {"target": "Debug@1.0", "ops": [{"op": "set_guard", "src": "test", "dst": "done", "guard": "rep &&"}]},
])
def test_bad_edit_scripts(script):
    with pytest.raises(EditError):
        apply(M.load("debug_v1_0"), script, "1.1")


# ---------------------------------------------------------------- step 1: kernel skeleton

def test_splits_are_stable_and_seeded():
    a, b = Splits(seed=1), Splits(seed=2)
    tasks = [f"task-{i}" for i in range(300)]
    assert [a.of(t) for t in tasks] == [Splits(seed=1).of(t) for t in tasks]
    assert [a.of(t) for t in tasks] != [b.of(t) for t in tasks]
    share = sum(a.of(t) == "train" for t in tasks) / len(tasks)
    assert 0.5 < share < 0.7 and a.of(None) == "train"


def test_heldout_runs_open_no_ticket_and_are_invisible():
    k = M.make_kernel({**CODING, "write_patch": M.patches(W.D2_REGRESS, W.D2_REGRESS)}, skills=("debug_v1_1",))
    held = next(t for t in (f"task-{i}" for i in range(50)) if k.splits.of(t) == "heldout")
    v10 = M.load("debug_v1_0")
    k.library.add(v10)                          # bypass the kernel on purpose: no audit ticket, just a flawed version
    tr = k.run(v10, ISSUE, task=held)
    assert tr.post_eval["post_safe"] is TV.F and not k.tickets
    assert not k.library.stats("Debug", "1.0").history
    with pytest.raises(Exception):
        k.trace(tr.meta["id"])


def test_forged_certificates_are_rejected():
    k = M.make_kernel({"Propose.draft": draft([M.D1])})
    t = ticket(k, "audit")
    good = {"ticket": t.id, "script": M.D1, "claims": {"gate0": "pass", "w8": "proved",
                                                         "evidence": {"check": "K-9999", "pass": True}}}
    assert "no Evolve session" in k.submit(good)["reason"]          # outside an Evolve run: no write token
    assert "not run by the kernel" in k.verify_submission(good)[1]  # the evidence check does not exist
    k._prepare(t)
    real = k.check(t.id, M.D1)
    assert real["pass"]
    cert = copy.deepcopy(good)
    cert["claims"]["evidence"]["check"] = real["id"]
    assert k.verify_submission(cert)[0]
    for tamper, why in [
        (lambda c: c["claims"].update(w8="unproved"), "do not match"),
        (lambda c: c.update(ticket="T-0042"), "another ticket"),
        (lambda c: c.update(script=M.D2), "another ticket or another script"),
        (lambda c: c.update(script=M.D4), "incompatible interface"),
        (lambda c: c.update(script=M.D3), "Gate 0 fails"),
        (lambda c: c.pop("claims"), "malformed"),
    ]:
        bad = copy.deepcopy(cert)
        tamper(bad)
        ok, reason, *_ = k.verify_submission(bad)
        assert not ok and why in reason, reason
    failing = k.check(t.id, M.D2)               # a real check that did not pass cannot back a certificate either
    cert["script"], cert["claims"]["evidence"]["check"] = M.D2, failing["id"]
    assert "did not pass" in k.verify_submission(cert)[1]
    assert k.library.versions("Debug") == [parse_ref("Debug@1.0")[1]]


def test_one_library_write_per_evolve_run():
    k = M.make_kernel({"Propose.draft": draft([M.D1])})
    k.drain()
    assert [str(v) for v in k.library.versions("Debug")] == ["1.0", "1.1"]
    from skillgraph.kernel.kernel import _Session
    k._session = _Session("T-0001", writes=1)
    assert "already written" in k.submit({"ticket": "T-0001"})["reason"]


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory() as d:        # not tmp_path: pytest's shared base directory may be unreadable
        yield Path(d)


def test_persistence_round_trip(root):
    scripts = {**CODING, "write_patch": M.patches(W.D2_REGRESS, W.D4_CORRECT), "Propose.draft": draft(M.DEBUG_CANDIDATES)}
    a = M.make_kernel(scripts, skills=("debug_v1_0", "coding_v1_0"), root=root)
    a.run("Coding", SPEC)
    a.process(ticket(a, "failure").id)
    tr = a.traces["run-0001"]
    assert Trace.from_dict(tr.to_dict()).to_dict() == tr.to_dict()

    b = Kernel(root=root, tools=W.TOOLS, predicates=W.PREDICATES, backend=ScriptedBackend({}))
    assert meta.install(b) == []                # the seeds come back from disk, not from the package
    assert {n: b.library.versions(n) for n in b.library.names()} == {n: a.library.versions(n) for n in a.library.names()}
    assert b.ledger == a.ledger and b.certs == a.certs and b.checks == a.checks
    assert {i: t.to_dict() for i, t in b.tickets.items()} == {i: t.to_dict() for i, t in a.tickets.items()}
    assert set(b.traces) == set(a.traces) and b.traces["run-0001"].to_dict() == tr.to_dict()
    for name, ver in [("Debug", "1.0"), ("Coding", "1.0"), ("Propose", "1.0")]:       # the statistics layer is rebuilt
        sa, sb = a.library.stats(name, ver), b.library.stats(name, ver)
        assert sa.outcomes == sb.outcomes and sa.reliability == sb.reliability and len(sa.history) == len(sb.history)
    assert b.open_ticket("failure", "Debug@1.1", {}).id == "T-0004"        # counters continue


# ---------------------------------------------------------------- step 2: Diagnose

def test_blame_goes_down_to_the_callee():
    k = M.make_kernel({**CODING, "write_patch": M.patches(W.D2_REGRESS)}, skills=("debug_v1_0", "coding_v1_0"))
    tr = k.run("Coding", SPEC)
    r = _blame(tr, tr.meta["id"])
    assert (r["target"], r["fault"], r["step"], r["trace"]) == ("Debug@1.0", "F1", None, "run-0001/4")


def test_blame_stays_with_the_caller_when_the_callee_kept_its_post():
    k = M.make_kernel({**CODING, "write_patch": M.patches(W.D3_DAY_ONLY)}, skills=("debug_v1_1", "coding_v1_0"))
    tr = k.run("Coding", SPEC)
    assert tr.post_eval["post_safe"] is TV.F
    r = _blame(tr, tr.meta["id"])
    assert (r["target"], r["fault"]) == ("Coding@1.0", "F1")


def test_blame_f2_f0_f3():
    k = M.make_kernel({"locate": lambda v: {"F": ["README.md"]}, "write_patch": lambda v: {"patch": {"README.md": "x"}}})
    tr = k.run("Debug", ISSUE)
    r = _blame(tr, "run-0001")
    assert (r["fault"], r["step"], r["evidence_steps"]) == ("F2", "locate", [1])            # the earliest violation
    assert _blame(k.run("Debug", {"issue": W.ISSUE_NO_REPRO, "repo": W.REPO}), "run-0002")["fault"] == "F0"

    blind = Predicates(W.PREDICATES.runtime, {n: f for n, f in W.PREDICATES.judge.items() if n != "relevant"})
    k = Kernel(tools=W.TOOLS, predicates=blind, backend=ScriptedBackend({
        "locate": M.LOCATE, "write_patch": M.patches(W.D2_REGRESS),
        "Diagnose.investigate": lambda v: {"fault": "F1", "report": {**v["report"], "fault": "F1", "evidence_steps": [3]}},
        "Propose.draft": draft([], [])}))
    meta.install(k)
    k.install(M.load("debug_v1_0"))
    k.run("Debug", ISSUE)
    rec = k.process(ticket(k, "failure").id)
    diag = next(s for s in k.traces[rec["trace"]].steps if s.node == "diagnose").sub_trace
    assert [s.node for s in diag.steps] == ["blame", "investigate", "done"]         # F3 goes to the agent
    assert diag.steps[0].output["fault"] == "F3" and rec["fault"]["type"] == "F1"


# ---------------------------------------------------------------- steps 3 and 4: Compare and Evolve on the executor

def test_example_a_failure_driven_repair():
    k = M.make_kernel({**CODING, "write_patch": M.patches(W.D2_REGRESS, W.D4_CORRECT),
                       "Propose.draft": draft(M.DEBUG_CANDIDATES)}, skills=("debug_v1_0", "coding_v1_0"))
    k.run("Coding", SPEC)
    t = ticket(k, "failure")
    assert t.target_hint == "Coding@1.0" and t.mandatory
    rec = k.process(t.id)
    assert verdicts(rec) == {"δ1": "admitted", "δ2": "the evidence run still does not succeed",
                             "δ3": "gate0_fail(W5)", "δ4": "major_only"}
    assert (rec["target"], rec["fault"]["type"], rec["admitted"]) == ("Debug@1.0", "F1", "Debug@1.1")
    assert rec["cost"] == {"llm_calls": 1, "rollouts": 1, "rollout_llm_calls": 1}
    assert t.status == "fixed" and k.library.resolve("Debug").edges[2].guard == M.load("debug_v1_1").edges[2].guard
    assert [str(v) for v in k.library.versions("Coding")] == ["1.0"]               # the caller was not touched
    cert = k.certs["Debug@1.1"]
    assert cert["ticket"] == t.id and cert["claims"]["w8"] == "proved" and k.verify(cert)[0]

    ev = k.traces[rec["trace"]]                                                     # the meta trace
    assert ev.skill == "Evolve" and ev.outputs == {"new_version": "Debug@1.1"} and ev.post_eval["post_safe"] is TV.T
    assert [s.node for s in ev.steps] == ["read", "diagnose", "propose", "gate0", "compare", "submit", "done"]
    assert k.library.stats("Propose", "1.0").reliability["draft"].a == 2            # meta skills get statistics too
    assert k.ledger == [rec]


def test_giving_up_on_everything_is_not_a_fix():
    k = M.make_kernel({**CODING, "write_patch": M.patches(*[W.D2_REGRESS] * 6),
                       "Propose.draft": draft([GIVE_UP])}, skills=("debug_v1_0", "coding_v1_0"))
    assert k.gate0(GIVE_UP)["ok"] and k.gate0(GIVE_UP)["w8"]
    k.run("Coding", SPEC)
    t = ticket(k, "failure")
    rec = k.process(t.id)
    assert verdicts(rec) == {"δ5": "the evidence run still does not succeed"}
    assert rec["outcome"] == "kept" and rec["evolve_end"] == "keep" and t.status == "unresolved"
    assert [str(v) for v in k.library.versions("Debug")] == ["1.0"]


def test_evolve_proposes_again_once_then_gives_up():
    k = M.make_kernel({"Propose.draft": draft([M.D3], [{"target": "Debug@1.0", "ops": "nonsense"}])})
    rec = k.process(ticket(k, "audit").id)
    ev = k.traces[rec["trace"]]
    assert [s.node for s in ev.steps] == ["read", "diagnose", "propose", "gate0", "propose", "gate0", "give_up"]
    assert rec["outcome"] == "kept" and rec["cost"]["llm_calls"] == 2 and verdicts(rec) == {"δ3": "gate0_fail(W5)"}
    draft_step = ev.steps[4].sub_trace.steps[1]
    assert draft_step.contract_post is TV.F                                         # the draft broke its own contract


def test_a_crashing_meta_skill_is_still_on_record():
    k = M.make_kernel({})                       # no script for Propose.draft: the backend raises
    rec = k.process(ticket(k, "audit").id)
    assert rec["outcome"] == "kept" and "crashed" in rec["note"] and k._session is None


# ---------------------------------------------------------------- step 5: mandatory signals

def test_a2_virtual_trace():
    v10 = M.load("debug_v1_0")
    cex = w8_counterexample(check_skill(v10))
    assert (cex["src"], cex["terminal"], cex["state"]["rep"], cex["state"]["suite"]) == ("test", "done", True, False)
    vt = virtual_trace(v10, cex)
    assert [s.node for s in vt.steps] == ["locate", "write_patch", "test", "done"]
    assert _blame(vt, "virt")["fault"] == "F1" and _blame(vt, "virt")["target"] == "Debug@1.0"
    assert w8_counterexample(check_skill(M.load("debug_v1_1"))) is None


def test_example_b_audit_repairs_before_any_failure():
    k = M.make_kernel({"Propose.draft": draft(M.DEBUG_CANDIDATES, [M.G1])}, skills=("debug_v1_0", "coding_v1_0"))
    assert [(t.kind, t.target_hint, t.opened_by) for t in k.tickets.values()] == [
        ("audit", "Debug@1.0", "kernel"), ("audit", "Coding@1.0", "kernel")]
    recs = k.drain()
    assert [r["admitted"] for r in recs] == ["Debug@1.1", "Coding@1.1"]
    assert verdicts(recs[0])["δ2"] == "the counterexample state still takes test → done"
    assert all(r["cost"]["rollouts"] == 0 for r in recs)
    assert [t.status for t in k.tickets.values()] == ["fixed", "fixed"]            # and the new versions raise no A2
    assert not [n for n in ("Debug", "Coding") if not check_skill(k.library.resolve(n), k.library.resolve).w8]
    assert sum(t.skill == "Audit" for t in k.traces.values()) == 1                  # the queue was ordered by Audit


def test_audit_ticket_needs_history_non_regression():
    k = M.make_kernel({"locate": M.LOCATE, "write_patch": M.patches(W.D4_CORRECT, W.D1_NOFIX, W.D1_NOFIX, W.D4_CORRECT),
                       "Propose.draft": draft([GIVE_UP], [M.D1])})
    assert k.run("Debug", ISSUE).post_eval["post_safe"] is TV.T                    # one true success on record
    t = ticket(k, "audit")
    rec = k.process(t.id)
    assert verdicts(rec) == {"δ5": "a past successful run no longer succeeds"} and rec["outcome"] == "kept"
    t.status = "open"                                                               # try again with a real fix
    rec = k.process(t.id)
    assert verdicts(rec) == {"δ1": "admitted"} and k.checks[rec["candidates"][0]["check"]]["history"] == 1


def test_a8_certificate_falsified_by_a_real_run():
    lying = {**W.TOOLS, "debug.run_tests": lambda repo, patch, issue: {"rep": True, "suite": True}}
    k = Kernel(tools=lying, predicates=W.PREDICATES, backend=ScriptedBackend({
        "locate": M.LOCATE, "write_patch": M.patches(W.D2_REGRESS), "Propose.draft": draft([], [])}))
    meta.install(k)
    k.install(M.load("debug_v1_1"))
    assert not k.tickets and k.certs["Debug@1.1"]["claims"]["w8"] == "proved"
    k.run("Debug", ISSUE)
    assert sorted(t.kind for t in k.tickets.values()) == ["failure", "recheck"]
    assert k.certs["Debug@1.1"]["status"] == "stale"
    rec = k.process(ticket(k, "recheck").id)    # a recheck ticket with a real trace goes through Evolve like a failure
    assert rec["fault"] == {"type": "F2", "step": "test"} and rec["outcome"] == "kept"


def test_a8_reverification():
    k = M.make_kernel({"Propose.draft": draft([M.D1])})
    assert k.audit() == []
    k.certs["Debug@1.0"]["claims"]["w8"] = "proved"                                 # a certificate that claims too much
    [t] = k.audit()
    assert (t.kind, t.target_hint) == ("recheck", "Debug@1.0") and k.certs["Debug@1.0"]["status"] == "stale"
    assert t.evidence["counterexample"]["src"] == "test" and k.audit() == []        # stale certificates are not re-raised
    rec = k.process(t.id)                       # no trace, but a counterexample: virtual trace, then Evolve
    assert rec["admitted"] == "Debug@1.1" and t.status == "fixed"
    rec = k.process(ticket(k, "audit").id)      # the A2 ticket about v1.0 is now moot
    assert rec["outcome"] == "resolved"

    k = M.make_kernel({}, skills=("debug_v1_1",))
    k.certs["Debug@1.1"]["claims"]["w8"] = "unproved"                               # wrong, but nothing to repair
    [t] = k.audit()
    assert k.process(t.id)["outcome"] == "suspended" and t.status == "suspended"


def test_stale_evidence_is_refreshed_and_blame_moves_to_the_caller():
    k = M.make_kernel({**CODING, "Propose.draft": draft(M.DEBUG_CANDIDATES, [M.G1]),
                       "write_patch": M.patches(W.D2_REGRESS, W.D2_REGRESS, W.D3_DAY_ONLY, W.D3_DAY_ONLY, W.D4_CORRECT)},
                      skills=("debug_v1_0", "coding_v1_0"))
    k.run("Coding", SPEC)
    k.run("Coding", SPEC)
    first, second = [t for t in k.tickets.values() if t.kind == "failure"]
    assert k.process(first.id)["admitted"] == "Debug@1.1"
    # the second ticket's evidence is about Debug@1.0. Re-run under Debug@1.1: Debug now keeps its promise, but Coding
    # still fails (only the reported test is fixed), so the refreshed evidence blames the caller.
    rec = k.process(second.id)
    assert second.evidence["traces"] == ["run-0003"] and k.traces["run-0003"].meta["refreshes"] == "run-0002"
    assert (rec["target"], rec["fault"]["type"], rec["admitted"]) == ("Coding@1.0", "F1", "Coding@1.1")
    assert k.traces["run-0003"].steps[3].sub_trace.version == "1.1"


def test_stale_evidence_that_now_passes_closes_the_ticket():
    k = M.make_kernel({**CODING, "Propose.draft": draft(M.DEBUG_CANDIDATES),
                       "write_patch": M.patches(W.D2_REGRESS, W.D2_REGRESS, W.D4_CORRECT, W.D4_CORRECT)},
                      skills=("debug_v1_0", "coding_v1_0"))
    k.run("Coding", SPEC)
    k.run("Coding", SPEC)
    first, second = [t for t in k.tickets.values() if t.kind == "failure"]
    k.process(first.id)
    rec = k.process(second.id)
    assert rec["outcome"] == "resolved" and second.status == "resolved" and not rec["candidates"]


def test_overdue_mandatory_ticket_jumps_the_queue():
    k = M.make_kernel({}, skills=("debug_v1_0", "coding_v1_0"))
    low = k.open_ticket("audit", "Debug@1.0", {"signal": "found by Audit"}, opened_by="audit")
    k.register(tools={"audit.rank": lambda queue: {"order": [{"id": low.id, "priority": 1.0}] + [
        {"id": t["id"], "priority": 0.0} for t in queue if t["id"] != low.id]}})
    assert k.next_ticket().id == low.id         # Audit may order the queue ...
    k.processed = k.DEADLINE
    assert k.next_ticket().id == "T-0001"       # ... but not push a mandatory ticket past its deadline
    k.processed = 0
    k.register(tools={"audit.rank": lambda queue: {"order": []}})
    assert k.next_ticket().id == "T-0001"       # an Audit that drops tickets is ignored: first in, first out
