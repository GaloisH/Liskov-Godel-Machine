"""Toy examples for the meta skills: the kernel, tickets, blame assignment, repair and the ledger (Step 2 design doc).

Run:  python examples/run_meta.py

No LLM is involved: `ScriptedBackend` plays every agent, including the Propose agent that drafts the candidates.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples import toy_meta as M                                            # noqa: E402
from examples import toy_world as W                                           # noqa: E402
from skillgraph import ScriptedBackend, meta                                  # noqa: E402
from skillgraph.kernel import Kernel                                          # noqa: E402


def title(s):
    print(f"\n{'=' * 12} {s} {'=' * 12}")


def show_tickets(k):
    for t in k.tickets.values():
        why = t.result.get("reason") or t.result.get("admitted") or ""
        print(f"  {t.id}  {t.kind:<8} {t.target_hint:<12} {t.status:<10} {t.evidence.get('signal', '')}"
              + (f"  → {why}" if why else ""))


def show_record(rec):
    print(f"  {rec['record']}: ticket {rec['ticket']} ({rec['kind']}), target {rec['target']}, fault {rec['fault']}")
    for c in rec["candidates"]:
        print(f"      {c['id']}  {'+'.join(c['ops']):<44} {c['verdict']}")
    print(f"      outcome: {rec['outcome']}" + (f" → {rec['admitted']}" if rec["admitted"] else "")
          + f"; cost {rec['cost']}" + (f"; note: {rec['note']}" if rec.get("note") else ""))


def example_a():
    title("Example A  Failure-driven repair (§11.1)")
    k = M.make_kernel({
        "understand": [{"plan": "validate month and day in parse_date"}],
        "implement": M.patches(W.D1_NOFIX),
        "locate": M.LOCATE,
        # the first patch is the one of the failed run; the second is what the agent writes when asked again
        "write_patch": M.patches(W.D2_REGRESS, W.D4_CORRECT),
        "Propose.draft": [{"cands": M.DEBUG_CANDIDATES}],
    }, skills=("debug_v1_0", "coding_v1_0"))
    print("Installed:", ", ".join(c["version"] for c in k.certs.values()))

    tr = k.run("Coding", {"spec": W.SPEC, "repo": W.REPO})
    print("\n① A Coding run. Debug hands back a patch that fixes the reported test but breaks leap years:")
    print(tr.pretty())
    print("\nTickets opened by the kernel (A2 at admission, A1 after this run):")
    show_tickets(k)

    failure = next(t for t in k.tickets.values() if t.kind == "failure")
    rec = k.process(failure.id)
    print(f"\n②–⑤ Evolve on {failure.id}. Its own run is a trace like any other:")
    print(k.traces[rec["trace"]].pretty())
    print("\nLedger record written by the kernel:")
    show_record(rec)
    print("\nCertificate of the new version:")
    cert = k.certs[rec["admitted"]]
    print("  " + json.dumps({x: cert[x] for x in ("id", "version", "ticket", "candidate", "claims")}, ensure_ascii=False))
    call = k.library.get("Coding", "1.0").nodes["debug"]
    print(f"\nCoding declares \"{call.skill} >= {call.min_version}\" and now resolves to "
          f"{k.library.resolve(call.skill, call.min_version).key}: Coding itself was not touched.")

    print("\nThe audit ticket for Debug@1.0 is still open. Its counterexample is gone in v1.1, so it closes by itself:")
    audit = next(t for t in k.tickets.values() if t.kind == "audit" and t.target_hint == "Debug@1.0")
    show_record(k.process(audit.id))
    return k


def example_b():
    title("Example B  Audit fixes the same flaw before any failure (§11.2)")
    k = M.make_kernel({
        "Propose.draft": [{"cands": M.DEBUG_CANDIDATES}, {"cands": [M.G1]}],
    }, skills=("debug_v1_0", "coding_v1_0"))
    print("Debug v1.0 and Coding v1.0 are admitted with W8 unproved, so the kernel opens two mandatory audit tickets:")
    show_tickets(k)
    t = k.tickets["T-0001"]
    print(f"\nCounterexample attached to {t.id}: {t.evidence['counterexample']}")

    recs = k.drain()
    vt = k.traces[t.evidence["traces"][0]]
    print(f"\nThe counterexample as a virtual failure trace ({vt.meta['id']}): every step keeps its contract, Post_safe = ⊥")
    print(vt.pretty())
    print("\nNo task was run, no agent of a task skill was called. Ledger:")
    for rec in recs:
        show_record(rec)
    print("\nLibrary:", {n: [str(v) for v in k.library.versions(n)] for n in ("Debug", "Coding")})
    show_tickets(k)
    return k


def example_kernel_guards(k):
    title("Kernel guards: forged certificates, stale certificates, persistence (§2, §10.5)")
    forged = {"ticket": "T-0001", "issued_by": "Compare", "script": {**M.D1, "target": "Debug@1.1"},
              "claims": {"gate0": "pass", "w8": "proved", "evidence": {"check": "K-9999", "pass": True}}}
    print("A hand-written certificate handed to submit outside an Evolve run:")
    print(f"  ✗ {k.submit(forged)['reason']}")
    print("The same certificate, re-verified on its own merits:")
    print(f"  ✗ {k.verify_submission(forged)[1]}")
    lying = {**forged, "script": M.D4 | {"target": "Debug@1.1"}}
    print("A certificate for δ4 (weakened Post_safe):")
    print(f"  ✗ {k.verify_submission(lying)[1]}")

    print("\nA8: tamper with the stored certificate of Debug@1.1 (make it claim W8 unproved), then let the kernel re-verify:")
    k2 = M.make_kernel({"Propose.draft": [{"cands": M.DEBUG_CANDIDATES}]})
    k2.drain()                                  # Debug@1.1 is admitted; now pretend its certificate says something else
    k2.certs["Debug@1.1"]["claims"]["w8"] = "unproved"
    for t in k2.audit():
        print(f"  {t.id}  {t.kind}  {t.target_hint}  {t.evidence['signal']}")
    print(f"  certificate status: {k2.certs['Debug@1.1']['status']}")

    with tempfile.TemporaryDirectory() as root:
        a = M.make_kernel({"Propose.draft": [{"cands": M.DEBUG_CANDIDATES}]}, root=root)
        a.drain()
        b = Kernel(root=root, tools=W.TOOLS, predicates=W.PREDICATES, backend=ScriptedBackend({}))
        meta.install(b)
        print(f"\nPersistence: saved to a directory and loaded back → versions of Debug {[str(v) for v in b.library.versions('Debug')]}, "
              f"{len(b.ledger)} ledger record(s), {len(b.traces)} trace(s), {len(b.tickets)} ticket(s)")
        print("  files:", sorted(p.name for p in Path(root).iterdir()))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    k = example_a()
    example_b()
    example_kernel_guards(k)


if __name__ == "__main__":
    main()
