"""Command line:
    python -m skillgraph check <dir>     run the admission check on every skill in a directory and print the report
    python -m skillgraph show  <dir> <name> <out.svg>   render the graph of a skill (latest version) to SVG
"""
import sys

from . import GateError, SkillLibrary, load_skills, render_skill


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) < 2 or argv[0] not in ("check", "show"):
        print(__doc__)
        return 2
    lib = SkillLibrary()
    results = lib.add_all(load_skills(argv[1]))
    if argv[0] == "show":
        if len(argv) < 4:
            print(__doc__)
            return 2
        print(render_skill(lib.resolve(argv[2]), argv[3]))
        return 0
    bad = 0
    for key, r in results.items():
        if isinstance(r, GateError):
            bad += 1
            print(f"✗ {key} rejected by the admission gate: {r}")
            if hasattr(r.report, "checks"):
                print(r.report)
        else:
            print(r.wf)
            if r.refines:
                print(r.refines)
        print()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
