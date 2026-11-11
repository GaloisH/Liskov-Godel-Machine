"""Toy world: a tiny two-file repository plus tests that really run.

Agents are scripted (outputs written in advance), but tools and the judge are real: a patch is complete Python source,
and running tests means exec-ing the source and calling the test_* functions. So phenomena like "the repro test passes but a regression is introduced" really happen.

Conventions: patch = {file path: new full content}; repo = {file path: content}; test files start with test_.
"""
from __future__ import annotations

from skillgraph import Predicates

# ---------------------------------------------------------------- repository

_HEAD = '''
def days_in_month(y, m):
    if m == 2:
        leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
        return 29 if leap else 28
    return 30 if m in (4, 6, 9, 11) else 31

def parse_date(s):
    y, m, d = (int(x) for x in s.split("-"))
'''


def _src(body: str) -> str:
    return _HEAD + "".join(f"    {line}\n" for line in body.strip().splitlines())


REPO = {
    "date_utils.py": _src("return (y, m, d)"),              # bug: no validation at all
    "test_date_utils.py": '''
def test_basic():
    assert parse_date("2023-12-31") == (2023, 12, 31)

def test_leap_day():
    assert parse_date("2024-02-29") == (2024, 2, 29)
''',
}

# four patches of increasing quality
D1_NOFIX = {"date_utils.py": _src("""
if d < 1:
    raise ValueError(s)
return (y, m, d)""")}
D2_REGRESS = {"date_utils.py": _src("""
if d < 1 or d > (28 if m == 2 else days_in_month(y, m)):     # forgets leap years
    raise ValueError(s)
return (y, m, d)""")}
D3_DAY_ONLY = {"date_utils.py": _src("""
if d < 1 or d > days_in_month(y, m):                         # days are right, but months are not validated
    raise ValueError(s)
return (y, m, d)""")}
D4_CORRECT = {"date_utils.py": _src("""
if not 1 <= m <= 12:
    raise ValueError(s)
if d < 1 or d > days_in_month(y, m):
    raise ValueError(s)
return (y, m, d)""")}
D_CHEAT = {**D1_NOFIX, "test_date_utils.py": "def test_basic():\n    pass\n"}     # cheating: edits the tests
D_MESSY = {"date_utils.py": D4_CORRECT["date_utils.py"] + "\nprint('debug')  # TODO remove\n"}

_T_INVALID_DAY = '''
def test_invalid_day():
    try:
        parse_date("2024-02-30")
    except ValueError:
        return
    assert False, "should raise ValueError"
'''
_T_INVALID_MONTH = '''
def test_invalid_month():
    try:
        parse_date("2024-13-01")
    except ValueError:
        return
    assert False, "should raise ValueError"
'''

ISSUE = {"text": "parse_date('2024-02-30') should raise ValueError",
         "repro": {"name": "test_invalid_day", "source": _T_INVALID_DAY}}
ISSUE_NO_REPRO = {"text": "date parsing seems a bit off", "repro": None}
SPEC = {"text": "parse_date must reject nonexistent dates",
        "acceptance": {"test_invalid_day": _T_INVALID_DAY, "test_invalid_month": _T_INVALID_MONTH}}


# ---------------------------------------------------------------- actually running tests

def is_test_file(path: str) -> bool:
    return path.rsplit("/", 1)[-1].startswith("test_")


def run_all(repo: dict, patch: dict, extra: dict | None = None) -> dict:
    """Apply the patch and run all tests. Returns test name → passed. extra holds additional test sources (name → source)."""
    files = {**repo, **(patch or {})}
    ns: dict = {}
    try:
        for path, src in files.items():
            if not is_test_file(path):
                exec(src, ns)
        tests = dict(ns)
        for path, src in files.items():
            if is_test_file(path):
                exec(src, tests)
        for src in (extra or {}).values():
            if src:
                exec(src, tests)
    except Exception:
        return {"<load>": False}
    out = {}
    for name in [n for n in tests if n.startswith("test_") and callable(tests[n])]:
        try:
            tests[name]()
            out[name] = True
        except Exception:
            out[name] = False
    return out


def _repro(issue):
    r = issue.get("repro") or {}
    return r.get("name"), {r["name"]: r.get("source", "")} if r else {}


def passes_repro(patch, issue, repo) -> bool:
    name, extra = _repro(issue)
    return bool(run_all(repo, patch, extra).get(name, False))


def passes_suite(patch, repo) -> bool:
    return all(run_all(repo, patch).values())


def passes_acceptance(patch, spec, repo) -> bool:
    res = run_all(repo, patch, spec["acceptance"])
    return all(res.get(n, False) for n in spec["acceptance"])


# ---------------------------------------------------------------- tool nodes

def debug_run_tests(repo, patch, issue) -> dict:
    return {"rep": passes_repro(patch, issue, repo), "suite": passes_suite(patch, repo)}


def coding_run_tests(repo, patch, spec) -> dict:
    """Run the acceptance tests + existing tests; on failure, wrap the **first** failing test into an issue."""
    res = run_all(repo, patch, spec["acceptance"])
    failing = [n for n in list(spec["acceptance"]) + sorted(res) if not res.get(n, False)]
    if not failing:
        return {"all_pass": True, "failing_report": {"text": "", "repro": None}}
    name = failing[0]
    return {"all_pass": False, "failing_report": {
        "text": f"{name} failed", "repro": {"name": name, "source": spec["acceptance"].get(name, "")}}}


TOOLS = {
    "debug.run_tests": debug_run_tests,
    "coding.run_tests": coding_run_tests,
    "review.no_feedback": lambda: {"feedback": ""},
    "review.run_suite": lambda repo, patch: {"ok": passes_suite(patch, repo)},
}

# ---------------------------------------------------------------- predicates

PREDICATES = Predicates(
    # computable predicates available at run time
    runtime={
        "has_repro_test": lambda issue: bool(issue.get("repro")),
        "has_acceptance_tests": lambda spec: bool(spec.get("acceptance")),
        "files": lambda patch: frozenset(patch),
        "touches_tests": lambda patch: any(is_test_file(p) for p in patch),
    },
    # judge used only in post-hoc evaluation (in reality hidden tests, reference answers or humans; at run time these predicates are always ?)
    judge={
        "passes_repro": passes_repro,
        "passes_suite": passes_suite,
        "passes_acceptance": passes_acceptance,
        "relevant": lambda F, issue: "date_utils.py" in F,
        "clean": lambda patch: not any("TODO" in s or "print(" in s for s in patch.values()),
        "describes": lambda doc, code: "parse_date" in doc,
    },
)
