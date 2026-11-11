"""Skill library: versioning, admission gate, statistics layer (design doc §1 statistics layer, §8.2).

Admission rules (part of the trusted kernel):
  1. must pass the well-formedness checks W0–W7 and the budget check;
  2. a minor version bump must satisfy the interface refinement rules, otherwise only a new major version can be released;
  3. when the budget changes, callers that would automatically pick up the new version are re-checked;
  4. every version carries a certificate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Skill, Version
from .refine import RefineReport, check_refinement
from .trace import TV, Trace
from .wellformed import Report, check_skill


class GateError(Exception):
    """Rejected by the admission gate."""

    def __init__(self, msg, report=None):
        super().__init__(msg)
        self.report = report


@dataclass
class Beta:
    a: float = 1.0
    b: float = 1.0

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    def __str__(self):
        return f"Beta({self.a:g}, {self.b:g})  mean {self.mean:.2f}"


@dataclass
class Stats:
    """Statistics layer: does not change what a skill means, only records how it actually performs."""
    reliability: dict = field(default_factory=dict)     # agent / call node → Beta posterior of keeping its contract
    fault_models: dict = field(default_factory=dict)    # node → declared fault model
    history: list = field(default_factory=list)         # trace history H
    outcomes: dict = field(default_factory=dict)        # outcome label counts
    post_safe: Beta = field(default_factory=Beta)       # overall reliability: posterior of Post_safe in post-hoc evaluation


@dataclass
class Certificate:
    skill: str
    wf: Report
    w8_proved: bool
    refines: RefineReport | None = None     # refinement check against the previous version

    def __str__(self):
        s = f"certificate {self.skill}: well-formed ✓, spec consistency {'proved' if self.w8_proved else 'unproved'}"
        if self.refines:
            s += f", vs {self.refines.old}: {'compatible interface' if self.refines.compatible else 'incompatible interface (major version)'}"
        return s


@dataclass
class Entry:
    skill: Skill
    certificate: Certificate
    stats: Stats


class SkillLibrary:
    def __init__(self):
        self._entries: dict[str, dict[Version, Entry]] = {}

    # ------------------------------------------------------------ queries

    def versions(self, name: str) -> list[Version]:
        return sorted(self._entries.get(name, {}))

    def get(self, name: str, version) -> Skill:
        return self._entries[name][Version.parse(version)].skill

    def entry(self, name: str, version) -> Entry:
        return self._entries[name][Version.parse(version)]

    def stats(self, name: str, version) -> Stats:
        return self.entry(name, version).stats

    def resolve(self, name: str, min_version: Version | None = None) -> Skill | None:
        """Resolve "name >= min_version": the latest minor version within the same major version.

        Without a lower bound, the latest version in the library. Major versions have incompatible interfaces, so resolution never upgrades across them.
        """
        vs = self.versions(name)
        if min_version is not None:
            vs = [v for v in vs if v.major == min_version.major and v >= min_version]
        return self._entries[name][vs[-1]].skill if vs else None

    def callers_of(self, name: str) -> list[Skill]:
        return [e.skill for d in self._entries.values() for e in d.values()
                if any(n.skill == name for n in e.skill.calls())]

    # ------------------------------------------------------------ admission

    def add(self, skill: Skill) -> Certificate:
        have = self._entries.setdefault(skill.name, {})
        if skill.version in have:
            raise GateError(f"{skill.key} is already in the library")
        report = check_skill(skill, self.resolve)
        if not report.ok:
            raise GateError(f"{skill.key} failed the well-formedness check {report.failed()}", report)
        older = [v for v in have if v < skill.version]
        prev = have[max(older)].skill if older else None
        refines = check_refinement(prev, skill) if prev else None
        if prev and prev.version.major == skill.version.major and not refines.compatible:
            raise GateError(f"the interface of {skill.key} is incompatible with {prev.key}; it can only be released as major version "
                            f"v{skill.version.major + 1}.0\n{refines}", refines)
        cert = Certificate(skill.key, report, report.w8, refines)
        stats = Stats(reliability={n.id: Beta(*n.prior) if n.kind == "agent" else Beta()
                                   for n in skill.nodes.values() if n.kind in ("agent", "call")},
                      fault_models={n.id: n.fault_model for n in skill.nodes.values() if n.fault_model})
        have[skill.version] = Entry(skill, cert, stats)
        if refines and refines.budget_changed:
            # the budget is the only item that must be re-checked up the call chain
            for caller in self.callers_of(skill.name):
                r = check_skill(caller, self.resolve)
                if not r.ok:
                    del have[skill.version]
                    raise GateError(f"the budget change of {skill.key} makes caller {caller.key} ill-formed {r.failed()}", r)
        return cert

    def add_all(self, skills) -> dict:
        """Batch admission: callees are ordered first automatically. Returns skill.key → certificate or GateError."""
        todo, out = sorted(skills, key=lambda s: (s.version, s.name)), {}
        while todo:
            ready = [s for s in todo if all(self.resolve(n.skill, n.min_version) for n in s.calls())] or list(todo)
            for s in ready:
                try:
                    out[s.key] = self.add(s)
                except GateError as ex:
                    out[s.key] = ex
                todo.remove(s)
        return out

    # ------------------------------------------------------------ statistics updates

    def record(self, tr: Trace):
        """Record a run (ideally after post-hoc evaluation) in the statistics layer: update reliability posteriors and store the trace."""
        if tr.status == "frontier":
            return
        st = self.stats(tr.skill, tr.version)
        st.history.append(tr)
        st.outcomes[tr.outcome] = st.outcomes.get(tr.outcome, 0) + 1
        if tr.post_eval and tr.outcome == "done":
            self._bump(st.post_safe, tr.post_eval["post_safe"])
        for s in tr.steps:
            if s.node in st.reliability:
                self._bump(st.reliability[s.node], s.contract_post)
            if s.sub_trace:
                self.record(s.sub_trace)

    @staticmethod
    def _bump(beta: Beta, tv):
        if tv is TV.T:
            beta.a += 1
        elif tv is TV.F:
            beta.b += 1         # ? is not counted
