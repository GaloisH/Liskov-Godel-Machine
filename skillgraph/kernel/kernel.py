"""The trusted kernel (Step 2 design doc §2): everything that decides "what counts as better" and "what is on record".

    install      human admission of a seed skill (the only write that needs no ticket)
    run          run a task skill, evaluate it post hoc, record it, raise the mandatory signals
    gate0        apply an edit script and run Gate 0 on the result
    check        evidence check of a candidate on a ticket; the result goes into the kernel's own check log
    submit       the only way a meta skill can write to the library: re-verifies the certificate
    process      run Evolve on one ticket and write the repair ledger, whatever Evolve did
    audit        re-verify certificates (A8)

Admission standard of this minimal version ("the ticket passes after the fix"):
    * failure / recheck ticket with a real trace: the candidate is replayed on the failing run of the target from the
      cache, continued with real agent calls past the divergence frontier, and must end `done` with Post_safe = ⊤;
    * audit / recheck ticket with a symbolic counterexample: the counterexample must be gone (the W8 obligation at that
      terminal is proved and the counterexample state no longer takes the edge into it), and every truly successful
      past run of the target must still succeed under replay + continuation.
Only interface-compatible candidates are admitted automatically; a new major version needs a human.

Meta skills are ordinary skills: they run on the same executor and reach the kernel only through the tools
`kernel.read_ticket`, `kernel.gate0`, `kernel.submit` and the read / check services below. A certificate handed to
`submit` is never trusted: static claims are recomputed, and the evidence claim must point at a check that the kernel
itself ran for the same ticket and the same script.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..executor import Executor, FallbackBackend, Predicates
from ..expr import ExprError
from ..library import GateError, SkillLibrary
from ..logic import _abs_eval
from ..model import Version
from ..refine import check_refinement
from ..trace import TV, Trace, digest
from ..wellformed import HARD, check_skill
from . import signals
from .edits import EditError, apply, op_names, parse_ref, ref_of, validate
from .splits import Splits
from .store import Store
from .tickets import KINDS, Ticket


class KernelError(Exception):
    pass


@dataclass
class _Session:
    """One Evolve run on one ticket: what the kernel saw, for the ledger, and the single write token (P2)."""
    ticket: str
    scripts: dict = field(default_factory=dict)     # script digest → script
    gate0: dict = field(default_factory=dict)       # script digest → Gate 0 result
    checks: dict = field(default_factory=dict)      # script digest → check record
    writes: int = 0
    admitted: str | None = None
    admitted_digest: str | None = None
    submit_reason: str = ""


class Kernel:
    EVOLVE, AUDIT = "Evolve", "Audit"
    DEADLINE = 20

    def __init__(self, root=None, tools=None, predicates=None, backend=None, seed: int = 0, ratios=(0.6, 0.2, 0.2)):
        """root: directory to persist to (None = in memory). tools / predicates / backend describe the task world."""
        self.library = SkillLibrary()
        self.backend = backend
        self.tools = dict(tools or {})
        p = predicates or Predicates()
        self.predicates = Predicates(dict(p.runtime), dict(p.judge))
        self.splits = Splits(seed, ratios)
        self.tickets: dict[str, Ticket] = {}
        self.ledger: list[dict] = []
        self.certs: dict[str, dict] = {}        # "Name@version" → kernel certificate
        self.checks: dict[str, dict] = {}       # check id → record
        self.traces: dict[str, Trace] = {}
        self.counters, self.processed = Counter(), 0
        self._session: _Session | None = None
        self.tools.update({"kernel.read_ticket": self._t_read_ticket, "kernel.gate0": self._t_gate0,
                           "kernel.submit": self._t_submit})
        self.predicates.runtime.update(well_formed=self._well_formed, kernel_accepted=self._accepted)
        self.store = Store(root) if root else None
        if self.store and self.store.exists:
            self._load()

    # ------------------------------------------------------------ plumbing

    def register(self, tools=None, runtime=None, judge=None):
        """Register tool functions and predicates (code is not persisted; re-register after loading)."""
        self.tools.update(tools or {})
        self.predicates.runtime.update(runtime or {})
        self.predicates.judge.update(judge or {})

    def executor(self, backend=None) -> Executor:
        return Executor(self.library, self.tools, backend or self.backend, self.predicates)

    def _id(self, prefix: str) -> str:
        self.counters[prefix] += 1
        self._save_state()
        return f"{prefix}-{self.counters[prefix]:04d}"

    def _save_state(self):
        if self.store:
            self.store.save_state({"counters": dict(self.counters), "processed": self.processed,
                                   "seed": self.splits.seed, "ratios": list(self.splits.ratios)})

    def _log(self, name: str, record: dict):
        if self.store:
            self.store.append(name, record)

    def _well_formed(self, ticket) -> bool:
        return isinstance(ticket, dict) and ticket.get("id") in self.tickets and ticket.get("kind") in KINDS

    def _accepted(self, ref) -> bool:
        return isinstance(ref, str) and ref in self.certs

    def _load(self):
        st = self.store.load_state()
        self.counters, self.processed = Counter(st["counters"]), st["processed"]
        self.splits = Splits(st["seed"], st["ratios"])
        bad = {k: r for k, r in self.library.add_all(self.store.load_skills()).items() if isinstance(r, GateError)}
        if bad:
            raise KernelError(f"stored skills no longer pass admission: {bad}")
        self.certs = self.store.load_certs()
        self.tickets = {d["id"]: Ticket.from_dict(d) for d in self.store.load_log("tickets")}
        self.ledger = self.store.load_log("ledger")
        self.checks = {d["id"]: d for d in self.store.load_log("checks")}
        self.traces = {t.meta["id"]: t for t in self.store.load_traces()}
        for t in sorted((t for t in self.traces.values() if t.meta.get("recorded")), key=lambda t: t.meta["seq"]):
            self.library.record(t)          # the statistics layer is a fold over the recorded traces

    # ------------------------------------------------------------ certificates

    def _certify(self, skill, report, issued_by: str, extra: dict | None = None) -> dict:
        ref = ref_of(skill)
        cert = {"id": self._id("C"), "version": ref, "issued_by": issued_by, "status": "valid",
                **(extra or {})}
        cert["claims"] = {**(extra or {}).get("claims", {}), "gate0": "pass",
                          "w8": "proved" if report.w8 else "unproved"}
        self.certs[ref] = cert
        if self.store:
            self.store.save_skill(ref, skill)
            self.store.save_cert(ref, cert)
        return cert

    def verify(self, cert: dict):
        """Recompute the static claims of an admitted version against the current library. → (ok, reason, report)"""
        name, ver = parse_ref(cert["version"])
        report = check_skill(self.library.get(name, ver), self.library.resolve)
        if not report.ok:
            return False, f"Gate 0 no longer passes {report.failed()}", report
        now = "proved" if report.w8 else "unproved"
        if cert["claims"].get("w8") != now:
            return False, f"claims W8 {cert['claims'].get('w8')}, but W8 is {now}", report
        return True, "", report

    def mark_stale(self, ref: str, reason: str):
        cert = self.certs[ref]
        cert.update(status="stale", stale_reason=reason)
        if self.store:
            self.store.save_cert(ref, cert)

    # ------------------------------------------------------------ human admission and task runs

    def install(self, skill, by: str = "human") -> dict:
        """Admit a seed skill on human authority. Still goes through Gate 0, and still raises A2."""
        self.library.add(skill)             # raises GateError
        report = self.library.entry(skill.name, skill.version).certificate.wf
        cert = self._certify(skill, report, by)
        signals.on_admit(self, skill, report)
        return cert

    def install_all(self, skills) -> list:
        """Install several seeds, callees first."""
        todo, out = sorted(skills, key=lambda s: (s.version, s.name)), []
        while todo:
            ready = [s for s in todo if all(self.library.resolve(n.skill, n.min_version) for n in s.calls())]
            for s in ready or todo[:1]:
                out.append(self.install(s))
                todo.remove(s)
        return out

    def run(self, skill, inputs: dict, task=None) -> Trace:
        """Run a task skill, evaluate it post hoc and keep the trace. Train runs update the statistics layer and
        raise the mandatory signals; heldout / test runs are only stored."""
        split = self.splits.of(task)
        tr = self._execute(skill, inputs, {"task": task, "split": split}, record=split == "train")
        if split == "train":
            signals.on_run(self, tr)
        return tr

    def _execute(self, skill, inputs, meta, prefix="run", backend=None, record=True) -> Trace:
        ex = self.executor(backend)
        tr = ex.posthoc(ex.run(skill, inputs))
        tr.meta = {"id": self._id(prefix), **meta}
        self._keep(tr, record)
        return tr

    def _keep(self, tr: Trace, record: bool):
        if record:
            self.counters["seq"] += 1
            tr.meta.update(recorded=True, seq=self.counters["seq"])
            self.library.record(tr)
        self.traces[tr.meta["id"]] = tr
        if self.store:
            self.store.save_trace(tr)

    def trace(self, ref: str) -> Trace:
        """"run-0001/4" = the sub-trace of step t4 of run-0001. Only train traces are visible."""
        parts = ref.split("/")
        tr = self.traces.get(parts[0])
        if tr is None or tr.meta.get("split", "train") != "train":
            raise KernelError(f"no visible trace {parts[0]}")
        for t in parts[1:]:
            step = next((s for s in tr.steps if str(s.t) == t and s.sub_trace), None)
            if step is None:
                raise KernelError(f"{ref}: no sub-trace at step {t}")
            tr = step.sub_trace
        return tr

    def skill_source(self, ref: str) -> dict:
        name, ver = parse_ref(ref)
        return self.library.get(name, ver).raw

    # ------------------------------------------------------------ tickets

    def open_ticket(self, kind, target_hint, evidence, opened_by="kernel", deadline=None) -> Ticket:
        if kind not in KINDS:
            raise KernelError(f"unknown ticket kind {kind!r}")
        t = Ticket(self._id("T"), kind, target_hint, dict(evidence), opened_by,
                   deadline=self.DEADLINE if deadline is None else deadline, opened_at=self.processed)
        self.tickets[t.id] = t
        self._log("tickets", t.to_dict())
        return t

    def _close(self, t: Ticket, status: str, **result):
        t.status, t.result = status, result
        self._log("tickets", t.to_dict())

    def next_ticket(self) -> Ticket | None:
        """Overdue mandatory tickets first (kernel rule); otherwise the order proposed by the Audit skill;
        first-in-first-out if Audit is missing or returns something that is not a permutation of the queue."""
        queue = sorted((t for t in self.tickets.values() if t.status == "open"), key=lambda t: t.id)
        if not queue:
            return None
        overdue = [t for t in queue if t.mandatory and self.processed - t.opened_at >= t.deadline]
        if overdue or len(queue) == 1 or not self.library.versions(self.AUDIT):
            return (overdue or queue)[0]
        try:
            tr = self._execute(self.AUDIT, {"queue": [t.view() for t in queue]}, {"split": "train"}, prefix="meta")
            order = tr.outputs.get("order") or []
            if tr.outcome != "done" or sorted(o["id"] for o in order) != [t.id for t in queue]:
                return queue[0]
        except Exception:                           # a broken Audit must not stall the queue
            return queue[0]
        for o in order:
            self.tickets[o["id"]].priority = float(o.get("priority", 0))
        return self.tickets[order[0]["id"]]

    # ------------------------------------------------------------ Gate 0

    def _candidate(self, script):
        """Apply a script to the current version of its target. → (target, candidate, report, refinement)"""
        validate(script)
        name, ver = parse_ref(script["target"])
        versions = self.library.versions(name)
        if ver not in versions:
            raise EditError(f"{script['target']} is not in the library")
        latest = self.library.resolve(name, Version(ver.major, 0))
        if latest.version != ver:
            raise EditError(f"{script['target']} is not the current version ({ref_of(latest)} is)")
        target = self.library.get(name, ver)
        cand = apply(target, script, Version(ver.major, ver.minor + 1))
        refine = check_refinement(target, cand)
        if not refine.compatible:           # an incompatible interface can only be a new major version
            cand = apply(target, script, Version(max(v.major for v in versions) + 1, 0))
        return target, cand, check_skill(cand, self.library.resolve), refine

    def gate0(self, script) -> dict:
        """Gate 0 on the edited skill: well-formedness W0–W7 + budget, and whether the interface stays compatible."""
        try:
            _, cand, report, refine = self._candidate(script)
        except EditError as ex:
            res = {"ok": False, "failed": ["edit"], "messages": [str(ex)]}
        else:
            msgs = [m for c in report.checks if not c.ok and c.code in HARD for m in c.messages]
            res = {"ok": report.ok, "failed": report.failed(), "messages": msgs[:3], "w8": report.w8,
                   "compat": refine.compatible, "version": ref_of(cand)}
        if self._session and isinstance(script, dict):
            dg = digest(script)
            self._session.scripts[dg], self._session.gate0[dg] = script, res
        return res

    # ------------------------------------------------------------ evidence checks

    def check(self, ticket_id: str, script, trace_ref: str | None = None) -> dict:
        """Does the ticket pass after this fix? Run by the kernel, on evidence validated by the kernel, and logged by
        the kernel. A meta skill can ask for a check but cannot produce one."""
        rec = {"id": self._id("K"), "ticket": ticket_id, "digest": digest(script), "pass": False, "reason": "",
               "rollouts": 0, "target": script.get("target") if isinstance(script, dict) else None,
               "candidate": script.get("id") if isinstance(script, dict) else None}
        try:
            ticket = self.tickets.get(ticket_id)
            if ticket is None:
                raise KernelError(f"unknown ticket {ticket_id}")
            target, cand, report, refine = self._candidate(script)
            rec["w8"] = "proved" if report.w8 else "unproved"
            if not report.ok:
                rec["reason"] = f"gate0: {report.failed()}"
            elif not refine.compatible:
                rec["reason"] = "major_only"        # weakening the interface is not a fix for the callers
            else:
                ev = self._evidence(ticket, trace_ref, ref_of(target))
                virtual = ev.meta.get("virtual")
                ok, why, detail = (self._check_virtual(target, cand, report, virtual) if virtual
                                   else self._check_replay(cand, ev))
                rec.update(detail, reason=why)
                rec["pass"] = ok
        except (EditError, KernelError) as ex:
            rec["reason"] = str(ex)
        self.checks[rec["id"]] = rec
        self._log("checks", rec)
        if self._session:
            self._session.checks[rec["digest"]] = rec
            if isinstance(script, dict):
                self._session.scripts.setdefault(rec["digest"], script)
        return rec

    def _evidence(self, ticket: Ticket, trace_ref, target_ref: str) -> Trace:
        """The failing (sub-)run of the target named by trace_ref, validated against the ticket."""
        ids = ticket.evidence.get("traces") or []
        ref = trace_ref or (ids[0] if ids else None)
        if not ref or ref.split("/")[0] not in ids:
            raise KernelError(f"{ref} is not evidence of ticket {ticket.id}")
        tr = self.trace(ref)
        if f"{tr.skill}@{tr.version}" != target_ref:
            raise KernelError(f"{ref} is a run of {tr.skill}@{tr.version}, not of {target_ref}")
        if not tr.post_eval or tr.post_eval["post_safe"] is not TV.F:
            raise KernelError(f"{ref} does not violate the Post_safe of {target_ref}: nothing to fix there")
        return tr

    def _rerun(self, skill, old: Trace):
        """Cached replay of `skill` on the inputs of `old`, continued live past the frontier. → (trace, rollout calls)"""
        fb = FallbackBackend([old], self.backend)
        ex = self.executor(fb)
        return ex.posthoc(ex.run(skill, old.inputs)), fb.live_calls

    def _check_replay(self, cand, ev: Trace):
        try:
            tr, live = self._rerun(cand, ev)
        except Exception as ex:                     # e.g. no backend to continue with
            return False, f"continuation failed: {ex!r}", {}
        ok = tr.outcome == "done" and tr.post_eval["post_safe"] is TV.T
        detail = {"evidence": "replay", "outcome": tr.outcome, "post_safe": str(tr.post_eval["post_safe"]),
                  "rollouts": int(live > 0), "rollout_llm_calls": live}
        return ok, "" if ok else "the evidence run still does not succeed", detail

    def _check_virtual(self, target, cand, report, cex: dict):
        detail = {"evidence": "counterexample", "rollouts": 0, "rollout_llm_calls": 0}
        # (a) the W8 obligation at that terminal is proved
        left = [x for x in report["W8"].data if x["kind"] == "post" and x.get("terminal") == cex["terminal"]]
        if left:
            return False, f"the W8 obligation at {cex['terminal']} is still unproved", detail
        # (b) the counterexample state no longer takes an edge into that terminal
        env = {**cex["state"], **{"@" + k: v for k, v in cex["state"].items()}}
        for e in cand.out_edges(cex["src"]) if cex["src"] in cand.nodes else []:
            if e.dst != cex["terminal"]:
                continue
            try:
                if _abs_eval(e.guard, env, cand.types):
                    return False, f"the counterexample state still takes {e.src} → {e.dst}", detail
            except (KeyError, ExprError):
                pass                                # the guard reads something outside the counterexample: (a) decides
        # (c) no regression on the truly successful past runs of the target
        for old in self._successes(target):
            try:
                tr, live = self._rerun(cand, old)
            except Exception as ex:
                return False, f"continuation failed on a past success: {ex!r}", detail
            detail["rollouts"] += int(live > 0)
            detail["rollout_llm_calls"] += live
            if tr.outcome != "done" or tr.post_eval["post_safe"] is not TV.T:
                return False, "a past successful run no longer succeeds", detail
        detail["history"] = len(self._successes(target))
        return True, "", detail

    def _successes(self, skill) -> list:
        hist = self.library.stats(skill.name, skill.version).history
        return [t for t in hist if t.outcome == "done" and t.post_eval and t.post_eval["post_safe"] is TV.T]

    # ------------------------------------------------------------ submit

    def verify_submission(self, cert):
        """Re-verify a certificate handed in by a meta skill. → (ok, reason, candidate skill, report)"""
        try:
            claims, script = cert["claims"], cert["script"]
            _, cand, report, refine = self._candidate(script)
        except (KeyError, TypeError) as ex:
            return False, f"malformed certificate: {ex!r}", None, None
        except EditError as ex:
            return False, str(ex), None, None
        if not report.ok:
            return False, f"Gate 0 fails {report.failed()}", None, None
        if not refine.compatible:
            return False, "incompatible interface: a new major version needs human review", None, None
        w8 = "proved" if report.w8 else "unproved"
        if claims.get("gate0") != "pass" or claims.get("w8") != w8:
            return False, f"claims do not match the recomputed checks (W8 is {w8})", None, None
        k = self.checks.get((claims.get("evidence") or {}).get("check"))
        if k is None:
            return False, "the evidence check named by the certificate was not run by the kernel", None, None
        if k["ticket"] != cert.get("ticket") or k["digest"] != digest(script):
            return False, "the evidence check belongs to another ticket or another script", None, None
        if not k["pass"]:
            return False, f"the evidence check did not pass: {k['reason']}", None, None
        return True, "", cand, report

    def submit(self, cert) -> dict:
        """The only library write available to meta skills. One write per Evolve run, for that run's ticket only."""
        s = self._session
        ok, why, cand, report = False, "", None, None
        if s is None:
            why = "no Evolve session: submit can only be called while a ticket is being processed"
        elif s.writes >= 1:
            why = "this Evolve run has already written one version"
        elif not isinstance(cert, dict) or cert.get("ticket") != s.ticket:
            why = "the certificate is not for the ticket being processed"
        else:
            ok, why, cand, report = self.verify_submission(cert)
        if ok:
            try:
                self.library.add(cand)
            except GateError as ex:                 # e.g. a budget change breaks a caller
                ok, why = False, str(ex)
        if not ok:
            if s:
                s.submit_reason = why
            return {"new_version": "", "accepted": False, "reason": why}
        ref = ref_of(cand)
        s.writes, s.admitted, s.admitted_digest = s.writes + 1, ref, digest(cert["script"])
        self._certify(cand, report, str(cert.get("issued_by", "meta")), {
            "ticket": cert["ticket"], "target": cert["script"]["target"], "candidate": cert["script"].get("id"),
            "script": cert["script"], "claims": {"compat": True, "evidence": cert["claims"]["evidence"]}})
        signals.on_admit(self, cand, report)
        return {"new_version": ref, "accepted": True, "reason": ""}

    # ------------------------------------------------------------ kernel tool nodes

    def _t_read_ticket(self, ticket) -> dict:
        return {"kind": ticket["kind"]}

    def _t_gate0(self, report, cands) -> dict:
        ok = [c for c in (cands if isinstance(cands, list) else []) if self.gate0(c)["ok"]]
        return {"cands": ok[:8], "n_ok": min(len(ok), 8)}

    def _t_submit(self, cert) -> dict:
        r = self.submit(cert)
        return {"new_version": r["new_version"], "accepted": r["accepted"]}

    # ------------------------------------------------------------ processing tickets

    def _current(self, name: str, version: str):
        return self.library.resolve(name, Version(Version.parse(version).major, 0))

    def _prepare(self, t: Ticket):
        """Make sure the ticket carries evidence that still applies to the current library.

        A symbolic counterexample becomes a virtual failure trace. Evidence about a superseded version is refreshed:
        if the problem is already gone the ticket is closed as resolved, otherwise the new evidence replaces the old.
        A ticket left without usable evidence is suspended for a human.
        """
        ids = t.evidence.get("traces") or []
        if ids and not self.traces[ids[0]].meta.get("virtual"):
            old = self.traces[ids[0]]
            if all(self._current(x.skill, x.version).version == Version.parse(x.version)
                   for _, x in signals._walk(old, ids[0])):
                return
            try:                                    # some version in the run was superseded: run it again
                fb = FallbackBackend([old], self.backend)
                new = self._execute(self._current(old.skill, old.version), old.inputs,
                                    {"task": old.meta.get("task"), "split": "train", "refreshes": ids[0]}, backend=fb)
            except Exception as ex:
                return self._close(t, "suspended", reason=f"could not refresh the evidence: {ex!r}")
            if new.post_eval["post_safe"] is not TV.F:
                return self._close(t, "resolved", reason="the evidence run passes under the current versions",
                                   trace=new.meta["id"])
            t.evidence["traces"], t.target_hint = [new.meta["id"]], f"{new.skill}@{new.version}"
            return
        name, ver = parse_ref(t.target_hint)
        skill = self._current(name, str(ver))
        cex = t.evidence.get("counterexample")
        if skill.version != ver:                    # the counterexample was about an older version
            report = check_skill(skill, self.library.resolve)
            cex = signals.w8_counterexample(report)
            if report.w8 or cex is None:
                return self._close(t, "resolved", reason=f"{ref_of(skill)} no longer has the counterexample")
            t.target_hint, t.evidence["counterexample"] = ref_of(skill), cex
        vt = signals.virtual_trace(skill, cex) if cex else None
        if vt is None:
            return self._close(t, "suspended", reason="no failing trace and no usable counterexample")
        vt.meta.update(id=self._id("virt"), split="train", ticket=t.id)
        self._keep(vt, record=False)
        t.evidence["traces"] = [vt.meta["id"]]

    def process(self, ticket_id: str) -> dict:
        """Handle one ticket: prepare its evidence, run Evolve on the executor, and write the ledger record."""
        t = self.tickets[ticket_id]
        if t.status != "open":
            raise KernelError(f"ticket {ticket_id} is {t.status}")
        rec = {"record": self._id("R"), "ticket": t.id, "kind": t.kind, "target": t.target_hint, "fault": None,
               "candidates": [], "admitted": None, "outcome": "", "cost": {"llm_calls": 0, "rollouts": 0}}
        self._prepare(t)
        if t.status != "open":
            rec.update(outcome=t.status, note=t.result.get("reason", ""))
        else:
            self._log("tickets", t.to_dict())
            s = self._session = _Session(t.id)
            try:
                tr = self._execute(self.EVOLVE, {"ticket": t.view()}, {"split": "train", "ticket": t.id}, prefix="meta")
                rec.update(trace=tr.meta["id"], evolve_end=tr.steps[-1].node if tr.steps else None)
                rec["cost"]["llm_calls"] = tr.llm_calls
                if tr.status != "ok":
                    rec["note"] = f"{tr.status}: {tr.detail}"
                report = next((x.output.get("report") for x in tr.steps if x.node == "diagnose"), None)
                if isinstance(report, dict):
                    rec.update(target=report.get("target"),
                               fault={"type": report.get("fault"), "step": report.get("step")})
            except Exception as ex:                 # a crashing meta skill is still on record
                rec["note"] = f"Evolve crashed: {ex!r}"
            finally:
                self._session = None
            for dg, script in s.scripts.items():
                g, k = s.gate0.get(dg), s.checks.get(dg)
                verdict = ("admitted" if dg == s.admitted_digest else
                           f"gate0_fail({','.join(g['failed'])})" if g and not g["ok"] else
                           "not_compared" if k is None else "passed" if k["pass"] else k["reason"])
                rec["candidates"].append({"id": script.get("id"), "ops": op_names(script), "verdict": verdict,
                                          "check": k["id"] if k else None})
            rec["cost"]["rollouts"] = sum(k.get("rollouts", 0) for k in s.checks.values())
            rec["cost"]["rollout_llm_calls"] = sum(k.get("rollout_llm_calls", 0) for k in s.checks.values())
            if s.submit_reason:
                rec["submit_rejected"] = s.submit_reason
            rec.update(admitted=s.admitted, outcome="admitted" if s.admitted else "kept")
            if s.admitted:
                rec["certificate"] = self.certs[s.admitted]["id"]
                self._close(t, "fixed", admitted=s.admitted, record=rec["record"])
            else:
                self._close(t, "unresolved", record=rec["record"])
        self.processed += 1
        self._save_state()
        self.ledger.append(rec)
        self._log("ledger", rec)
        return rec

    def drain(self, limit: int = 50) -> list:
        """Process open tickets until the queue is empty (or the limit is reached)."""
        out = []
        while len(out) < limit and (t := self.next_ticket()) is not None:
            out.append(self.process(t.id))
        return out

    def audit(self) -> list:
        """Re-verify the certificates of all current versions (A8). Returns the recheck tickets opened."""
        return signals.audit(self)
