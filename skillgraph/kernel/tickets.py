"""Tickets: the single entry point of every improvement (Step 2 design doc §4)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

KINDS = ("failure", "audit", "create", "recheck", "meta")
# open → fixed       a new version was admitted for this ticket
#      → unresolved  Evolve ran and gave up (the current version is kept)
#      → resolved    the evidence no longer fails under the current library (a newer version already fixed it)
#      → suspended   no usable evidence; waits for a human
STATUSES = ("open", "fixed", "unresolved", "resolved", "suspended")


@dataclass
class Ticket:
    id: str
    kind: str
    target_hint: str                    # where the problem was seen; blame assignment may move it to another skill
    evidence: dict = field(default_factory=dict)    # traces: [trace ids], signal: str, counterexample: {...}
    opened_by: str = "kernel"           # kernel = mandatory signal; audit = found by the Audit skill
    priority: float = 0.0               # set by the Audit skill's policy
    deadline: int = 20                  # a mandatory ticket must be handled within this many processed tickets
    opened_at: int = 0                  # number of tickets processed when this one was opened
    status: str = "open"
    result: dict = field(default_factory=dict)

    @property
    def mandatory(self) -> bool:
        return self.opened_by == "kernel"

    def to_dict(self) -> dict:
        return asdict(self)

    def view(self) -> dict:
        """What a meta skill gets to see: the ticket without its bookkeeping."""
        return {k: v for k, v in asdict(self).items() if k not in ("status", "result")}

    @staticmethod
    def from_dict(d: dict) -> "Ticket":
        return Ticket(**d)
