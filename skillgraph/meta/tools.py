"""Tool functions and predicates of the seed meta skills (Step 2 design doc §5, §9.1).

Everything here is the *evolvable* side: it finds problems, assigns blame, filters and ranks candidates. It reaches the
kernel only through read services (`trace`, `skill_source`) and through `gate0` / `check`, whose results the kernel
recomputes or looks up in its own log before anything is written. A bug here costs efficiency, not correctness.
"""
from __future__ import annotations

from functools import partial

from ..kernel.edits import REQUIRED, EditError, new_parameters, validate
from ..trace import TV

# ---------------------------------------------------------------- Diagnose: blame assignment (§5.2)


def _blame(tr, ref: str) -> dict:
    """Layer-by-layer blame assignment on a nested trace. Always lands on exactly one skill."""
    out = {"target": f"{tr.skill}@{tr.version}", "trace": ref, "step": None, "evidence_steps": []}
    if tr.pre is TV.F:
        return {**out, "fault": "F0", "note": "the input is outside Pre"}
    unknown = []
    for s in tr.steps:
        if s.kind == "terminal":
            continue
        if s.contract_post is TV.F:                 # the earliest contract violation
            if s.sub_trace is not None:             # the callee did not deliver its Post: go down one layer
                return _blame(s.sub_trace, f"{ref}/{s.t}")
            return {**out, "fault": "F2", "step": s.node, "evidence_steps": [s.t],
                    "note": f"node {s.node} violated its contract"}
        if s.contract_pre is TV.F and s.kind == "agent":
            return {**out, "fault": "F1", "step": s.node, "evidence_steps": [s.t],
                    "note": f"the workflow did not establish the precondition of {s.node}"}
        if s.contract_post is TV.U:
            unknown.append(s.t)
    if unknown:
        return {**out, "fault": "F3", "evidence_steps": unknown, "note": "some contract checks are undecided"}
    return {**out, "fault": "F1", "evidence_steps": [s.t for s in tr.steps if s.kind != "terminal"],
            "note": "every step kept its contract, yet Post_safe does not hold"}


def blame(kernel, ticket) -> dict:
    ids = (ticket.get("evidence") or {}).get("traces") or []
    if not ids:
        raise ValueError(f"ticket {ticket.get('id')} has no evidence trace")
    report = {"ticket": ticket["id"], **_blame(kernel.trace(ids[0]), ids[0])}
    return {"report": report, "fault": report["fault"]}


# ---------------------------------------------------------------- Propose (§5.4)

# seed table "fault type → operations to try first"; to be replaced by priors learned from the ledger (§8)
HINTS = {
    "F0": ["add_tool", "refine_interface"],
    "F1": ["set_guard", "add_tool", "set_contract"],
    "F2": ["add_tool", "set_loop_bound", "set_prompt", "add_agent"],
    "F3": ["add_tool", "set_contract"],
}


def hints(kernel, report) -> dict:
    return {"hints": {"fault": report["fault"], "try_first": HINTS.get(report["fault"], []),
                      "principle": "prefer tools over new agents: tools add no unknown parameter"},
            "target_ir": kernel.skill_source(report["target"])}


def _parses(c) -> bool:
    try:
        validate(c)
        return True
    except EditError:
        return False


def lint(kernel, report, cands) -> dict:
    """Drop candidates that do not parse, target another skill, or fail the Gate 0 pre-check."""
    ok = [c for c in (cands if isinstance(cands, list) else [])
          if _parses(c) and c["target"] == report["target"] and kernel.gate0(c)["ok"]]
    return {"cands": ok[:8], "n_ok": min(len(ok), 8)}


# ---------------------------------------------------------------- Compare, minimal version (§5.5)


def check(kernel, ticket, report, cands) -> dict:
    """Ask the kernel for an evidence check of every candidate (fixed order, no budget policy yet)."""
    return {"results": [{"script": c, **kernel.check(ticket["id"], c, report.get("trace"))} for c in cands]}


def pick(kernel, ticket, report, results) -> dict:
    """Strategy node: among the candidates that pass, prefer no new unknown parameter, then the smallest script."""
    passing = sorted((r for r in results if r["pass"]),
                     key=lambda r: (new_parameters(r["script"]), len(r["script"]["ops"])))
    if not passing:
        return {"cert": {}, "verdict": "keep"}
    r = passing[0]
    cert = {"ticket": ticket["id"], "script": r["script"], "issued_by": "Compare",
            "claims": {"gate0": "pass", "w8": r["w8"], "evidence": {"check": r["id"], "pass": True}}}
    return {"cert": cert, "verdict": "admit"}


# ---------------------------------------------------------------- Audit, ordering only (§6)


def rank(queue) -> dict:
    """Mandatory tickets first, then the nearest deadline, then first-in-first-out."""
    order = sorted(queue, key=lambda t: (t["opened_by"] != "kernel", t["opened_at"] + t["deadline"], t["id"]))
    n = len(order)
    return {"order": [{"id": t["id"], "priority": round(1 - i / n, 3)} for i, t in enumerate(order)]}


# ---------------------------------------------------------------- predicates used in the seeds' contracts


def _single_target(x) -> bool:
    if isinstance(x, dict):                         # a fault report
        return bool(x.get("target"))
    return isinstance(x, list) and len({c.get("target") for c in x if isinstance(c, dict)}) == 1


RUNTIME = {
    "single_target": _single_target,
    "cites_step": lambda report: bool(report.get("evidence_steps")),
    "parses": lambda cands: isinstance(cands, list) and all(_parses(c) for c in cands),
    "ops_allowed": lambda cands: all(o.get("op") in REQUIRED for c in cands for o in c.get("ops", [])),
    "scripts_ok": lambda cands: isinstance(cands, list) and all(_parses(c) for c in cands)
    and len({c["target"] for c in cands}) <= 1,
    "has_evidence": lambda cert: bool(((cert.get("claims") or {}).get("evidence") or {}).get("check")),
    "is_permutation": lambda order, queue: sorted(o["id"] for o in order) == sorted(t["id"] for t in queue),
}


def tools(kernel) -> dict:
    return {"diagnose.blame": partial(blame, kernel), "propose.hints": partial(hints, kernel),
            "propose.lint": partial(lint, kernel), "compare.check": partial(check, kernel),
            "compare.pick": partial(pick, kernel), "audit.rank": rank}
