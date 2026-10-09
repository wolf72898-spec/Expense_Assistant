#!/usr/bin/env python3
"""expense — single entry point to the Expense Assistant engine.

    expense.py doctor                         check Python + packages, show install command
    expense.py locate [--search DIR …]        find this employee's workspace(s)
    expense.py prep  [--ws W] <command> …     session / report / rules / memory engine   (expense_prep.py)
    expense.py pkg   <command> …              single-report builder                      (expense_pkg.py)
    expense.py evolve [--ws W] <command> …    evaluate runs, evolve A-box / personal R-box (evolve.py)
    expense.py item-check --ws W REPORT ITEM  one item's state: fields, gaps, prompts, company requirements
    expense.py install-engine --ws W          copy this engine into the workspace (<ws>/.engine) so any environment can run it
    expense.py version
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
import json
import shutil
import os
import subprocess
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
ROOT = LIB.parent
PREP = LIB / "expense-report-prep" / "scripts" / "expense_prep.py"
PKG = LIB / "expense-report-package" / "scripts" / "expense_pkg.py"
EVOLVE = LIB / "skill-evaluate-and-evolve" / "scripts" / "evolve.py"


def out(o):
    print(json.dumps(o, ensure_ascii=False, indent=2, default=str))


def plugin_version() -> str:
    try:
        return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    except Exception:
        return "unknown"


def run(script: Path, args: list[str]) -> int:
    return subprocess.call([sys.executable, str(script), *args])


def doctor():
    mods = {}
    for m, req in (("yaml", True), ("jsonschema", False), ("pypdf", False), ("typesafe_sdk", None)):
        try:
            mod = __import__(m); mods[m] = "ok"
            if m == "jsonschema" and not hasattr(mod, "Draft202012Validator"):
                mods[m] = "ok (older version; draft-7 validation used, ≥4 recommended)"
        except ImportError:
            mods[m] = "missing (required)" if req else "missing (recommended)" if req is False else \
                "missing (optional: TypeSafe Jev judgments)"
    missing = [{"yaml": "pyyaml", "typesafe_sdk": "typesafe-sdk"}.get(m, m) for m, s in mods.items() if s.startswith("missing")]
    ok = sys.version_info >= (3, 9) and mods["yaml"] == "ok"
    out({"ok": ok, "plugin_version": plugin_version(), "python": sys.version.split()[0],
         "python_ok": sys.version_info >= (3, 9), "packages": mods,
         "install": f"{Path(sys.executable).name} -m pip install {' '.join(missing)}" + (" --break-system-packages" if missing else "")
         if missing else None,
         "company_policy": str(ROOT / "company" / "policy.json") if (ROOT / "company" / "policy.json").exists() else None,
         "workspaces": find_workspaces([])})


def find_workspaces(extra: list[str]) -> list[dict]:
    cands: list[Path] = []
    if os.environ.get("EXPENSE_WS"):
        cands.append(Path(os.environ["EXPENSE_WS"]).expanduser())
    roots = [Path(os.environ["EXPENSE_WS_ROOT"]).expanduser()] if os.environ.get("EXPENSE_WS_ROOT") else []
    roots.append(Path.home() / "ExpenseWorkspace")
    mnt = Path.home() / "mnt"
    if mnt.exists():
        for d in sorted(mnt.iterdir()):
            if d.name.startswith("."):
                continue
            roots.append(d / "ExpenseWorkspace")
            if d.is_dir():
                cands += [p.parent for p in d.glob("*/*/workspace.json")]
                cands += [p.parent for p in d.glob("*/*/*/workspace.json")]
    roots += [Path(e).expanduser() for e in extra]
    for r in roots:
        try:
            ok = r.is_dir()
        except OSError:
            ok = False
        if ok:
            cands += [p.parent for p in r.glob("*/workspace.json")] + [p.parent for p in r.glob("workspace.json")]
    seen, res = set(), []
    for c in cands:
        c = c.resolve()
        if c in seen or not (c / "workspace.json").exists():
            continue
        seen.add(c)
        try:
            cfg = json.loads((c / "workspace.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        cur = (c.parent / ".current")
        eng = c / ".engine" / ".claude-plugin" / "plugin.json"
        res.append({"workspace": str(c), "user": cfg.get("user", {}).get("name"), "side": cfg.get("side"),
                    "current": cur.exists() and cur.read_text().strip() == c.name,
                    "engine": str(c / ".engine" / "lib" / "expense.py") if eng.exists() else None,
                    "engine_version": json.loads(eng.read_text())["version"] if eng.exists() else None})
    return res


def item_check(ws: str, rid: str, iid: str):
    g = subprocess.run([sys.executable, str(PREP), "--ws", ws, "pkg", rid, "gaps"], capture_output=True, text=True)
    if g.returncode:
        sys.exit(g.stderr or g.stdout)
    gaps = json.loads(g.stdout)
    rpt = json.loads((Path(ws) / "drafts" / rid / "report.json").read_text(encoding="utf-8"))
    it = next((i for i in rpt["items"] if i["item_id"] == iid), None)
    if not it:
        sys.exit(f"error: unknown item {iid}")
    attrs = ["--category", it.get("category", "other")]
    if (it.get("trip") or {}).get("city"):
        attrs += ["--city", it["trip"]["city"]]
    if (it.get("amount") or {}).get("currency"):
        attrs += ["--currency", it["amount"]["currency"]]
    if any(p.get("method") == "ocr" for p in (it.get("provenance") or {}).values()):
        attrs += ["--method", "ocr"]
    h = subprocess.run([sys.executable, str(PREP), "--ws", ws, "rules", "hints", *attrs], capture_output=True, text=True)
    hints = json.loads(h.stdout) if h.returncode == 0 else {}
    mine = [x for x in gaps["gaps"] if x.get("item_id") == iid]
    low = {f: p.get("confidence") for f, p in (it.get("provenance") or {}).items()
           if p.get("confidence") is not None and p["confidence"] < 0.8}
    out({"item": {k: it.get(k) for k in ("item_id", "category", "description", "expense_date", "merchant", "amount",
                                          "amount_reimbursable", "project", "trip", "attendees", "evidence_ids", "status")},
         "status": it.get("status"),
         "blockers": [x for x in mine if x["severity"] == "blocker"],
         "warnings": [x for x in mine if x["severity"] != "blocker"],
         "low_confidence_fields": low,
         "ask_now": hints.get("ask_now", []), "company_requires": hints.get("company_requires", []),
         "report_status": gaps["status"]})


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "doctor":
        doctor(); return 0
    if cmd == "version":
        out({"plugin_version": plugin_version()}); return 0
    if cmd == "install-engine":
        ws = Path(rest[rest.index("--ws") + 1]).expanduser()
        if not (ws / "workspace.json").exists():
            sys.exit(f"error: {ws} is not an expense workspace")
        dest = ws / ".engine"                            # overwrite in place: deleting may be forbidden
        shutil.copytree(LIB, dest / "lib", ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
        for d in ("company", ".claude-plugin"):
            if (ROOT / d).exists():
                shutil.copytree(ROOT / d, dest / d, dirs_exist_ok=True)
        out({"engine": str(dest / "lib" / "expense.py"), "version": plugin_version()}); return 0
    if cmd == "locate":
        extra = [rest[i + 1] for i, a in enumerate(rest) if a == "--search" and i + 1 < len(rest)]
        out(find_workspaces(extra)); return 0
    if cmd == "prep":
        return run(PREP, rest)
    if cmd == "pkg":
        return run(PKG, rest)
    if cmd == "evolve":
        return run(EVOLVE, rest)
    if cmd == "item-check":
        ws = rest[rest.index("--ws") + 1] if "--ws" in rest else os.environ.get("EXPENSE_WS")
        pos = [a for i, a in enumerate(rest) if a != "--ws" and (i == 0 or rest[i - 1] != "--ws")]
        if not ws or len(pos) != 2:
            sys.exit("usage: expense.py item-check --ws WORKSPACE REPORT_ID ITEM_ID")
        item_check(ws, pos[0], pos[1]); return 0
    sys.exit(f"unknown command {cmd}\n{__doc__}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
