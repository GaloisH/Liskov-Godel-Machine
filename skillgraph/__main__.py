"""命令行：
    python -m skillgraph check <目录>     对目录下所有 skill 做入库检查，打印报告
    python -m skillgraph show  <目录> <名字> <输出.svg>   把某个 skill（最新版本）的图渲染成 SVG
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
            print(f"✗ {key} 入库被拒绝: {r}")
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
