"""Mandatory signals (Step 2 design doc §6.1): the three signals that concern correctness are raised by the kernel itself.

    A1  post-hoc evaluation fails            → failure ticket
    A2  W8 unproved at admission             → audit ticket, with the symbolic counterexample attached
    A8  a certificate no longer holds        → recheck ticket, and the certificate is marked stale
        (a) re-verification against the current library fails, or
        (b) a version certified "W8 proved" delivers a result that violates Post_safe in a real run

The Audit skill may order these tickets but cannot suppress them.

A W8 counterexample is turned into a *virtual failure trace* (§6.2): every node on a path to the counterexample keeps
its contract, yet Post_safe does not hold. Blame assignment then needs no special case for audit tickets.
"""
from __future__ import annotations

from ..model import Version
from ..trace import TV, Step, Trace
from .edits import ref_of


def w8_counterexample(report) -> dict | None:
    """The first "all contracts kept, Post_safe not established" counterexample of a well-formedness report, if any."""
    w8 = next((c for c in report.checks if c.code == "W8"), None)
    data = w8.data if w8 else []
    posts = [x for x in data if x["kind"] == "post" and x["src"]]
    return (posts or data or [None])[0]


def virtual_trace(skill, cex: dict) -> Trace | None:
    """A trace along the shortest path from the entry to the counterexample edge. None if the counterexample is not of
    the form "edge src → terminal violates Post_safe"."""
    if cex.get("kind") != "post" or not cex.get("src") or cex.get("terminal") not in skill.nodes:
        return None
    prev, todo = {skill.entry: None}, [skill.entry]
    while todo:                                     # breadth-first search
        v = todo.pop(0)
        for e in skill.out_edges(v):
            if e.dst not in prev and skill.nodes[e.dst].kind != "terminal":
                prev[e.dst] = v
                todo.append(e.dst)
    if cex["src"] not in prev:
        return None
    path, v = [], cex["src"]
    while v is not None:
        path.append(v)
        v = prev[v]
    path.reverse()
    steps = []
    for i, v in enumerate(path):
        n = skill.nodes[v]
        steps.append(Step(i + 1, v, n.kind, contract_pre=TV.T if n.kind == "agent" else None, contract_post=TV.T,
                          inv=TV.T, next=path[i + 1] if i + 1 < len(path) else cex["terminal"]))
    steps.append(Step(len(path) + 1, cex["terminal"], "terminal"))
    tr = Trace(skill.name, str(skill.version), {}, pre=TV.T, steps=steps,
               outcome=skill.nodes[cex["terminal"]].outcome,
               post_eval={"post_safe": TV.F, "post_live": TV.U, "inv": TV.T})
    tr.meta = {"virtual": dict(cex)}
    return tr


def _has_open(kernel, kind, target) -> bool:
    return any(t.status == "open" and t.kind == kind and t.target_hint == target for t in kernel.tickets.values())


def on_admit(kernel, skill, report):
    """A2: spec consistency unproved at admission."""
    cex = w8_counterexample(report)
    if report.w8 or cex is None or _has_open(kernel, "audit", ref_of(skill)):
        return None
    return kernel.open_ticket("audit", ref_of(skill), {"signal": "W8 unproved", "counterexample": cex})


def _walk(tr: Trace, ref: str):
    yield ref, tr
    for s in tr.steps:
        if s.sub_trace:
            yield from _walk(s.sub_trace, f"{ref}/{s.t}")


def on_run(kernel, tr: Trace) -> list:
    """A1 and A8 (b), after the post-hoc evaluation of a train run."""
    out, rid = [], tr.meta["id"]
    if tr.post_eval["post_safe"] is TV.F:
        out.append(kernel.open_ticket("failure", f"{tr.skill}@{tr.version}", {
            "traces": [rid], "signal": f"post_safe = ⊥ in {rid}"}))
    for ref, sub in _walk(tr, rid):
        target = f"{sub.skill}@{sub.version}"
        cert = kernel.certs.get(target)
        if (sub.outcome == "done" and sub.post_eval and sub.post_eval["post_safe"] is TV.F and cert
                and cert["status"] == "valid" and cert["claims"].get("w8") == "proved"):
            kernel.mark_stale(target, f"certified W8 proved, but {ref} delivered a result violating Post_safe")
            out.append(kernel.open_ticket("recheck", target, {
                "traces": [rid], "signal": f"certificate {cert['id']} falsified by {ref}"}))
    return out


def audit(kernel) -> list:
    """A8 (a): re-verify the certificate of every current version (the latest minor of each major)."""
    out = []
    for name in list(kernel.library.names()):
        for major in sorted({v.major for v in kernel.library.versions(name)}):
            skill = kernel.library.resolve(name, Version(major, 0))
            target = ref_of(skill)
            cert = kernel.certs.get(target)
            if cert is None or cert["status"] != "valid":
                continue
            ok, why, report = kernel.verify(cert)
            if ok:
                continue
            kernel.mark_stale(target, why)
            ev = {"signal": f"certificate {cert['id']} failed re-verification: {why}"}
            cex = w8_counterexample(report)
            if cex is not None:
                ev["counterexample"] = cex
            out.append(kernel.open_ticket("recheck", target, ev))
    return out
