#!/usr/bin/env python3
"""expense_prep — employee-side expense workspace (session layer over expense_pkg).

Layout (one folder per user):
    <ws>/workspace.json                 owner + settings
    <ws>/inbox/                         drop vouchers here (tracked by sha256)
    <ws>/drafts/<report_id>/            live drafts (managed by expense_pkg)
    <ws>/outputs/<report_id>/<id>_vNNN.zip + <id>_vNNN.manifest.json + latest.json
    <ws>/outbox/policy_feedback/        proposals for company rules (picked up by the finance side)
    <ws>/ontology/tbox.yaml             T-box: expense segment of the personal ontology
    <ws>/ontology/rbox/core.yaml        R-box axioms (characteristics, inverses, chains, learning)
    <ws>/ontology/rbox/org.yaml         R-box company rules (read-only on the employee side)
    <ws>/ontology/rbox/personal.yaml    R-box personal rules (recall, prompts, checks, evolution meta-rules)
    <ws>/ontology/rbox/compiled/        policy.json + rules.index.json compiled from org + personal
    <ws>/ontology/obsidian/             optional wiki export of the A-box
    <ws>/db/expense.db                  SQLite: relational (reports, versions, inputs, sessions),
                                        document (report_docs JSON), property graph (onto_node/onto_edge = A-box)
    <ws>/tools/expense-report-package/  materialised expense_pkg + schemas + policy
    <ws>/logs/

Requires Python 3.9+, PyYAML. `jsonschema`, `pypdf` optional (used by expense_pkg).
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("error: PyYAML required — pip install pyyaml")

# 青禾修改：新增 PROJECT_ROOT，用于定位青禾企业制度包 —— 见 CHANGES.md
SKILL_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = Path(os.environ.get("EXPENSE_WS_ROOT", Path.home() / "ExpenseWorkspace"))
EVIDENCE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff", ".ofd", ".xml"}
APPENDIX_RE = re.compile(r"^## Appendix [A-Z] — `([^`]+)`.*?\n```[\w-]*\n(.*?)^```", re.S | re.M)
CATEGORY_MERCHANT_CLASS = {
    "lodging": "exp:Hotel", "transport.air": "exp:Airline", "transport.rail": "exp:RailOperator",
    "transport.taxi": "exp:TaxiService", "meals": "exp:Restaurant", "entertainment": "exp:Restaurant",
}
CATEGORY_PLACE_CLASS = {"transport.rail": "exp:Station", "transport.air": "exp:Airport"}

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS reports(
  report_id TEXT PRIMARY KEY, title TEXT, status TEXT, employee TEXT, project_code TEXT, purpose TEXT,
  period_start TEXT, period_end TEXT, currency TEXT, grand_total REAL, item_count INTEGER,
  open_blockers INTEGER, draft_path TEXT, latest_version INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS report_versions(
  report_id TEXT, version INTEGER, label TEXT, zip_path TEXT, manifest_path TEXT, zip_sha256 TEXT,
  report_sha256 TEXT, status TEXT, grand_total REAL, item_count INTEGER, gaps_blocker INTEGER,
  gaps_warning INTEGER, parent_version INTEGER, session_id TEXT, note TEXT, created_at TEXT, submitted_at TEXT,
  PRIMARY KEY(report_id, version));
CREATE TABLE IF NOT EXISTS report_docs(
  report_id TEXT, version INTEGER, report_json TEXT, manifest_json TEXT, PRIMARY KEY(report_id, version));
CREATE TABLE IF NOT EXISTS inputs(
  sha256 TEXT PRIMARY KEY, path TEXT, ext TEXT, bytes INTEGER, first_seen TEXT, status TEXT,
  report_id TEXT, evidence_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, goal TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS session_events(session_id TEXT, seq INTEGER, at TEXT, kind TEXT, payload TEXT,
  PRIMARY KEY(session_id, seq));
CREATE TABLE IF NOT EXISTS onto_schema(box TEXT PRIMARY KEY, version TEXT, content TEXT, loaded_at TEXT);
CREATE TABLE IF NOT EXISTS onto_node(id TEXT PRIMARY KEY, class TEXT, label TEXT, aliases TEXT DEFAULT '[]',
  props TEXT DEFAULT '{}', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS onto_edge(id INTEGER PRIMARY KEY AUTOINCREMENT, subj TEXT, pred TEXT, obj TEXT,
  basis TEXT, source TEXT, confidence REAL DEFAULT 1.0, qualifiers TEXT DEFAULT '{}', first_seen TEXT, last_seen TEXT,
  UNIQUE(subj, pred, obj, basis, source));
CREATE INDEX IF NOT EXISTS ix_edge_sp ON onto_edge(subj, pred);
CREATE INDEX IF NOT EXISTS ix_edge_op ON onto_edge(obj, pred);
CREATE INDEX IF NOT EXISTS ix_edge_src ON onto_edge(source);
CREATE TABLE IF NOT EXISTS rbox_versions(layer TEXT, version INTEGER, content TEXT, sha256 TEXT, actor TEXT, note TEXT,
  created_at TEXT, PRIMARY KEY(layer, version));
"""
RBOX_LAYERS = ["core", "org", "personal"]


# ───────────────────────── helpers ─────────────────────────
def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def norm(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip()).lower()


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", name.strip().lower(), flags=re.U).strip("-")
    return s or "user"


def extract_appendices(skill_md: Path, dest: Path) -> list[str]:
    """Materialise every '## Appendix X — `path`' code block of a SKILL.md into dest/path."""
    # 青禾修改：上游此处无存在性判断，目录内无 SKILL.md 时会崩溃 —— 见 CHANGES.md
    if not skill_md.exists():
        return []
    text = skill_md.read_text(encoding="utf-8")
    written = []
    for rel, body in APPENDIX_RE.findall(text):
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        written.append(rel)
    return written


def find_pkg_skill(hint: str | None) -> Path | None:
    cands = []
    if hint:
        cands.append(Path(hint).expanduser())
    if os.environ.get("EXPENSE_PKG_SKILL"):
        cands.append(Path(os.environ["EXPENSE_PKG_SKILL"]).expanduser())
    cands.append(SKILL_ROOT.parent / "expense-report-package")
    for c in cands:                       # explicit hints and the bundled copy win; no scanning needed
        if c.exists() and (c.name == "SKILL.md" or (c / "scripts" / "expense_pkg.py").exists() or (c / "SKILL.md").exists()):
            return c
    for base in [Path.home() / ".claude" / "skills", Path("/root/.claude/skills"), Path.cwd()]:
        try:
            if base.exists():
                cands += list(base.glob("**/expense-report-package"))
        except (PermissionError, OSError):   # sandboxed hosts may deny these folders
            continue
    for c in cands:
        if c.is_file() and c.name == "SKILL.md":
            return c
        if (c / "scripts" / "expense_pkg.py").exists() or (c / "SKILL.md").exists():
            return c
    return None


# ───────────────────────── workspace ─────────────────────────
class WS:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "db" / "expense.db"
        self.pkg = root / "tools" / "expense-report-package" / "scripts" / "expense_pkg.py"
        self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(self.db_path)
            self._db.row_factory = sqlite3.Row
            # TRUNCATE keeps the journal file instead of deleting it — works on synced / sandboxed
            # folders where deleting files is not permitted (default DELETE mode fails there).
            self._db.execute("PRAGMA journal_mode=TRUNCATE")
            self._db.executescript(DDL)
        return self._db

    def cfg(self) -> dict:
        return json.loads((self.root / "workspace.json").read_text(encoding="utf-8"))

    def meta(self, key, value=None, delete=False):
        if delete:
            self.db.execute("DELETE FROM meta WHERE key=?", (key,)); self.db.commit(); return None
        if value is None:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value)); self.db.commit()

    def draft(self, rid: str) -> Path:
        return self.root / "drafts" / rid

    def log(self, kind: str, payload: dict) -> None:
        sid = self.meta("current_session")
        if not sid:
            return
        seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM session_events WHERE session_id=?", (sid,)).fetchone()[0]
        self.db.execute("INSERT INTO session_events VALUES(?,?,?,?,?)",
                        (sid, seq, now(), kind, json.dumps(payload, ensure_ascii=False, default=str)))
        self.db.commit()

    def invoice_ledger(self, exclude: str) -> dict:
        """invoice_no → 'RPT…/ITM…' for every non-excluded item in the employee's *other* reports (org rule kind invoice_unique)."""
        led = {}
        for d in sorted((self.root / "drafts").glob("*/report.json")):
            if d.parent.name == exclude:
                continue
            try:
                rep = json.loads(d.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for it in rep.get("items", []):
                no = (it.get("invoice") or {}).get("invoice_no")
                if no and it.get("status") != "excluded":
                    led.setdefault(no, f"{d.parent.name}/{it['item_id']}")
        return led

    def secret_path(self, name: str) -> Path:
        custom = (self.cfg().get("secrets") or {}).get("dir")
        base = Path(custom).expanduser() if custom else self.root / ".secrets"
        return base / f"{name}.key"

    def secret(self, name: str) -> str | None:
        env = {"typesafe": "TYPESAFE_API_KEY"}.get(name)
        if env and os.environ.get(env):
            return os.environ[env]
        p = self.secret_path(name)
        try:
            v = p.read_text(encoding="utf-8").strip()
            return v or None
        except OSError:
            return None

    def jev_env(self) -> dict:
        """Environment for a judge run: the key from the secret store, model settings from workspace.json › jev."""
        env = dict(os.environ)
        key = self.secret("typesafe")
        if key:
            env["TYPESAFE_API_KEY"] = key
        j = self.cfg().get("jev") or {}
        if j.get("model"):
            env["TYPESAFE_DEFAULT_MODEL"] = j["model"]
        if j.get("base_url"):
            env["TYPESAFE_BASE_URL"] = j["base_url"]
        return env

    def run_pkg(self, sub: str, args: list[str], rid: str | None = None) -> tuple[int, str, str]:
        cmd = [sys.executable, str(self.pkg), sub]
        if rid:
            cmd += ["--draft", str(self.draft(rid))]
            if self.draft(rid).exists():
                (self.draft(rid) / "context.json").write_text(json.dumps(
                    {"ledger": self.invoice_ledger(rid), "projects": project_registry(self)}, ensure_ascii=False), encoding="utf-8")
        cmd += args
        env = self.jev_env() if sub in ("judge", "ingest-xml") else None   # the key only reaches the Jev subprocesses
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return p.returncode, p.stdout, p.stderr


def resolve_ws(arg: str | None) -> WS:
    if arg:
        root = Path(arg).expanduser()
    elif os.environ.get("EXPENSE_WS"):
        root = Path(os.environ["EXPENSE_WS"]).expanduser()
    else:
        cur = DEFAULT_ROOT / ".current"
        if not cur.exists():
            sys.exit(f"error: no workspace — run `init --user NAME` (root {DEFAULT_ROOT})")
        root = DEFAULT_ROOT / cur.read_text().strip()
    if not (root / "workspace.json").exists():
        sys.exit(f"error: {root} is not an expense workspace")
    return WS(root)


# ───────────────────────── ontology store (A-box) ─────────────────────────
def node_upsert(ws: WS, nid: str, cls: str, label: str | None = None, props: dict | None = None,
                aliases: list[str] | None = None) -> None:
    row = ws.db.execute("SELECT * FROM onto_node WHERE id=?", (nid,)).fetchone()
    ts = now()
    if row:
        p = json.loads(row["props"]); p.update({k: v for k, v in (props or {}).items() if v not in (None, "")})
        al = sorted(set(json.loads(row["aliases"])) | set(aliases or []))
        ws.db.execute("UPDATE onto_node SET class=?, label=COALESCE(?,label), props=?, aliases=?, updated_at=? WHERE id=?",
                      (cls or row["class"], label, json.dumps(p, ensure_ascii=False), json.dumps(al, ensure_ascii=False), ts, nid))
    else:
        ws.db.execute("INSERT INTO onto_node VALUES(?,?,?,?,?,?,?)",
                      (nid, cls, label or nid, json.dumps(sorted(set(aliases or [])), ensure_ascii=False),
                       json.dumps({k: v for k, v in (props or {}).items() if v not in (None, "")}, ensure_ascii=False), ts, ts))


def edge_add(ws: WS, s: str, p: str, o: str, basis: str, source: str, confidence: float = 1.0,
             qualifiers: dict | None = None) -> None:
    ts = now()
    ws.db.execute("""INSERT INTO onto_edge(subj,pred,obj,basis,source,confidence,qualifiers,first_seen,last_seen)
                     VALUES(?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(subj,pred,obj,basis,source) DO UPDATE SET last_seen=excluded.last_seen,
                     confidence=MAX(confidence, excluded.confidence), qualifiers=excluded.qualifiers""",
                  (s, p, o, basis, source, confidence, json.dumps(qualifiers or {}, ensure_ascii=False), ts, ts))


def load_box(ws: WS, box: str) -> dict:
    if box == "rbox":
        return load_rbox(ws)
    return yaml.safe_load((ws.root / "ontology" / f"{box}.yaml").read_text(encoding="utf-8"))


# ───────────────────────── R-box layers (core / org / personal) ─────────────────────────
def schema_validator(jsonschema, schema):
    """Draft 2020-12 when available (jsonschema ≥ 4), else the newest draft the installed version knows."""
    cls = getattr(jsonschema, "Draft202012Validator", None)
    if cls is None:   # jsonschema 3.x: drop the urn $id so local "#/$defs/…" refs resolve in-document
        schema = {k: v for k, v in schema.items() if k != "$id"}
        cls = jsonschema.validators.validator_for(schema)
    return cls(schema, format_checker=jsonschema.FormatChecker())


def layer_path(ws: WS, layer: str) -> Path:
    return ws.root / "ontology" / "rbox" / f"{layer}.yaml"


def load_layer(ws: WS, layer: str) -> dict:
    return yaml.safe_load(layer_path(ws, layer).read_text(encoding="utf-8")) or {}


def dump_yaml(d: dict) -> str:
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=140)


def snapshot_layer(ws: WS, layer: str, actor: str, note: str) -> None:
    content = layer_path(ws, layer).read_text(encoding="utf-8")
    ver = int(yaml.safe_load(content).get("version") or 1) if layer != "org" else \
        (ws.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM rbox_versions WHERE layer='org'").fetchone()[0])
    ws.db.execute("INSERT OR REPLACE INTO rbox_versions VALUES(?,?,?,?,?,?,?)",
                  (layer, ver, content, sha256_bytes(content.encode()), actor, note, now()))
    ws.db.commit()


def save_layer(ws: WS, layer: str, data: dict, actor: str, note: str) -> int:
    if layer != "org":
        data["version"] = int(data.get("version") or 0) + 1
    layer_path(ws, layer).write_text(dump_yaml(data), encoding="utf-8")
    snapshot_layer(ws, layer, actor, note)
    return data.get("version")


def layer_versions(ws: WS) -> dict:
    return {l: ws.db.execute("SELECT MAX(version) FROM rbox_versions WHERE layer=?", (l,)).fetchone()[0] for l in RBOX_LAYERS}


def load_rbox(ws: WS) -> dict:
    """Merged view used by inference and recall."""
    core, personal = load_layer(ws, "core"), load_layer(ws, "personal")
    return {**{k: core.get(k) for k in ("characteristics", "inverses", "chains", "weighting", "learning")},
            "suggest": [r for r in personal.get("recall", []) if r.get("mode") != "off"],
            "prompts": personal.get("prompts", []), "evolution": personal.get("evolution", {})}


def policy_to_org(policy: dict, source: str) -> dict:
    """Convert a legacy policy.json into org-layer business rules (one rule per atomic constraint)."""
    rules, labels = [], {}
    for cat, spec in policy.get("categories", {}).items():
        if spec.get("label"):
            labels[cat] = spec["label"]
        for f in spec.get("required_fields", []):
            rules.append({"id": f"ORG-FLD-{cat}-{f}", "kind": "required_field", "applies_to": {"category": cat}, "field": f})
        for i, g in enumerate(spec.get("required_evidence", []), 1):
            rules.append({"id": f"ORG-EVD-{cat}-{i}", "kind": "required_evidence", "applies_to": {"category": cat}, "any_of": g})
        for i, g in enumerate(spec.get("recommended_evidence", []), 1):
            rules.append({"id": f"ORG-REC-{cat}-{i}", "kind": "recommended_evidence", "applies_to": {"category": cat}, "any_of": g})
        cap = spec.get("cap")
        if cap:
            # 青禾修改：per/mode 改为容错取值。财务填制度时漏填 per 会导致整个工作区初始化崩溃 —— 见 CHANGES.md
            per = cap.get("per", "item")
            rules.append({"id": f"ORG-CAP-{cat}", "kind": "cap", "applies_to": {"category": cat}, "amount": cap.get("amount", 0),
                          "per": per, "mode": cap.get("mode", "warn")})
            for city, amt in (cap.get("by_city") or {}).items():
                rules.append({"id": f"ORG-CAP-{cat}-{city}", "kind": "cap", "applies_to": {"category": cat, "city": city},
                              "amount": amt, "per": per, "mode": cap.get("mode", "warn")})
    rules += [dict(r) for r in policy.get("receipt_rules", [])]          # company receipt / invoice R-box (native form)
    return {"layer": "org", "authority": "company", "version": policy.get("policy_id", "1"), "source": source,
            "settings": {"report_currency": policy.get("report_currency", "CNY"),
                         "require_project": policy.get("require_project", True),
                         "require_purpose": policy.get("require_purpose", True),
                         **({"submission_window_days": policy["submission_window_days"]}
                            if policy.get("submission_window_days") else {}),
                         **({"invoice_title": policy["invoice_title"]} if policy.get("invoice_title") else {}),
                         **({"invoice_titles": policy["invoice_titles"]} if policy.get("invoice_titles") else {}),
                         **({"organization": policy["organization"]} if policy.get("organization") else {}),
                         **{k: policy[k] for k in ("grades", "grade_labels", "exception_policy", "chart_of_accounts", "allocation")
                            if policy.get(k)},
                         "labels": labels},
            "rules": rules,
            **({"rule_sets": policy["rule_sets"]} if policy.get("rule_sets") else {})}


def evidence_req(g: list[str]) -> str:
    return f"evidence:any_of[{','.join(g)}]"


VAT_TYPES = ["vat_special", "vat_general", "e_invoice", "rail_e_ticket"]
MODULE_ORDER = ["tenant", "ingestion", "verification", "save", "reimbursement", "oa_integrity", "transfer_delete", "metadata"]


def compile_receipt(org: dict, index: dict) -> tuple[dict, list]:
    """Company receipt / invoice rules (org layer) → policy.receipt (checks the package can run) + policy.workflow
    (backend BIP / OA steps the package guides the employee through). Every requirement is indexed to its rule id."""
    rules = [r for r in org.get("rules", []) if r.get("status", "active") == "active" and not r["applies_to"].get("category")]
    rc: dict = {}
    for r in rules:
        k = r["kind"]
        if k == "invoice_verification":
            rc["verification"] = {"rule_id": r["id"], "invoice_types": r.get("invoice_types") or VAT_TYPES,
                                  "verify_fields": r.get("verify_fields", {}), "success_state": r.get("success_state", "相符"),
                                  "severity": r.get("severity", "warning"), "currency": r["applies_to"].get("currency", "CNY")}
            index["*|receipt:verify"] = r["id"]
        elif k == "invoice_status":
            rc.setdefault("status_rules", []).append({x: r[x] for x in ("id", "field", "allowed", "blocked", "severity", "action",
                                                                        "text", "blocking") if x in r})
            if r.get("severity") and not r.get("action"):
                index.setdefault(f"*|status:{r['field']}", r["id"])
        elif k == "invoice_line_items":
            rc["line_items"] = {"rule_id": r["id"], "invoice_types": r.get("invoice_types") or VAT_TYPES,
                                "severity": r.get("severity", "warning"), "remediation": r.get("remediation", "")}
            index["*|field:invoice.line_items"] = r["id"]
        elif k == "invoice_unique":
            rc["unique"] = {"rule_id": r["id"], "severity": r.get("severity", "blocker")}
            index["*|unique:invoice_no"] = r["id"]
        elif k == "ingestion_route":
            rc.setdefault("routes", []).append({x: r[x] for x in ("id", "source_form", "method", "formats", "fallback", "text") if x in r})
            index.setdefault("*|source_file", r["id"])
        elif k == "summary_name":
            rc["summary_name"] = {"rule_id": r["id"], "template": r.get("template", "{project_code} {report_title}")}
    if rc.get("verification") and rc["verification"]["success_state"]:
        for s in rc.get("status_rules", []):   # 查验结果 success state is one fact, stated once
            if s.get("field") == "verify_result":
                s.setdefault("allowed", [rc["verification"]["success_state"]])
    wf = sorted(({x: r[x] for x in ("id", "module", "stage", "enforced_by", "text", "scope") if x in r}
                 for r in rules if r.get("text")),
                key=lambda x: (MODULE_ORDER.index(x["module"]) if x.get("module") in MODULE_ORDER else 99, x["id"]))
    return rc, wf


def rule_sets(org: dict) -> list[dict]:
    sets = org.get("rule_sets") or []
    return sets if any(s["id"] == "standard" for s in sets) else [{"id": "standard", "label": "标准规则集"}, *sets]


def apply_rule_set(org: dict, rs_id: str) -> dict:
    """Effective org layer for one rule set: the standard rules with the set's overrides (and those of the sets it
    extends) applied. Every rule a set changes or adds is re-labelled `<id>@<set>`, so gaps name the override."""
    import copy
    import fnmatch
    by = {s["id"]: s for s in rule_sets(org)}
    chain, cur = [], by.get(rs_id)
    if not cur:
        raise ValueError(f"unknown rule set {rs_id}")
    while cur and cur["id"] != "standard":
        chain.insert(0, cur); cur = by.get(cur.get("extends", "standard"))
    eff = copy.deepcopy(org)
    rules = eff.get("rules", [])
    base_amount = {r["id"]: r.get("amount") for r in rules}
    coa = copy.deepcopy((eff.get("settings") or {}).get("chart_of_accounts") or {})
    for s in chain:
        for ov in s.get("overrides", []):
            hits = [r for r in rules if fnmatch.fnmatchcase(r["id"].split("@")[0], ov["rule"])]
            if not hits:
                raise ValueError(f"rule set {s['id']}: override {ov['rule']} matches no company rule")
            for r in hits:
                base = r["id"].split("@")[0]
                if "scale" in ov and base_amount.get(base) is not None:
                    r["amount"] = round(base_amount[base] * ov["scale"], 2)
                r.update(ov.get("set", {}))
                r["id"] = f"{base}@{s['id']}"
                if ov.get("doc"):
                    r["doc"] = ov["doc"]
        for add in s.get("add_rules", []):
            rules.append({**add, "id": f"{add['id']}@{s['id']}"})
        if s.get("coa_overrides"):
            coa["map"] = [{**m, "rule_set": s["id"]} for m in s["coa_overrides"]] + coa.get("map", [])
    eff["rules"] = rules
    if coa:
        eff.setdefault("settings", {})["chart_of_accounts"] = coa
    return eff


def compiled_paths(ws: WS, rs_id: str = "standard") -> tuple[Path, Path]:
    cdir = ws.root / "ontology" / "rbox" / "compiled"
    sfx = "" if rs_id in (None, "standard") else f".{rs_id}"
    return cdir / f"policy{sfx}.json", cdir / f"rules.index{sfx}.json"


def compile_rules(ws: WS) -> dict:
    """org + personal R-box → one compiled policy per company rule set (standard → policy.json, gm → policy.gm.json …)
    plus rules.index[.set].json (requirement → rule id)."""
    org = load_layer(ws, "org")
    res = None
    sets = []
    for s in rule_sets(org):
        r = _compile_one(ws, apply_rule_set(org, s["id"]), s)
        sets.append({"id": s["id"], "label": s.get("label"), "min_grade": s.get("min_grade", "staff"), "policy_id": r["policy_id"]})
        res = res or r
    res["rule_sets"] = sets
    return res


def _compile_one(ws: WS, org: dict, rs: dict) -> dict:
    personal = load_layer(ws, "personal")
    st = org.get("settings", {})
    vers = layer_versions(ws)
    cats: dict[str, dict] = {c: {"label": l} for c, l in (st.get("labels") or {}).items()}
    index: dict[str, str] = {}
    active = lambda r: r.get("status", "active") == "active"
    for layer_name, rules in (("org", org.get("rules", [])), ("personal", personal.get("personal_checks", []))):
        for r in filter(active, rules):
            # 青禾修改：applies_to 容错。制度里漏填该字段会导致编译整条规则库失败 —— 见 CHANGES.md
            cat = (r.get("applies_to") or {}).get("category")
            if not cat:
                continue
            c = cats.setdefault(cat, {"label": cat})
            kind = r["kind"]
            if kind == "invoice_title":
                continue
            if layer_name == "personal" and kind == "required_evidence":
                kind = "recommended_evidence"      # personal rules never create blockers
            if kind == "required_field":
                if layer_name == "personal":
                    continue
                c.setdefault("required_fields", []).append(r["field"]); index[f"{cat}|field:{r['field']}"] = r["id"]
            elif kind in ("required_evidence", "recommended_evidence"):
                c.setdefault(kind, []).append(r["any_of"]); index[f"{cat}|{evidence_req(r['any_of'])}"] = r["id"]
            elif kind == "cap" and layer_name == "org":
                city = r["applies_to"].get("city")
                cap = c.setdefault("cap", {"amount": r["amount"], "per": r["per"], "mode": r.get("mode", "warn")})
                if city:
                    cap.setdefault("by_city", {})[city] = r["amount"]; index[f"{cat}|cap|{city}"] = r["id"]
                else:
                    cap.update({"amount": r["amount"], "per": r["per"], "mode": r.get("mode", "warn")})
                    index[f"{cat}|cap"] = r["id"]
    for c in cats.values():
        c.setdefault("required_evidence", [])
    receipt, workflow = compile_receipt(org, index)
    core_layer = load_layer(ws, "core")
    credentials = compile_credentials(core_layer, org, index)
    judgments = [j for j in core_layer.get("judgments", []) if j.get("status", "active") == "active"
                 and j["id"] not in set((st.get("judgments_disabled") or []))]
    for j in judgments:
        index[f"*|judgment:{j['id']}"] = j["id"]
    policy = {"policy_id": f"rbox:org@{vers['org']}+personal@{vers['personal']}" + ("" if rs["id"] == "standard" else f"#{rs['id']}"),
              "rule_set": {"id": rs["id"], "label": rs.get("label", rs["id"]), "min_grade": rs.get("min_grade", "staff"),
                           **({"placeholder": True} if rs.get("_placeholder") else {})},
              **({"exception_policy": st["exception_policy"]} if st.get("exception_policy") else {}),
              **({"allocation": st["allocation"]} if st.get("allocation") else {}),
              **({"grades": st["grades"]} if st.get("grades") else {}),
              **({"coa": compile_coa(st["chart_of_accounts"], index)} if st.get("chart_of_accounts") else {}),
              "report_currency": st.get("report_currency", "CNY"),
              "require_project": st.get("require_project", True), "require_purpose": st.get("require_purpose", True),
              **({"submission_window_days": st["submission_window_days"]} if st.get("submission_window_days") else {}),
              "categories": cats,
              **({"receipt": receipt} if receipt else {}), **({"workflow": workflow} if workflow else {}),
              **({"credentials": credentials} if credentials else {}),
              **({"judgments": judgments} if judgments else {}),
              **({"organization": st["organization"]} if st.get("organization") else {})}
    # 发票抬头: the employee's default comes from the personal rule (→ A-box node); the company's registered titles
    # (税务组织, org rule kind invoice_title) are all acceptable buyers. Legacy single `invoice_title` setting still honoured.
    org_titles = [dict(x) for x in (st.get("invoice_titles") or ([st["invoice_title"]] if st.get("invoice_title") else []))]
    org_rule = next((r for r in org.get("rules", []) if r.get("kind") == "invoice_title" and active(r)), None)
    title, rule_id = None, (org_rule or {}).get("id", "ORG-SET-invoice_title")
    r = next((r for r in personal.get("personal_checks", []) if r.get("kind") == "invoice_title" and active(r)), None)
    if r:
        title = invoice_title_from_abox(ws, r["title_ref"])
        if title and not org_titles:
            rule_id = r["id"]
        if title:
            title["invoice_types"] = r.get("invoice_types") or VAT_TYPES
            title["currency"] = (r.get("applies_to") or {}).get("currency", "CNY")
    if not title and org_titles:
        title = dict(org_titles[0])
    if title and org_rule:
        title["invoice_types"] = org_rule.get("invoice_types") or title.get("invoice_types") or VAT_TYPES
        title["currency"] = (org_rule.get("applies_to") or {}).get("currency", title.get("currency", "CNY"))
    if title:
        policy["invoice_title"] = {k: v for k, v in title.items() if v}
        allowed = {x["tax_id"]: {"name": x["name"], "tax_id": x["tax_id"]} for x in org_titles}
        allowed.setdefault(title["tax_id"], {"name": title["name"], "tax_id": title["tax_id"]})
        policy["invoice_titles"] = list(allowed.values())
        index["*|field:invoice.buyer"] = rule_id
    index["report|field:report.purpose"] = "ORG-SET-require_purpose"
    index["*|field:project"] = "ORG-SET-require_project"
    schema = ws.root / "tools/expense-report-package/schemas/expense-policy.schema.json"
    try:
        import jsonschema
        schema_validator(jsonschema, json.loads(schema.read_text())).validate(policy)
    except ImportError:
        pass
    pol_path, idx_path = compiled_paths(ws, rs["id"]); pol_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(policy, ensure_ascii=False, indent=2)
    pol_path.write_text(body, encoding="utf-8")
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"policy_id": policy["policy_id"], "policy_sha256": sha256_bytes(body.encode()), "rules": len(index),
            "path": str(pol_path), "versions": vers}


def compile_credentials(core: dict, org: dict, index: dict) -> dict:
    """National credential layer (core: GB/T 42965-aligned element + consistency rules, trust ranking, privacy
    exclusions) + the company's acceptance rule (org: which credentials a domestic / overseas claim may rest on)."""
    if not core.get("credential_classes"):
        return {}
    rules = [r for r in core.get("credential_rules", []) if r.get("status", "active") == "active"]
    for r in rules:
        index[f"*|credential:{r['id']}"] = r["id"]
    acc = next((r for r in org.get("rules", []) if r.get("kind") == "credential_acceptance" and r.get("status", "active") == "active"), None)
    if acc:
        index["*|credential:acceptance"] = acc["id"]
    std = {s["id"]: s["code"] for s in core.get("standards", [])}
    classes = {c: {**v, "standards": [std.get(x, x) for x in v.get("standards", [])]} for c, v in core["credential_classes"].items()}
    return {"classes": classes, "rules": rules, "trust_ranking": core.get("trust_ranking", {}),
            "privacy_excluded": core.get("privacy_excluded_elements", []), "scenarios": core.get("special_scenarios", []),
            **({"acceptance": {k: acc[k] for k in ("id", "domestic", "overseas", "text") if k in acc}} if acc else {})}


def compile_coa(coa: dict, index: dict) -> dict:
    """Chart of Accounts: category (+ optional project prefix) → 会计科目. First matching line wins; rule-set overrides
    are prepended by apply_rule_set so they win over the standard mapping."""
    lines = []
    for i, m in enumerate(coa.get("map", []), 1):
        cats = m["category"] if isinstance(m["category"], list) else [m["category"]]
        rid = f"COA-{m.get('rule_set', 'std')}-{i:02d}"
        lines.append({"id": rid, "categories": cats, "account": m["account"],
                      **({"project_prefix": m["project_prefix"]} if m.get("project_prefix") else {})})
    index["*|coa"] = "COA"
    return {"accounts": coa.get("accounts", {}), "map": lines, **({"placeholder": True} if coa.get("_placeholder") else {})}


TITLE_FIELDS = (("name", "legal_name", "名称"), ("tax_id", "tax_id", "税号"), ("address", "registered_address", "单位地址"),
                ("phone", "phone", "电话"), ("bank", "bank_name", "开户银行"), ("account", "bank_account", "银行账户"))


def invoice_title_from_abox(ws: WS, nid: str) -> dict | None:
    row = ws.db.execute("SELECT label, props FROM onto_node WHERE id=? AND class='exp:InvoiceTitle'", (nid,)).fetchone()
    if not row:
        return None
    p = json.loads(row["props"])
    return {k: p.get(prop) for k, prop, _ in TITLE_FIELDS if p.get(prop)}


def rule_for_gap(ws: WS, gap: dict, category: str | None, city: str | None = None, rule_set: str = "standard") -> str | None:
    idx_path = compiled_paths(ws, rule_set)[1]
    if not idx_path.exists():
        idx_path = compiled_paths(ws)[1]
    if not idx_path.exists():
        return None
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    req = gap.get("requirement") or ""
    if req.startswith("cap:"):
        return idx.get(f"{category}|cap|{city}") or idx.get(f"{category}|cap")
    if req == "field:report.purpose":
        return idx.get("report|field:report.purpose")
    if req == "field:project":
        return idx.get("*|field:project")
    if req.startswith("field:invoice.buyer"):
        return idx.get("*|field:invoice.buyer")
    return idx.get(f"{category}|{req}") or idx.get(f"*|{req}")


def matches(applies_to: dict, attrs: dict) -> bool:
    return all(attrs.get(k) is not None and (attrs.get(k) == v or (k == "city" and v in str(attrs.get(k))))
               for k, v in (applies_to or {}).items())


def superclasses(tbox: dict, cls: str) -> list[str]:
    chain, cur, seen = [], cls, set()
    while cur and cur not in seen:
        seen.add(cur); chain.append(cur)
        cur = (tbox.get("classes", {}).get(cur) or {}).get("subClassOf")
    return chain


def find_nodes(ws: WS, text: str, cls: str | None = None, limit: int = 10) -> list[sqlite3.Row]:
    q = norm(text)
    rows = ws.db.execute("SELECT * FROM onto_node").fetchall()
    hits = []
    for r in rows:
        names = [norm(r["label"])] + [norm(a) for a in json.loads(r["aliases"])] + [r["id"].lower()]
        if any(q == n for n in names):
            score = 2
        elif any(q and (q in n or (len(n) >= 2 and n in q)) for n in names):
            score = 1
        else:
            continue
        if cls:
            if cls not in superclasses(load_box(ws, "tbox"), r["class"]):
                continue
        hits.append((score, r))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:limit]]


# ───────────────────────── commands: workspace ─────────────────────────
def cmd_init(a):
    slug = a.slug or slugify(a.user)
    root = Path(a.root).expanduser() / slug if not a.ws else Path(a.ws).expanduser()
    if (root / "workspace.json").exists() and not a.force:
        sys.exit(f"error: workspace exists at {root} (use --force to re-seed tools/ontology; data is kept)")
    for d in ["inbox", "drafts", "outputs", "outbox/policy_feedback", "ontology/obsidian", "ontology/rbox/compiled",
              "db", "tools", "logs", "schemas"]:
        (root / d).mkdir(parents=True, exist_ok=True)

    # 1. tools: materialise expense-report-package
    src = find_pkg_skill(a.pkg_skill)
    if not src:
        sys.exit("error: expense-report-package skill not found — pass --pkg-skill /path/to/expense-report-package/SKILL.md")
    dest = root / "tools" / "expense-report-package"      # overwritten in place: never delete (sandboxed folders)
    if src.is_file():
        written = extract_appendices(src, dest)
    elif (src / "scripts" / "expense_pkg.py").exists():
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        written = ["(copied dir)"]
    else:
        written = extract_appendices(src / "SKILL.md", dest)
    if not (dest / "scripts" / "expense_pkg.py").exists():
        sys.exit(f"error: could not materialise expense_pkg.py from {src}")

    # 2. ontology seeds (never overwrite a user-edited layer unless --force)
    seeded = []
    tgt = root / "ontology" / "tbox.yaml"
    if not tgt.exists() or a.force:
        shutil.copy2(SKILL_ROOT / "ontology" / "tbox.yaml", tgt)
    for layer in ["core", "personal"]:   # --force refreshes core (skill-owned) but never the user's personal layer
        tgt = root / "ontology" / "rbox" / f"{layer}.yaml"
        if not tgt.exists() or (a.force and layer == "core"):
            shutil.copy2(SKILL_ROOT / "ontology" / "rbox" / f"{layer}.yaml", tgt); seeded.append(layer)
    org_tgt = root / "ontology" / "rbox" / "org.yaml"
    if not org_tgt.exists() or a.policy:
        # 青禾修改：改用青禾制度中枢导出的制度包（原路径指向不存在的 lib/company）—— 见 CHANGES.md
        company = PROJECT_ROOT / "company" / "policy.json"
        pol_path = Path(a.policy).expanduser() if a.policy else company if company.exists() else \
            dest / "config" / "policy.default.json"
        org_tgt.write_text(dump_yaml(policy_to_org(json.loads(pol_path.read_text(encoding="utf-8")), f"import:{pol_path.name}")),
                           encoding="utf-8"); seeded.append("org")
    for f in ["package-manifest.schema.json", "rbox-layer.schema.json"]:
        shutil.copy2(SKILL_ROOT / "schemas" / f, root / "schemas" / f)

    cfg_path = root / "workspace.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {"created_at": now()}
    cfg.update({"workspace_version": "1.0", "user": {**(cfg.get("user") or {}),   # re-init keeps existing profile fields
                "name": a.user, "slug": slug,
                **{k: v for k, v in {"employee_id": a.employee_id, "department": a.department,
                                     "email": a.email, "organization": a.org}.items() if v}},
                "side": "employee", "policy": "ontology/rbox/compiled/policy.json", "pkg_source": str(src)})
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    ws = WS(root)
    content = (root / "ontology" / "tbox.yaml").read_text(encoding="utf-8")
    ws.db.execute("INSERT OR REPLACE INTO onto_schema VALUES(?,?,?,?)",
                  ("tbox", str(yaml.safe_load(content).get("version")), content, now()))
    for layer in RBOX_LAYERS:   # record every layer that has no history yet (fresh DB, or a recovered workspace)
        has = ws.db.execute("SELECT 1 FROM rbox_versions WHERE layer=?", (layer,)).fetchone()
        if not has or (layer in ("org", "core") and layer in seeded):
            last = ws.db.execute("SELECT content FROM rbox_versions WHERE layer=? ORDER BY version DESC LIMIT 1", (layer,)).fetchone()
            if not has or last[0] != layer_path(ws, layer).read_text(encoding="utf-8"):
                snapshot_layer(ws, layer, ("org-import" if layer == "org" else "plugin-upgrade") if has else "init",
                               "seed" if not has else f"{layer} refreshed at init")
    compiled = compile_rules(ws)
    node_upsert(ws, "person:self", "exp:Employee", a.user,
                {"employee_id": a.employee_id, "department": a.department, "email": a.email})
    if a.org:
        node_upsert(ws, f"org:{norm(a.org)}", "exp:Organization", a.org)
        edge_add(ws, "person:self", "exp:memberOf", f"org:{norm(a.org)}", "stated", "user:init")
    ws.db.commit()
    company = seed_company(ws)
    seed_standards(ws)
    if not a.ws:
        (Path(a.root).expanduser() / ".current").write_text(slug)
    out({"workspace": str(root), "user": a.user, "tools": written, "db": str(ws.db_path),
         "rbox": {"seeded": seeded, "compiled": compiled}, **({"company": company} if company else {})})


def sync_report(ws: WS, rid: str) -> dict | None:
    p = ws.draft(rid) / "report.json"
    if not p.exists():
        return None
    r = json.loads(p.read_text(encoding="utf-8"))
    h = r["report"]; t = h.get("totals") or {}
    blockers = sum(1 for g in r.get("gaps", []) if g["severity"] == "blocker")
    prev = ws.db.execute("SELECT latest_version, created_at FROM reports WHERE report_id=?", (rid,)).fetchone()
    ws.db.execute("""INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (rid, h["title"], h["status"], h["employee"].get("name"), (h.get("default_project") or {}).get("code"),
                   h.get("purpose"), (h.get("period") or {}).get("start"), (h.get("period") or {}).get("end"),
                   h["currency"], t.get("grand_total", 0), len(r["items"]), blockers, str(ws.draft(rid)),
                   prev["latest_version"] if prev else 0, prev["created_at"] if prev else h["created_at"], now()))
    ws.db.commit()
    return r


def cmd_status(a):
    ws = resolve_ws(a.ws); cfg = ws.cfg()
    for rid in [p.name for p in (ws.root / "drafts").iterdir() if p.is_dir() and not p.name.startswith("_")]:
        sync_report(ws, rid)
    reports = [dict(r) for r in ws.db.execute(
        "SELECT report_id,title,status,project_code,grand_total,currency,item_count,open_blockers,latest_version,updated_at "
        "FROM reports ORDER BY updated_at DESC").fetchall()]
    new_inputs = ws.db.execute("SELECT COUNT(*) FROM inputs WHERE status='new'").fetchone()[0]
    untracked = [str(p.relative_to(ws.root)) for p in (ws.root / "inbox").rglob("*")
                 if p.is_file() and p.suffix.lower() in EVIDENCE_EXTS
                 and not ws.db.execute("SELECT 1 FROM inputs WHERE sha256=?", (sha256_file(p),)).fetchone()]
    mem = {"nodes": ws.db.execute("SELECT COUNT(*) FROM onto_node").fetchone()[0],
           "edges": ws.db.execute("SELECT COUNT(*) FROM onto_edge").fetchone()[0],
           "reports_learned": ws.db.execute("SELECT COUNT(DISTINCT source) FROM onto_edge WHERE source LIKE 'report:%'").fetchone()[0]}
    out({"workspace": str(ws.root), "user": cfg["user"], "session": ws.meta("current_session"),
         "reports": reports, "inbox": {"new_registered": new_inputs, "untracked_files": untracked},
         "memory": mem, "rbox_versions": layer_versions(ws), "pending_evolution": pending_deltas(ws),
         "self_defaults": recall_node(ws, "person:self", top=3)})


def project_registry(ws: "WS") -> dict:
    """A-box → {project code: {name, charge_code, budget_id, budget_name, cost_center}} (+ __budgets__ status), for the
    report builder to fill allocation fields from memory. Stated facts win over observed ones."""
    reg: dict = {}
    for row in ws.db.execute("SELECT id,label,props FROM onto_node WHERE class='exp:Project'"):
        pr = json.loads(row["props"] or "{}")
        code = pr.get("code") or row["id"].split(":", 1)[1]
        e = {"name": pr.get("name") or (row["label"] if row["label"] != code else None), "cost_center": pr.get("cost_center")}
        for pred, key, pfx in (("exp:hasChargeCode", "charge_code", "charge:"), ("exp:drawsOnBudget", "budget_id", "budget:")):
            hit = ws.db.execute(f"SELECT obj FROM onto_edge WHERE subj=? AND pred=? ORDER BY CASE basis WHEN 'stated' THEN 0 "
                                f"ELSE 1 END, last_seen DESC LIMIT 1", (row["id"], pred)).fetchone()
            if hit:
                e[key] = hit[0][len(pfx):] if hit[0].startswith(pfx) else hit[0]
        reg[code] = {k: v for k, v in e.items() if v}
    buds = {}
    for row in ws.db.execute("SELECT id,label,props FROM onto_node WHERE class='exp:Budget'"):
        pr = json.loads(row["props"] or "{}")
        buds[row["id"].split(":", 1)[1]] = {k: v for k, v in {"name": row["label"], "status": pr.get("status"),
                                                               "fiscal_year": pr.get("fiscal_year")}.items() if v}
    for code, e in reg.items():
        if e.get("budget_id") in buds and buds[e["budget_id"]].get("name") not in (None, e["budget_id"]):
            e.setdefault("budget_name", buds[e["budget_id"]]["name"])
    if buds:
        reg["__budgets__"] = buds
    return reg


def register_project(ws: WS, pr: dict, basis: str = "stated", source: str | None = None, default: bool = False) -> str:
    """exp:Project node (+ exp:ChargeCode, exp:Budget) and their edges; optional employee default (exp:defaultProject)."""
    src = source or f"user:{ws.meta('current_session') or dt.date.today()}"
    pid = f"project:{pr['code']}"
    node_upsert(ws, pid, "exp:Project", pr.get("name") or pr["code"],
                {"code": pr["code"], "name": pr.get("name"), "cost_center": pr.get("cost_center")},
                aliases=[pr["code"]] + ([pr["name"]] if pr.get("name") else []))
    if pr.get("charge_code"):
        cid = f"charge:{pr['charge_code']}"
        node_upsert(ws, cid, "exp:ChargeCode", pr["charge_code"], {"charge_code": pr["charge_code"]}, aliases=[pr["charge_code"]])
        if basis == "stated":
            ws.db.execute("DELETE FROM onto_edge WHERE subj=? AND pred='exp:hasChargeCode' AND basis='stated' AND obj!=?", (pid, cid))
        edge_add(ws, pid, "exp:hasChargeCode", cid, basis, src)
    if pr.get("budget_id"):
        bid = f"budget:{pr['budget_id']}"
        node_upsert(ws, bid, "exp:Budget", pr.get("budget_name") or pr["budget_id"],
                    {"budget_id": pr["budget_id"], "status": pr.get("budget_status"), "fiscal_year": pr.get("fiscal_year")},
                    aliases=[pr["budget_id"]])
        if basis == "stated":
            ws.db.execute("DELETE FROM onto_edge WHERE subj=? AND pred='exp:drawsOnBudget' AND basis='stated' AND obj!=?", (pid, bid))
        edge_add(ws, pid, "exp:drawsOnBudget", bid, basis, src)
        if pr.get("charge_code"):
            edge_add(ws, f"charge:{pr['charge_code']}", "exp:drawsOnBudget", bid, basis, src)
    if default:
        ws.db.execute("DELETE FROM onto_edge WHERE subj='person:self' AND pred='exp:defaultProject'")
        edge_add(ws, "person:self", "exp:defaultProject", pid, "stated", src)
    edge_add(ws, "person:self", "exp:worksOn", pid, basis, src)
    ws.db.commit()
    return pid


def default_project(ws: WS) -> dict | None:
    e = ws.db.execute("SELECT obj FROM onto_edge WHERE subj='person:self' AND pred='exp:defaultProject' LIMIT 1").fetchone()
    if not e:
        return None
    code = e[0].split(":", 1)[1]
    return {"code": code, **{k: v for k, v in project_registry(ws).get(code, {}).items() if k in ALLOC_KEYS}}


ALLOC_KEYS = ("name", "charge_code", "budget_id", "budget_name", "cost_center")


def cmd_project(a):
    """Project / charge-code / budget registry in the A-box (the employee's cost-allocation memory)."""
    ws = resolve_ws(a.ws)
    if a.action == "set":
        pr = json.loads(a.json)
        if not pr.get("code"):
            sys.exit("error: project needs a code")
        bad = set(pr) - {"code", "default", "budget_status", "fiscal_year", *ALLOC_KEYS}
        if bad:
            sys.exit(f"error: unknown keys {sorted(bad)}")
        pid = register_project(ws, pr, default=bool(pr.get("default")))
        synced = sync_policy(ws, open_reports(ws))          # re-resolve open reports with the new registry
        ws.log("project_set", {"project": pr["code"], "default": bool(pr.get("default"))})
        out({"project": pid, "registry": project_registry(ws).get(pr["code"]), "default": bool(pr.get("default")),
             "rechecked_reports": synced})
    elif a.action == "list":
        reg = project_registry(ws); dflt = (default_project(ws) or {}).get("code")
        out([{"code": c, **v, **({"default": True} if c == dflt else {})} for c, v in reg.items() if c != "__budgets__"])
    elif a.action == "show":
        out({"code": a.code, **project_registry(ws).get(a.code, {}),
             "reports": [dict(r) for r in ws.db.execute("SELECT report_id,title,status,grand_total FROM reports WHERE project_code=?",
                                                        (a.code,))]})


def seed_standards(ws: WS) -> int:
    """Core layer → A-box individuals: the data standards credentials conform to and the GB/T 42965.2 scenarios."""
    core, n = load_layer(ws, "core"), 0
    for s in core.get("standards", []):
        node_upsert(ws, f"std:{s['code']}", "exp:Standard", s.get("title") or s["code"],
                    {"code": s["code"], "issuer": s.get("issuer"), "effective": s.get("effective")}, aliases=[s["code"]]); n += 1
    for sc in core.get("special_scenarios", []):
        node_upsert(ws, f"scenario:{sc}", "exp:SpecialBusinessScenario", sc); n += 1
    ws.db.commit()
    return n


def seed_company(ws: WS) -> dict | None:
    """Company pack → A-box: the organisation node and every registered 发票抬头 / 税务组织 it bills under, and the
    employee's membership. Facts come from the org layer (source company:<policy>) so every employee who installs the
    plugin starts with them; the employee's own stated default title is never replaced."""
    st = load_layer(ws, "org").get("settings", {})
    org, titles = st.get("organization"), st.get("invoice_titles") or ([st["invoice_title"]] if st.get("invoice_title") else [])
    if not org and not titles:
        return None
    src = f"company:{load_layer(ws, 'org').get('version')}"
    org_id = (org or {}).get("id") or f"org:{norm((org or {}).get('name') or titles[0]['name'])}"
    if org:
        node_upsert(ws, org_id, "exp:Organization", org.get("name"),
                    {"legal_name": org.get("legal_name"), "bip_tenant": org.get("bip_tenant"), "systems": org.get("systems")},
                    aliases=[*(org.get("aliases") or []), *([org["legal_name"]] if org.get("legal_name") else [])])
        edge_add(ws, "person:self", "exp:memberOf", org_id, "stated", src)
    tids = []
    for t_ in titles:
        tid = f"invoice_title:{t_['tax_id']}"; tids.append(tid)
        node_upsert(ws, tid, "exp:InvoiceTitle", t_["name"], {prop: t_.get(k) for k, prop, _ in TITLE_FIELDS if t_.get(k)},
                    aliases=[t_["tax_id"]])
        edge_add(ws, org_id, "exp:hasInvoiceTitle", tid, "stated", src)
    has_default = ws.db.execute("SELECT 1 FROM onto_edge WHERE subj='person:self' AND pred='exp:billsTo'").fetchone()
    if tids and not has_default:
        edge_add(ws, "person:self", "exp:billsTo", tids[0], "stated", src)
    ws.db.commit()
    cfg = ws.cfg()
    if org and not (cfg.get("user") or {}).get("organization"):
        cfg.setdefault("user", {})["organization"] = org.get("name")
        (ws.root / "workspace.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"organization": org_id, "invoice_titles": tids}


def set_invoice_title(ws: WS, cfg: dict, title: dict) -> dict:
    """A-box: exp:InvoiceTitle node (stated) + org hasInvoiceTitle + self billsTo; R-box: personal rule PC-INVOICE-TITLE."""
    old = {k: v for k, v in title.items()}
    tid = f"invoice_title:{title['tax_id']}"
    props = {prop: title.get(k) for k, prop, _ in TITLE_FIELDS if title.get(k)}
    node_upsert(ws, tid, "exp:InvoiceTitle", title["name"], props, aliases=[title["tax_id"]])
    org_name = (cfg.get("user") or {}).get("organization")
    org_id = f"org:{norm(org_name)}" if org_name else f"org:{norm(title['name'])}"
    node_upsert(ws, org_id, "exp:Organization", org_name or title["name"], {"legal_name": title["name"]},
                aliases=[title["name"]])
    src = f"user:{ws.meta('current_session') or dt.date.today()}"
    ws.db.execute("DELETE FROM onto_edge WHERE pred IN ('exp:hasInvoiceTitle','exp:billsTo') AND basis='stated' AND obj!=?", (tid,))
    edge_add(ws, org_id, "exp:hasInvoiceTitle", tid, "stated", src)
    edge_add(ws, "person:self", "exp:billsTo", tid, "stated", src)
    if org_name:
        edge_add(ws, "person:self", "exp:memberOf", org_id, "stated", src)
    ws.db.commit()
    per = load_layer(ws, "personal")
    rule = {"id": "PC-INVOICE-TITLE", "kind": "invoice_title", "applies_to": {"currency": "CNY"}, "title_ref": tid,
            "invoice_types": ["vat_special", "vat_general", "e_invoice", "rail_e_ticket"], "severity": "blocker",
            "doc": "仅适用于中国发票（增值税专/普票、全电发票、铁路电子客票，人民币金额）：购买方名称与税号必须与发票抬头一致；境外发票不校验",
            "origin": {"by": "user", "at": now()}}
    existing = next((x for x in per.get("personal_checks", []) if x.get("id") == rule["id"]), None)
    op = {"op": "update", "section": "personal_checks", "id": rule["id"], "set": rule} if existing else \
         {"op": "add", "section": "personal_checks", "value": rule}
    apply_patch(per, [op])
    v = save_layer(ws, "personal", per, "user", f"发票抬头 → {title['name']} ({title['tax_id']})")
    return {"abox": {"invoice_title": tid, "organization": org_id}, "rbox": {"layer": "personal", "version": v,
                                                                             "rule": rule["id"]}}


def mask_account(n: str) -> str:
    n = re.sub(r"\D", "", n or "")
    return f"{n[:4]}****{n[-4:]}" if len(n) >= 8 else "****"


def luhn_ok(n: str) -> bool:
    s = 0
    for i, d in enumerate(reversed(n)):
        d = int(d) * (2 if i % 2 else 1)
        s += d - 9 if d > 9 else d
    return s % 10 == 0


def set_payee(ws: WS, cfg: dict, p: dict) -> dict:
    """A-box: exp:PaymentAccount (stated) + person:self —reimbursedTo→ it (replacing the previous default).
    The personal R-box recall rule SUG-SELF-PAYEE fills report.payee from this edge."""
    acct = re.sub(r"\s", "", p.get("account", ""))
    if not re.fullmatch(r"[0-9]{12,19}", acct) or not luhn_ok(acct):
        sys.exit("error: payee.account must be the full 12–19 digit card number (checksum failed)")
    if p.get("cnaps") and not re.fullmatch(r"[0-9]{12}", p["cnaps"]):
        sys.exit("error: payee.cnaps (联行号) must be 12 digits")
    aid = f"account:{norm(p.get('bank', 'bank'))}-{acct[-4:]}"
    props = {"bank_name": p.get("bank"), "branch": p.get("branch"), "branch_address": p.get("branch_address"),
             "cnaps": p.get("cnaps"), "card_type": p.get("card_type"), "holder": p.get("holder") or (cfg.get("user") or {}).get("name"),
             "account_number": acct, "account_masked": mask_account(acct)}
    node_upsert(ws, aid, "exp:PaymentAccount", p.get("alias") or f"{p.get('bank', '')}卡 {mask_account(acct)}", props,
                aliases=[x for x in (p.get("alias"), mask_account(acct)) if x])
    ws.db.execute("DELETE FROM onto_edge WHERE subj='person:self' AND pred='exp:reimbursedTo' AND obj!=?", (aid,))
    edge_add(ws, "person:self", "exp:reimbursedTo", aid, "stated", f"user:{ws.meta('current_session') or dt.date.today()}")
    ws.db.commit()
    per = load_layer(ws, "personal")
    if not any(r.get("id") == "SUG-SELF-PAYEE" for r in per.get("recall", [])):   # workspaces created before 0.1.7
        apply_patch(per, [{"op": "add", "section": "recall", "value": {
            "id": "SUG-SELF-PAYEE", "key_class": "exp:Employee", "path": ["exp:reimbursedTo"], "fills": "report.payee",
            "mode": "auto", "auto_when": {"min_share": 1.0, "min_sources": 1}}}])
        save_layer(ws, "personal", per, "user", "默认报销收款账户 → recall rule SUG-SELF-PAYEE")
    return {"abox": {"payment_account": aid, "edge": "person:self exp:reimbursedTo"},
            "rbox": {"recall_rule": "SUG-SELF-PAYEE"}, "masked": mask_account(acct)}


def default_payee(ws: WS, cfg: dict) -> dict | None:
    """Resolve report.payee through the personal R-box recall rule (auto when the stated default is unambiguous)."""
    sug = recall_node(ws, "person:self", top=1).get("SUG-SELF-PAYEE")
    if not sug or sug["candidates"][0]["apply"] != "auto":
        return None
    row = ws.db.execute("SELECT label, props FROM onto_node WHERE id=?", (sug["candidates"][0]["id"],)).fetchone()
    pr = json.loads(row["props"])
    return {k: v for k, v in {"name": pr.get("holder") or (cfg.get("user") or {}).get("name"), "alias": row["label"],
                              "bank": pr.get("bank_name"), "branch": pr.get("branch"), "branch_address": pr.get("branch_address"),
                              "cnaps": pr.get("cnaps"), "account": pr.get("account_number"),
                              "account_masked": pr.get("account_masked")}.items() if v}


PROFILE_KEYS = {"user": {"name", "name_zh", "employee_id", "department", "email", "organization", "grade", "title"},
                "invoice_title": {"name", "tax_id", "address", "phone", "bank", "account"},
                "payee": {"alias", "bank", "branch", "branch_address", "cnaps", "account", "holder", "card_type"}}


def cmd_profile(a):
    """Employee profile and 发票抬头 (workspace.json). Changing the title re-compiles rules and re-checks open reports."""
    ws = resolve_ws(a.ws); cfg = ws.cfg()
    if a.action == "set":
        patch = json.loads(a.json)
        for sec, vals in patch.items():
            if sec not in PROFILE_KEYS or not isinstance(vals, dict) or set(vals) - PROFILE_KEYS[sec]:
                sys.exit(f"error: allowed keys: {json.dumps({k: sorted(v) for k, v in PROFILE_KEYS.items()})}")
            cfg.setdefault(sec, {}).update({k: v for k, v in vals.items() if v is not None})
        payee = cfg.pop("payee", None)                          # lives in the ontology, not in workspace.json
        payee_note = set_payee(ws, cfg, payee) if payee else None
        title = cfg.pop("invoice_title", None)                  # lives in the ontology, not in workspace.json
        rbox_note = None
        if title:
            if not re.fullmatch(r"[0-9A-Z]{15,20}", title.get("tax_id", "")) or not title.get("name"):
                sys.exit("error: invoice_title needs name and tax_id (15–20 character 纳税人识别号)")
            rbox_note = set_invoice_title(ws, cfg, title)
        (ws.root / "workspace.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if "user" in patch:
            u = patch["user"]
            node_upsert(ws, "person:self", "exp:Employee", u.get("name"),
                        {k: u.get(k) for k in ("employee_id", "department", "email", "name_zh", "grade", "title")},
                        aliases=[x for x in (u.get("name_zh"), u.get("name")) if x]); ws.db.commit()
        if payee_note:
            pa = default_payee(ws, cfg)
            for rid in open_reports(ws):
                if pa and ws.draft(rid).exists():
                    ws.run_pkg("set-header", ["--json", json.dumps({"payee": pa}, ensure_ascii=False)], rid)
        c = compile_rules(ws); synced = sync_policy(ws, open_reports(ws))
        ws.log("profile_set", {"sections": sorted(patch)})
        out({"updated": sorted(patch), "compiled": c["policy_id"], "rechecked_reports": synced,
             "ontology": rbox_note or payee_note})
    else:
        r = next((r for r in load_layer(ws, "personal").get("personal_checks", []) if r.get("kind") == "invoice_title"), None)
        t = invoice_title_from_abox(ws, r["title_ref"]) if r else None
        if not t:                                   # no personal default → the company's registered title
            e = ws.db.execute("SELECT obj FROM onto_edge WHERE subj='person:self' AND pred='exp:billsTo' LIMIT 1").fetchone()
            t = invoice_title_from_abox(ws, e[0]) if e else None
        pa = default_payee(ws, cfg)
        out({"user": cfg.get("user"), "invoice_title": t,
             "payee": {k: v for k, v in (pa or {}).items() if k != "account"} or None,
             "payee_text": "\n".join(f"{l}：{pa[k]}" for k, l in (("name", "户名"), ("bank", "开户银行"), ("branch", "开户支行"),
                                     ("account_masked", "卡号"), ("cnaps", "联行号"), ("branch_address", "网点地址")) if pa.get(k))
             if pa else None,
             "invoice_title_text": "\n".join(f"{l}：{t[k]}" for k, l in (("name", "名称"), ("tax_id", "税号"), ("address", "单位地址"),
                                            ("phone", "电话"), ("bank", "开户银行"), ("account", "银行账户")) if t.get(k))
             if t else None})


def cmd_ingest(a):
    """One command per voucher. 数电票 XML: attach → parse → complete item (Jev category) → judgments.
    Other files: attach (and link to --item) so the agent reads them."""
    ws = resolve_ws(a.ws); rid = a.report_id
    if not ws.draft(rid).exists():
        sys.exit(f"error: unknown report {rid}")
    import time as _time
    t0 = _time.perf_counter()
    res = {"steps": []}
    for f in a.file:
        fp = Path(f).expanduser()
        kind = a.kind or ("e_invoice" if fp.suffix.lower() in (".xml", ".ofd") else None)
        if not kind:
            sys.exit("error: --kind required for non-XML files")
        args = ["attach", "--file", str(fp), "--kind", kind] + (["--item", a.item] if a.item else [])
        if fp.suffix.lower() in (".xml", ".ofd"):
            args += ["--source-form", "original_electronic"]
        code, so, se = ws.run_pkg(args[0], args[1:], rid)
        if code:
            sys.exit(se or so)
        att = json.loads(so)
        h = sha256_file(fp)
        ws.db.execute("""INSERT INTO inputs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET
                         status='ingested', report_id=excluded.report_id, evidence_id=excluded.evidence_id""",
                      (h, str(fp), fp.suffix.lower(), fp.stat().st_size, now(), "ingested", rid, att["evidence_id"], None))
        ws.db.commit()
        step = {"file": fp.name, "evidence_id": att["evidence_id"], "duplicate": att.get("duplicate", False)}
        if fp.suffix.lower() in (".xml", ".ofd") and not att.get("duplicate"):
            code, so, se = ws.run_pkg("credential", ["--evidence", att["evidence_id"], "--from-source"], rid)
            if code:
                step["error"] = (se or so).strip()
            else:
                step["credential"] = json.loads(so).get("parsed")
                if not a.item:
                    code, so, se = ws.run_pkg("ingest-xml", ["--evidence", att["evidence_id"]], rid)
                    step.update(json.loads(so) if code == 0 else {"error": (se or so).strip()})
        res["steps"].append(step)
    if not a.no_judge and ws.jev_env().get("TYPESAFE_API_KEY"):
        code, so, _ = ws.run_pkg("judge", [], rid)
        if code == 0:
            j = json.loads(so)
            res["judgments"] = {k: j.get(k) for k in ("calls", "wall_ms", "model")}
    ws.run_pkg("gaps", [], rid)
    r = sync_report(ws, rid)
    res.update(status=r["report"]["status"], total=r["report"]["totals"]["grand_total"],
               blockers=[g["message"] for g in r["gaps"] if g["severity"] == "blocker"],
               ms=round((_time.perf_counter() - t0) * 1000))
    ws.log("ingest", {"report_id": rid, "files": [s_["file"] for s_ in res["steps"]]})
    out(res)


def cmd_secret(a):
    """Workspace secret store. Keys never go into workspace.json, reports, packages, logs or exports."""
    ws = resolve_ws(a.ws)
    name = a.name
    p = ws.secret_path(name)
    if a.action == "set":
        val = sys.stdin.readline().strip() if not a.value else a.value
        if not val:
            sys.exit("error: pipe the key on stdin, e.g. `pbpaste | … prep secret set typesafe`")
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
        p.write_text(val + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        gi = ws.root / ".gitignore"
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        if ".secrets/" not in lines:
            gi.write_text("\n".join(lines + [".secrets/"]) + "\n", encoding="utf-8")
        ws.log("secret_set", {"name": name})                      # the value is never logged
        out({"name": name, "stored": str(p), "masked": mask_secret(val)})
    elif a.action == "status":
        val = ws.secret(name)
        src = "env" if name == "typesafe" and os.environ.get("TYPESAFE_API_KEY") else ("file" if val else None)
        try:
            import typesafe_sdk
            sdk = typesafe_sdk.__version__
        except ImportError:
            sdk = None
        out({"name": name, "configured": bool(val), "source": src, "masked": mask_secret(val) if val else None,
             "sdk": sdk, "jev": ws.cfg().get("jev") or {"model": "jev-latest (default)"}})
    elif a.action == "remove":
        if p.exists():
            p.write_text("", encoding="utf-8")                    # emptied in place (sandboxes may forbid deletes)
        ws.log("secret_removed", {"name": name})
        out({"name": name, "removed": True})
    elif a.action == "test":
        env = ws.jev_env()
        if not env.get("TYPESAFE_API_KEY"):
            sys.exit("error: no TypeSafe key configured")
        code = ("import os,json,time;from typesafe_sdk import TypeSafeClient,Noul;t=time.perf_counter();"
                "c=TypeSafeClient();r=c.system_one(state={'invoice':'*住宿服务*房费'},questions={'ok':Noul(instructions='`invoice` is a hotel charge.')});"
                "print(json.dumps({'model':r.model,'noul':r.nouls['ok'].noul,'ms':round((time.perf_counter()-t)*1000)}))")
        pr = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        if pr.returncode:
            sys.exit(f"error: TypeSafe test failed: {(pr.stderr or pr.stdout).strip().splitlines()[-1] if (pr.stderr or pr.stdout).strip() else pr.returncode}")
        out({"ok": True, **json.loads(pr.stdout)})


def mask_secret(v: str | None) -> str | None:
    return None if not v else (v[:4] + "…" + v[-4:] if len(v) > 12 else "****")


BIP_NORM = {"未报账": "unreported", "部分已报账": "partially_reported", "已报账": "reported", "已保存": "saved",
            "保存成功": "saved", "保存失败": "failed", "校验失败": "failed", "未采集": "not_collected",
            "明细完整": "ok", "明细为空": "empty"}


def cmd_bip(a):
    """Record what happened to a report's invoices in the company's e-invoice / reimbursement systems (as the employee reports it), then re-check.
    The agent never simulates 查验: it records the backend's result and applies the company rules to it."""
    ws = resolve_ws(a.ws)
    if not ws.draft(a.report_id).exists():
        sys.exit(f"error: unknown report {a.report_id}")
    rep = json.loads((ws.draft(a.report_id) / "report.json").read_text(encoding="utf-8"))
    pol = json.loads((ws.draft(a.report_id) / "policy.json").read_text(encoding="utf-8"))
    vat = ((pol.get("receipt") or {}).get("verification") or {}).get("invoice_types") or VAT_TYPES
    sel = a.items or "all"
    pick = {"all": lambda i: (i.get("invoice") or {}).get("invoice_type") not in (None, "none", "foreign_invoice"),
            "vat": lambda i: (i.get("invoice") or {}).get("invoice_type") in vat}
    items = [i for i in rep["items"] if i.get("status") != "excluded" and
             (pick[sel](i) if sel in pick else i["item_id"] in sel.split(","))]
    if not items:
        sys.exit("error: no matching items")
    n = lambda v: BIP_NORM.get(v, v) if v else v
    patch = {k: v for k, v in {"save_status": n(a.save_status), "verify_result": a.verify, "reimb_status": n(a.reimb_status),
                               "oa_detail": n(a.oa_detail), "failure_reason": a.reason, "transfer_to": a.transfer_to}.items() if v}
    if a.warning:
        patch["warnings"] = a.warning
    if a.docs:
        patch["associated_docs"] = a.docs
    if not patch:
        sys.exit("error: nothing to record")
    if a.transfer_to:                       # org transfer rule: only 未报账 invoices may be transferred to another person
        rule = next((s for s in (pol.get("receipt") or {}).get("status_rules", []) if s.get("action") == "transfer"), None)
        allowed = (rule or {}).get("allowed", ["unreported"])
        bad = [i["item_id"] for i in items
               if (patch.get("reimb_status") or ((i.get("invoice") or {}).get("bip") or {}).get("reimb_status")) not in allowed]
        if bad:
            sys.exit(f"error: {', '.join(bad)} cannot be transferred — only invoices whose 报账状态 is "
                     f"{'/'.join(BIP_STATUS_ZH.get(x, x) for x in allowed)} may be transferred "
                     f"({(rule or {}).get('id', 'transfer rule')}; record --reimb-status first if the e-invoice system shows 未报账)")
    patch["updated_at"] = now()
    code, so, se = ws.run_pkg("add-source", ["--type", "utterance", "--text", a.note or f"BIP/OA 状态：{json.dumps(patch, ensure_ascii=False)}"],
                              a.report_id)
    if code:
        sys.exit(se or so)
    src = json.loads(so)["source_id"]
    for i in items:
        code, so, se = ws.run_pkg("upsert", ["--item", i["item_id"], "--source", src, "--method", "user_stated",
                                             "--json", json.dumps({"invoice": {"bip": patch}}, ensure_ascii=False)], a.report_id)
        if code:
            sys.exit(se or so)
        if a.transfer_to:
            ws.run_pkg("upsert", ["--item", i["item_id"], "--source", src, "--method", "user_stated", "--json",
                                  json.dumps({"status": "excluded", "notes": f"发票已在 BIP 转移给 {a.transfer_to}"}, ensure_ascii=False)],
                       a.report_id)
    ws.run_pkg("gaps", [], a.report_id)
    r = sync_report(ws, a.report_id)
    ws.log("bip_status", {"report_id": a.report_id, "items": [i["item_id"] for i in items], "patch": patch})
    ids = {i["item_id"] for i in items}
    out({"report_id": a.report_id, "recorded": patch, "items": sorted(ids), "status": r["report"]["status"],
         "gaps": [g for g in r.get("gaps", []) if g.get("item_id") in ids and g["type"] in
                  ("invoice_status", "invoice_detail", "invoice_verification", "duplicate_suspect", "source_file")],
         "workflow": [{"step": s["step"], "text": s["text"], "done": s.get("done")} for s in r.get("workflow", [])]})


BIP_STATUS_ZH = {v: k for k, v in BIP_NORM.items() if v in ("unreported", "partially_reported", "reported")}


def pending_deltas(ws: WS) -> int:
    try:
        return ws.db.execute("SELECT COUNT(*) FROM evolution_deltas WHERE status='proposed'").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def gap_snapshot(ws: WS, report: dict) -> list[dict]:
    """Compact, rule-attributed view of a report's gaps — the telemetry the evolver reads."""
    items = {i["item_id"]: i for i in report.get("items", [])}
    rs = ((report.get("report") or {}).get("rule_set") or {}).get("id", "standard")
    snap = []
    for g in report.get("gaps", []):
        it = items.get(g.get("item_id"), {})
        cat, city = it.get("category"), (it.get("trip") or {}).get("city")
        snap.append({"type": g["type"], "severity": g["severity"], "requirement": g.get("requirement"),
                     "item_id": g.get("item_id"), "category": cat, "city": city,
                     "rule_id": rule_for_gap(ws, g, cat, city, rs),
                     "attendees": len(it.get("attendees") or []) if it else None,
                     "currency": (it.get("amount") or {}).get("currency") if it else None})
    return snap


# ───────────────────────── sessions ─────────────────────────
def cmd_session(a):
    ws = resolve_ws(a.ws)
    if a.action == "start":
        cur = ws.meta("current_session")
        if cur and not a.force:
            out({"session_id": cur, "resumed": True}); return
        sid = f"SES-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"   # unique even within one second
        ws.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (sid, now(), None, a.goal, None))
        ws.meta("current_session", sid); ws.log("session_start", {"goal": a.goal})
        out({"session_id": sid})
    elif a.action == "log":
        ws.log(a.kind, {"text": a.text} if a.text else json.loads(a.json or "{}")); out({"ok": True})
    elif a.action == "end":
        sid = ws.meta("current_session")
        if not sid:
            out({"ok": True, "note": "no active session"}); return
        ws.log("session_end", {"summary": a.summary})
        ws.db.execute("UPDATE sessions SET ended_at=?, summary=? WHERE session_id=?", (now(), a.summary, sid))
        ws.meta("current_session", delete=True)
        ev = [dict(r) for r in ws.db.execute("SELECT kind, COUNT(*) n FROM session_events WHERE session_id=? GROUP BY kind", (sid,))]
        out({"session_id": sid, "ended": True, "events": ev})
    elif a.action == "list":
        out([dict(r) for r in ws.db.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (a.limit,))])


# ───────────────────────── inbox ─────────────────────────
def cmd_inbox(a):
    ws = resolve_ws(a.ws)
    if a.action == "scan":
        roots = [ws.root / "inbox"] + [Path(f).expanduser() for f in (a.folder or [])]
        pending, dups, known, seen = [], [], 0, {}
        for root in roots:
            for p in sorted(root.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in EVIDENCE_EXTS:
                    continue
                h = sha256_file(p)
                if h in seen:  # byte-identical copy inside this scan
                    dups.append({"path": str(p), "same_as": seen[h]}); continue
                seen[h] = str(p)
                row = ws.db.execute("SELECT status, path FROM inputs WHERE sha256=?", (h,)).fetchone()
                if row:
                    if row["status"] == "new":
                        pending.append({"sha256": h, "path": row["path"]})
                    else:
                        known += 1
                        if row["path"] != str(p):
                            dups.append({"path": str(p), "same_as": row["path"], "status": row["status"]})
                    continue
                ws.db.execute("INSERT INTO inputs VALUES(?,?,?,?,?,?,?,?,?)",
                              (h, str(p), p.suffix.lower(), p.stat().st_size, now(), "new", None, None, None))
                pending.append({"sha256": h, "path": str(p)})
        ws.db.commit(); ws.log("inbox_scan", {"pending": len(pending), "duplicates": len(dups)})
        out({"pending": pending, "duplicates": dups, "already_processed": known})
    elif a.action == "mark":
        ws.db.execute("UPDATE inputs SET status=?, note=? WHERE sha256 LIKE ?", (a.status, a.note, a.sha + "%"))
        ws.db.commit(); out({"ok": True})
    elif a.action == "list":
        q = "SELECT * FROM inputs" + (" WHERE status=?" if a.status else "") + " ORDER BY first_seen"
        out([dict(r) for r in ws.db.execute(q, (a.status,) if a.status else ())])


# ───────────────────────── reports ─────────────────────────
def cmd_report(a):
    ws = resolve_ws(a.ws); cfg = ws.cfg(); u = cfg["user"]
    if a.action == "new":
        self_props = json.loads(ws.db.execute("SELECT props FROM onto_node WHERE id='person:self'").fetchone()["props"])
        choose_rule_set(ws, u, a.rule_set or "standard", a.exception_approval)      # fail before creating anything
        proj = None
        if a.project:                            # report level: given code, completed from the A-box registry
            reg = project_registry(ws).get(a.project, {})
            proj = {"code": a.project, "origin": "user", **{k: v for k, v in reg.items() if k in ALLOC_KEYS},
                    **{k: v for k, v in {"name": a.project_name, "charge_code": a.charge_code, "budget_id": a.budget_id}.items() if v}}
        elif default_project(ws):                # default level: the employee's default project
            proj = {**default_project(ws), "origin": "profile_default",
                    **{k: v for k, v in {"charge_code": a.charge_code, "budget_id": a.budget_id}.items() if v}}
        tmp = ws.root / "drafts" / f"_new_{os.getpid()}"
        ccy = json.loads((ws.root / cfg["policy"]).read_text(encoding="utf-8")).get("report_currency", "CNY")
        display = u.get("name_zh") if ccy == "CNY" and u.get("name_zh") else u["name"]   # Chinese reports use the Chinese name
        args = ["--employee", display]
        for flag, val in [("--employee-id", u.get("employee_id") or self_props.get("employee_id")),
                          ("--department", u.get("department") or self_props.get("department")),
                          ("--title", a.title),
                          ("--purpose", a.purpose), ("--period-start", a.period_start), ("--period-end", a.period_end),
                          ("--policy", str(ws.root / cfg["policy"]))]:
            if val:
                args += [flag, val]
        for _ in range(5):                      # never reuse an existing report id
            code, so, se = ws.run_pkg("init", ["--draft", str(tmp), "--force"] + args)
            if code:
                sys.exit(se or so)
            rid = json.loads(so)["report_id"]
            if not ws.draft(rid).exists():
                break
        else:
            sys.exit("error: could not allocate a unique report id")
        tmp.rename(ws.draft(rid))
        if proj:
            ws.run_pkg("set-header", ["--json", json.dumps({"default_project": proj}, ensure_ascii=False)], rid)
        rs_hdr = choose_rule_set(ws, u, a.rule_set or "standard", a.exception_approval)
        if rs_hdr["id"] != "standard":
            apply_report_rule_set(ws, rid, rs_hdr)
        else:
            ws.run_pkg("gaps", [], rid)
        pa = default_payee(ws, cfg)
        if pa:
            ws.run_pkg("set-header", ["--json", json.dumps({"payee": pa}, ensure_ascii=False)], rid)
        sync_report(ws, rid); ws.log("report_new", {"report_id": rid, "title": a.title, "project": a.project,
                                                    "payee": (pa or {}).get("account_masked")})
        out({"report_id": rid, "draft": str(ws.draft(rid)), "rule_set": rs_hdr,
             "allocation": {k: v for k, v in (proj or {}).items()} or None,
             "suggestions": recall_node(ws, "person:self", top=3) if not proj else {}})
    elif a.action == "rule-set":
        if not a.report_id or not ws.draft(a.report_id).exists():
            sys.exit("usage: report rule-set REPORT_ID --rule-set gm|vp|standard [--exception-approval REF]")
        rs_hdr = choose_rule_set(ws, u, a.rule_set or "standard", a.exception_approval)
        res = apply_report_rule_set(ws, a.report_id, rs_hdr)
        out({"report_id": a.report_id, "rule_set": rs_hdr, **res})
    elif a.action == "list":
        for rid in [p.name for p in (ws.root / "drafts").iterdir() if p.is_dir() and not p.name.startswith("_")]:
            sync_report(ws, rid)
        q = "SELECT * FROM reports" + (" WHERE status=?" if a.status else "") + " ORDER BY updated_at DESC"
        out([dict(r) for r in ws.db.execute(q, (a.status,) if a.status else ())])
    elif a.action == "show":
        r = sync_report(ws, a.report_id)
        if not r:
            sys.exit(f"error: unknown report {a.report_id}")
        vers = [dict(v) for v in ws.db.execute(
            "SELECT version,label,status,grand_total,gaps_blocker,created_at,submitted_at,zip_path FROM report_versions "
            "WHERE report_id=? ORDER BY version", (a.report_id,))]
        code, so, _ = ws.run_pkg("summary", [], a.report_id)
        out({"report": dict(ws.db.execute("SELECT * FROM reports WHERE report_id=?", (a.report_id,)).fetchone()),
             "versions": vers, "gaps": r["gaps"]})
        print(so)


def cmd_pkg(a):
    """Pass-through to expense_pkg for one report; keeps DB, inbox and session log in sync."""
    ws = resolve_ws(a.ws)
    rest = list(a.rest)
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        sys.exit("usage: pkg REPORT_ID <expense_pkg subcommand> [args]")
    sub, args = rest[0], rest[1:]
    if not ws.draft(a.report_id).exists():
        sys.exit(f"error: unknown report {a.report_id}")
    code, so, se = ws.run_pkg(sub, args, a.report_id)
    sys.stdout.write(so); sys.stderr.write(se)
    if code == 0 and sub == "attach":
        res = json.loads(so)
        f = Path(args[args.index("--file") + 1])
        h = sha256_file(f)
        ws.db.execute("""INSERT INTO inputs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET
                         status='ingested', report_id=excluded.report_id, evidence_id=excluded.evidence_id""",
                      (h, str(f), f.suffix.lower(), f.stat().st_size, now(), "ingested", a.report_id, res["evidence_id"], None))
        ws.db.commit()
    r = sync_report(ws, a.report_id)
    payload = {"report_id": a.report_id, "args": args, "exit": code, "result": so[-2000:]}
    if code == 0 and sub == "gaps" and r:
        payload["gaps"] = gap_snapshot(ws, r); payload["status"] = r["report"]["status"]
        payload["result"] = ""
    ws.log(f"pkg:{sub}", payload)
    sys.exit(code)


# ───────────────────────── versioned publish ─────────────────────────
def zip_contents(zf: zipfile.ZipFile) -> list[dict]:
    return [{"path": n, "sha256": sha256_bytes(zf.read(n)), "bytes": zf.getinfo(n).file_size}
            for n in zf.namelist() if n != "MANIFEST.json"]


def diff_reports(old: dict | None, new: dict) -> dict | None:
    if not old:
        return None
    def key(i):
        return json.dumps({k: v for k, v in i.items() if k not in ("status", "amount_reimbursable", "provenance")},
                          sort_keys=True, ensure_ascii=False)
    o = {i["item_id"]: i for i in old["items"]}; n = {i["item_id"]: i for i in new["items"]}
    return {"items_added": sorted(set(n) - set(o)), "items_removed": sorted(set(o) - set(n)),
            "items_changed": sorted(k for k in set(o) & set(n) if key(o[k]) != key(n[k])),
            "evidence_added": sorted({e["evidence_id"] for e in new["evidence"]} - {e["evidence_id"] for e in old["evidence"]}),
            "total_delta": round(new["report"]["totals"]["grand_total"] - old["report"]["totals"]["grand_total"], 2),
            "status_from": old["report"]["status"]}


def cmd_publish(a):
    ws = resolve_ws(a.ws); cfg = ws.cfg(); rid = a.report_id
    if not ws.draft(rid).exists():
        sys.exit(f"error: unknown report {rid}")
    judge_note = None
    if not a.no_judge and ws.jev_env().get("TYPESAFE_API_KEY") and (ws.cfg().get("jev") or {}).get("enabled", True):
        jc, jo, je = ws.run_pkg("judge", [], rid)                 # cached per state: re-publishing costs nothing
        judge_note = json.loads(jo) if jc == 0 and jo.strip().startswith("{") else {"error": (je or jo).strip()[-300:]}
    tmpdir = Path(tempfile.mkdtemp(prefix="expense_pub_"))   # system temp: deletable everywhere
    tmpzip = tmpdir / "pkg.zip"
    code, so, se = ws.run_pkg("package", ["--out", str(tmpzip)] + (["--allow-incomplete"] if a.allow_incomplete else []), rid)
    if code:
        shutil.rmtree(tmpdir, ignore_errors=True)
        sync_report(ws, rid); ws.log("publish_refused", {"report_id": rid, "exit": code})
        sys.stdout.write(so); sys.exit(code)
    last = ws.db.execute("SELECT MAX(version) FROM report_versions WHERE report_id=?", (rid,)).fetchone()[0] or 0
    ver = last + 1; label = f"v{ver:03d}"
    odir = ws.root / "outputs" / rid; odir.mkdir(parents=True, exist_ok=True)
    zpath = odir / f"{rid}_{label}.zip"; mpath = odir / f"{rid}_{label}.manifest.json"
    if zpath.exists():
        sys.exit(f"error: {zpath} already exists — versions are immutable")
    with zipfile.ZipFile(tmpzip) as zf:
        report_bytes = zf.read("report.json"); report = json.loads(report_bytes)
        contents = zip_contents(zf)
        entries = {n: zf.read(n) for n in zf.namelist()}
    parent_doc = None
    if last:
        row = ws.db.execute("SELECT report_json FROM report_docs WHERE report_id=? AND version=?", (rid, last)).fetchone()
        parent_doc = json.loads(row["report_json"]) if row else None
    gaps = report["gaps"]
    lineage = [f"v{v:03d}" for (v,) in ws.db.execute(
        "SELECT version FROM report_versions WHERE report_id=? ORDER BY version", (rid,))] + [label]
    manifest = {
        "manifest_version": "1.0", "report_id": rid, "version": ver, "version_label": label,
        "parent_version": last or None, "created_at": now(),
        **({"judgments": {k: (report.get("judgments") or {}).get(k) for k in ("model", "run_at", "calls")}}
           if report.get("judgments") else {}),
        "created_by": {k: v for k, v in {"name": cfg["user"]["name"], "employee_id": cfg["user"].get("employee_id")}.items() if v},
        "session_id": ws.meta("current_session"), "status": report["report"]["status"],
        "policy_id": report["report"].get("policy_id", ""), "report_schema": "urn:expense-ontology:expense-report:1.0",
        "note": a.note or "", "report_json_sha256": sha256_bytes(report_bytes),
        "totals": report["report"].get("totals", {}),
        "counts": {"items": len(report["items"]), "evidence": len(report["evidence"]),
                   "gaps_blocker": sum(g["severity"] == "blocker" for g in gaps),
                   "gaps_warning": sum(g["severity"] == "warning" for g in gaps),
                   "gaps_info": sum(g["severity"] == "info" for g in gaps)},
        "contents": contents, "changes_from_parent": diff_reports(parent_doc, report), "lineage": lineage,
        "rbox": {**{f"{k}_version": v for k, v in layer_versions(ws).items()},
                 "policy_sha256": sha256_file(ws.draft(rid) / "policy.json")},
    }
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in entries.items():
            z.writestr(n, b)
        z.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest_ext = {**manifest, "zip": {"file": zpath.name, "sha256": sha256_file(zpath), "bytes": zpath.stat().st_size}}
    try:
        import jsonschema
        schema_validator(jsonschema, json.loads((ws.root / "schemas" / "package-manifest.schema.json").read_text())).validate(manifest_ext)
    except ImportError:
        pass
    mpath.write_text(json.dumps(manifest_ext, ensure_ascii=False, indent=2), encoding="utf-8")
    (odir / "latest.json").write_text(json.dumps({"version": ver, "zip": zpath.name, "manifest": mpath.name,
                                                  "status": manifest["status"]}, indent=2), encoding="utf-8")
    ws.db.execute("INSERT INTO report_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, ver, label, str(zpath), str(mpath), manifest_ext["zip"]["sha256"], manifest["report_json_sha256"],
                   manifest["status"], manifest["totals"].get("grand_total", 0), manifest["counts"]["items"],
                   manifest["counts"]["gaps_blocker"], manifest["counts"]["gaps_warning"], last or None,
                   manifest["session_id"], a.note, manifest["created_at"], None))
    ws.db.execute("INSERT INTO report_docs VALUES(?,?,?,?)", (rid, ver, report_bytes.decode("utf-8"),
                                                              json.dumps(manifest_ext, ensure_ascii=False)))
    ws.db.commit()
    sync_report(ws, rid)
    ws.db.execute("UPDATE reports SET latest_version=? WHERE report_id=?", (ver, rid)); ws.db.commit()
    shutil.rmtree(tmpdir, ignore_errors=True)
    learned = None
    if not a.no_learn:
        learned = learn(ws, rid); infer(ws)
    ws.log("publish", {"report_id": rid, "version": label, "zip": str(zpath), "status": manifest["status"],
                       "gaps": gap_snapshot(ws, report), "rbox": manifest["rbox"]})
    out({"report_id": rid, "version": label, "zip": str(zpath), "manifest": str(mpath), "status": manifest["status"],
         "grand_total": manifest["totals"].get("grand_total"), "counts": manifest["counts"],
         "changes_from_parent": manifest["changes_from_parent"], "learned": learned})


def cmd_versions(a):
    ws = resolve_ws(a.ws)
    out([dict(r) for r in ws.db.execute("SELECT * FROM report_versions WHERE report_id=? ORDER BY version", (a.report_id,))])


def cmd_diff(a):
    ws = resolve_ws(a.ws)
    def doc(v):
        r = ws.db.execute("SELECT report_json FROM report_docs WHERE report_id=? AND version=?", (a.report_id, v)).fetchone()
        if not r:
            sys.exit(f"error: no version {v}")
        return json.loads(r["report_json"])
    out(diff_reports(doc(a.a), doc(a.b)))


def cmd_submit(a):
    ws = resolve_ws(a.ws); rid = a.report_id
    v = a.version or ws.db.execute("SELECT MAX(version) FROM report_versions WHERE report_id=?", (rid,)).fetchone()[0]
    row = ws.db.execute("SELECT status FROM report_versions WHERE report_id=? AND version=?", (rid, v)).fetchone()
    if not row:
        sys.exit("error: publish a version first")
    if row["status"] != "ready" and not a.force:
        sys.exit(f"error: version v{v:03d} is '{row['status']}', not ready (use --force)")
    ts = now()
    ws.db.execute("UPDATE report_versions SET submitted_at=? WHERE report_id=? AND version=?", (ts, rid, v))
    ws.run_pkg("set-header", ["--json", json.dumps({"status": "submitted"})], rid)
    sync_report(ws, rid)
    node_upsert(ws, f"report:{rid}", "exp:ExpenseReport", None, {"status": "submitted", "submitted_version": v,
                                                                   "submitted_at": ts})
    ws.db.commit(); ws.log("submit", {"report_id": rid, "version": v})
    out({"report_id": rid, "submitted_version": f"v{v:03d}", "at": ts})


# ───────────────────────── learning: report → A-box ─────────────────────────
def learn(ws: WS, rid: str, from_draft: bool = False) -> dict:
    if from_draft:
        report = json.loads((ws.draft(rid) / "report.json").read_text(encoding="utf-8"))
    else:
        row = ws.db.execute("SELECT report_json FROM report_docs WHERE report_id=? ORDER BY version DESC LIMIT 1", (rid,)).fetchone()
        if not row:
            sys.exit(f"error: {rid} has no published version (use --draft)")
        report = json.loads(row["report_json"])
    src = f"report:{rid}"
    ws.db.execute("DELETE FROM onto_edge WHERE source=? AND basis='observed'", (src,))
    h = report["report"]; me = "person:self"; n_before = ws.db.execute("SELECT COUNT(*) FROM onto_edge").fetchone()[0]
    E = lambda s, p, o, q=None, c=1.0: edge_add(ws, s, p, o, "observed", src, c, q)

    emp = h["employee"]
    node_upsert(ws, me, "exp:Employee", None, {"employee_id": emp.get("employee_id"), "department": emp.get("department")})
    rnode = f"report:{rid}"
    node_upsert(ws, rnode, "exp:ExpenseReport", h["title"],
                {"status": h["status"], "grand_total": (h.get("totals") or {}).get("grand_total"),
                 "currency": h["currency"], "purpose": h.get("purpose"),
                 "period": f"{(h.get('period') or {}).get('start', '')}~{(h.get('period') or {}).get('end', '')}"})
    E(me, "exp:submitted", rnode)
    if h.get("policy_id"):
        node_upsert(ws, f"policy:{h['policy_id']}", "exp:Policy", h["policy_id"]); E(rnode, "exp:governedBy", f"policy:{h['policy_id']}")

    def project(pr):
        if not pr:
            return None
        return register_project(ws, {k: v for k, v in pr.items() if k in ("code", *ALLOC_KEYS)}, basis="observed", source=src)
    rproj = project(h.get("default_project"))
    if rproj:
        E(rnode, "exp:forProject", rproj); E(me, "exp:worksOn", rproj)
    rs = h.get("rule_set") or {}
    if rs.get("id") and rs["id"] != "standard":
        node_upsert(ws, f"ruleset:{rs['id']}", "exp:RuleSet", rs.get("label") or rs["id"])
        E(rnode, "exp:underRuleSet", f"ruleset:{rs['id']}")

    prev = None
    for (v,) in ws.db.execute("SELECT version FROM report_versions WHERE report_id=? ORDER BY version", (rid,)).fetchall():
        vid = f"version:{rid}@v{v:03d}"
        node_upsert(ws, vid, "exp:ReportVersion", f"{rid} v{v:03d}")
        E(rnode, "exp:hasVersion", vid)
        if prev:
            E(vid, "exp:supersedes", prev)
        prev = vid

    core = load_layer(ws, "core")
    std_ids = {s["code"]: f"std:{s['code']}" for s in core.get("standards", [])}
    for ev in report["evidence"]:
        evn = f"evidence:{rid}/{ev['evidence_id']}"
        node_upsert(ws, evn, "exp:Evidence", ev.get("original_name") or ev["evidence_id"],
                    {"kind": ev["kind"], "sha256": ev["sha256"]})
        c = ev.get("credential")
        if c:                                   # the 票据 itself, separate from the file that carries it
            ident = c.get("identity") or {}
            cid = f"credential:{ident.get('invoice_number') or ident.get('receipt_number') or rid + '/' + ev['evidence_id']}"
            node_upsert(ws, cid, c["credential_class"], ident.get("invoice_number") or ident.get("receipt_number") or ev["evidence_id"],
                        {"jurisdiction": c.get("jurisdiction"), "trust_rank": (c.get("source") or {}).get("trust_rank"),
                         "total_amount": (c.get("totals") or {}).get("total_amount"), "currency": (c.get("totals") or {}).get("currency")})
            E(cid, "exp:embodiedIn", evn)
            for s_ in c.get("standards", []):
                if s_ in std_ids:
                    E(cid, "exp:conformsTo", std_ids[s_])
            if (c.get("special") or {}).get("scenario"):
                E(cid, "exp:hasScenario", f"scenario:{c['special']['scenario']}")
            tid = (c.get("buyer") or {}).get("taxpayer_id")
            if tid and ws.db.execute("SELECT 1 FROM onto_node WHERE id=?", (f"invoice_title:{tid}",)).fetchone():
                E(cid, "exp:issuedTo", f"invoice_title:{tid}")
            for iid_ in ev.get("item_ids", []):
                E(cid, "exp:evidences", f"item:{rid}/{iid_}")

    for it in report["items"]:
        if it.get("status") == "excluded":
            continue
        cat = it.get("category", "other"); iid = f"item:{rid}/{it['item_id']}"
        amt = it.get("amount") or {}
        node_upsert(ws, iid, "exp:ExpenseItem", it.get("description"),
                    {"amount": amt.get("value"), "currency": amt.get("currency"), "expense_date": it.get("expense_date")})
        cnode = f"category:{cat}"; node_upsert(ws, cnode, "exp:ExpenseCategory", cat)
        E(rnode, "exp:hasItem", iid); E(iid, "exp:inCategory", cnode)
        for eid in it.get("evidence_ids", []):
            E(iid, "exp:supportedBy", f"evidence:{rid}/{eid}")
        pnode = project(it.get("project")) or rproj
        if it.get("project"):
            E(iid, "exp:forProject", pnode); E(me, "exp:worksOn", pnode)
        acc = it.get("accounting") or {}
        if acc.get("charge_code"):
            node_upsert(ws, f"charge:{acc['charge_code']}", "exp:ChargeCode", acc["charge_code"], {"charge_code": acc["charge_code"]})
            E(iid, "exp:chargedTo", f"charge:{acc['charge_code']}")
        if acc.get("budget_id"):
            node_upsert(ws, f"budget:{acc['budget_id']}", "exp:Budget", acc["budget_id"], {"budget_id": acc["budget_id"]})
            E(iid, "exp:drawsOnBudget", f"budget:{acc['budget_id']}")
        if acc.get("account_code"):
            node_upsert(ws, f"account:coa:{acc['account_code']}", "exp:LedgerAccount", acc.get("account_name") or acc["account_code"],
                        {"account_code": acc["account_code"]})
            E(iid, "exp:postedTo", f"account:coa:{acc['account_code']}")
        t = it.get("trip") or {}
        conf = min([p.get("confidence", 1.0) for p in (it.get("provenance") or {}).values()] or [1.0])
        if it.get("merchant"):
            mcls = CATEGORY_MERCHANT_CLASS.get(cat, "exp:Merchant")
            mid = f"merchant:{norm(it['merchant'])}"
            props = {}
            if cat == "lodging" and t.get("nights") and amt.get("value"):
                props["nightly_rate"] = round(amt["value"] / t["nights"], 2)
            node_upsert(ws, mid, mcls, it["merchant"], props)
            E(iid, "exp:paidAt", mid); E(me, "exp:usedMerchant", mid, c=conf)
            E(mid, "exp:typicalCategory", cnode, c=conf)
            if pnode:
                E(mid, "exp:usedForProject", pnode)
            if mcls == "exp:Hotel":
                E(me, "exp:stayedAt", mid, {"check_in": t.get("check_in"), "nights": t.get("nights")}, conf)
                if t.get("city"):
                    city = f"city:{norm(t['city'])}"; node_upsert(ws, city, "exp:City", t["city"])
                    E(mid, "exp:locatedIn", city)
        if t.get("from") and t.get("to"):
            pcls = CATEGORY_PLACE_CLASS.get(cat, "pcg:Place")
            a_, b_ = f"place:{norm(t['from'])}", f"place:{norm(t['to'])}"
            node_upsert(ws, a_, pcls, t["from"]); node_upsert(ws, b_, pcls, t["to"])
            rt = f"route:{norm(t['from'])}→{norm(t['to'])}"
            node_upsert(ws, rt, "exp:Route", f"{t['from']}→{t['to']}", {"carrier_no": t.get("carrier_no")})
            E(rt, "exp:origin", a_); E(rt, "exp:destination", b_)
            E(me, "exp:travelsRoute", rt, {"date": it.get("expense_date")}, conf)
            E(rt, "exp:typicalCategory", cnode); E(iid, "exp:onRoute", rt)
            if pnode:
                E(rt, "exp:usedForProject", pnode)
        if it.get("payment_method"):
            pm = f"instrument:{it['payment_method']}"; node_upsert(ws, pm, "exp:PaymentInstrument", it["payment_method"])
            E(iid, "exp:paidWith", pm); E(me, "exp:usesInstrument", pm)
    ws.db.commit()
    n_after = ws.db.execute("SELECT COUNT(*) FROM onto_edge WHERE source=?", (src,)).fetchone()[0]
    return {"source": src, "edges": n_after, "net_new_edges": ws.db.execute("SELECT COUNT(*) FROM onto_edge").fetchone()[0] - n_before}


def cmd_learn(a):
    ws = resolve_ws(a.ws); res = learn(ws, a.report_id, a.draft); res["inferred"] = infer(ws); out(res)


# ───────────────────────── R-box: inference & recall ─────────────────────────
def infer(ws: WS) -> int:
    rbox = load_box(ws, "rbox")
    ws.db.execute("DELETE FROM onto_edge WHERE basis='inferred'")
    facts = {(r["subj"], r["pred"], r["obj"]) for r in ws.db.execute("SELECT DISTINCT subj,pred,obj FROM onto_edge")}
    derived: dict[tuple, str] = {}
    for _ in range(10):
        new = {}
        allf = facts | set(derived)
        by_pred: dict[str, list] = {}
        for s, p, o in allf:
            by_pred.setdefault(p, []).append((s, o))
        for inv in rbox.get("inverses", []):
            for s, o in by_pred.get(inv["property"], []):
                new[(o, inv["inverse"], s)] = inv["id"]
            for s, o in by_pred.get(inv["inverse"], []):
                new[(o, inv["property"], s)] = inv["id"]
        for prop, chars in (rbox.get("characteristics") or {}).items():
            if "transitive" in chars:
                pairs = by_pred.get(prop, [])
                idx: dict[str, list] = {}
                for s, o in pairs:
                    idx.setdefault(s, []).append(o)
                for s, o in pairs:
                    for o2 in idx.get(o, []):
                        if o2 != s:
                            new[(s, prop, o2)] = f"TRANS-{prop}"
        for ch in rbox.get("chains", []):
            p1, p2 = ch["chain"]
            idx = {}
            for s, o in by_pred.get(p2, []):
                idx.setdefault(s, []).append(o)
            for s, o in by_pred.get(p1, []):
                for o2 in idx.get(o, []):
                    new[(s, ch["implies"], o2)] = ch["id"]
        added = {k: v for k, v in new.items() if k not in allf}
        if not added:
            break
        derived.update(added)
    for (s, p, o), rule in derived.items():
        edge_add(ws, s, p, o, "inferred", f"rbox:{rule}", 1.0)
    ws.db.commit()
    return len(derived)


def aggregate(ws: WS, subj: str, pred: str) -> list[dict]:
    rows = ws.db.execute("""SELECT e.obj, n.label, n.class,
                SUM(CASE basis WHEN 'stated' THEN 3 WHEN 'observed' THEN 1 ELSE 0.5 END) AS score,
                COUNT(DISTINCT source) AS sources, MAX(last_seen) AS last_seen, MAX(confidence) AS confidence,
                GROUP_CONCAT(DISTINCT basis) AS basis
             FROM onto_edge e LEFT JOIN onto_node n ON n.id=e.obj
             WHERE e.subj=? AND e.pred=? GROUP BY e.obj ORDER BY score DESC, last_seen DESC""", (subj, pred)).fetchall()
    return [dict(r) for r in rows]


def recall_node(ws: WS, nid: str, top: int = 5) -> dict:
    tbox, rbox = load_box(ws, "tbox"), load_box(ws, "rbox")
    row = ws.db.execute("SELECT * FROM onto_node WHERE id=?", (nid,)).fetchone()
    if not row:
        return {}
    supers = superclasses(tbox, row["class"])
    res = {}
    for rule in rbox.get("suggest", []):
        if rule["key_class"] not in supers:
            continue
        frontier = [(nid, 1.0)]
        for pred in rule["path"]:
            scores: dict[str, dict] = {}
            for s, w in frontier:
                for c in aggregate(ws, s, pred):
                    d = scores.setdefault(c["obj"], {**c, "score": 0.0})
                    d["score"] += c["score"] * w
            frontier = [(k, v["score"]) for k, v in scores.items()]
        total = sum(v["score"] for v in scores.values()) or 1
        cands = sorted(scores.values(), key=lambda v: (-v["score"], v["last_seen"] or ""))[:top]
        if cands:
            aw = rule.get("auto_when") or {}
            def apply(c, first):
                share = c["score"] / total
                ok = (share >= aw.get("min_share", 1.01) and
                      (c["sources"] >= aw.get("min_sources", 99) or "stated" in (c["basis"] or "")))
                return "auto" if first and rule.get("mode") == "auto" and ok else "ask"
            res[rule["id"]] = {"fills": rule["fills"], "mode": rule.get("mode", "suggest"), "candidates": [
                {"id": c["obj"], "label": c["label"], "class": c["class"], "score": round(c["score"], 2),
                 "share": round(c["score"] / total, 2), "sources": c["sources"], "basis": c["basis"],
                 "last_seen": c["last_seen"], "apply": apply(c, i == 0)} for i, c in enumerate(cands)]}
    return res


def offers_of(result: dict) -> list[dict]:
    """Flatten recall output into the offers the evaluator later scores (accepted / rejected / unused)."""
    offers = []
    for nid, v in result.items():
        for rid, s in (v.get("suggestions") or {}).items():
            c = s["candidates"][0]
            offers.append({"node": nid, "rule": rid, "fills": s["fills"], "top_id": c["id"], "top_label": c["label"],
                           "share": c["share"], "apply": c["apply"]})
    return offers


def cmd_recall(a):
    ws = resolve_ws(a.ws)
    targets = []
    if a.self_:
        targets.append("person:self")
    if a.node:
        targets.append(a.node)
    if a.label:
        targets += [r["id"] for r in find_nodes(ws, a.label, a.cls)]
    if a.route:
        targets.append(f"route:{norm(a.route[0])}→{norm(a.route[1])}")
    matched = []
    if a.text:  # every known entity mentioned (exactly or fuzzily) in a free-text utterance
        matched = mentions(ws, a.text)
        targets += [m["id"] for m in matched]
    seen, result = set(), {}
    for nid in targets:
        if nid in seen:
            continue
        seen.add(nid)
        row = ws.db.execute("SELECT id,label,class FROM onto_node WHERE id=?", (nid,)).fetchone()
        if row:
            result[nid] = {"label": row["label"], "class": row["class"], "suggestions": recall_node(ws, nid, a.top)}
    ws.log("recall", {"targets": list(result), "query": {"text": a.text, "label": a.label, "class": a.cls,
                                                         "self": a.self_, "node": a.node},
                      "offers": offers_of(result)})
    if a.text:
        out({"mentions": matched, "suggestions": {k: v for k, v in result.items() if v["suggestions"]}})
    else:
        out(result or {"note": "no matching memory"})


def bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def mentions(ws: WS, text: str, threshold: float = 0.7) -> list[dict]:
    """Entities referenced in an utterance: exact substring, or bigram containment ≥ threshold
    (handles '静安某某酒店' vs '上海静安某某酒店'). Routes are added when both endpoints are mentioned."""
    t = norm(text); tb = bigrams(t)
    rows = ws.db.execute("SELECT id,label,aliases,class FROM onto_node WHERE class NOT IN "
                         "('exp:ExpenseItem','exp:Evidence','exp:ReportVersion','exp:ExpenseReport','exp:ExpenseCategory')").fetchall()
    hits = {}
    for r in rows:
        best = 0.0
        for n in [norm(r["label"])] + [norm(x) for x in json.loads(r["aliases"])]:
            if len(n) < 2:
                continue
            if n in t:
                best = 1.0; break
            if len(n) >= 4:
                nb = bigrams(n)
                best = max(best, len(nb & tb) / len(nb))
        if best >= threshold:
            hits[r["id"]] = {"id": r["id"], "label": r["label"], "class": r["class"], "match": round(best, 2)}
    places = {k for k in hits if k.startswith("place:") or k.startswith("city:")}
    for r in ws.db.execute("SELECT subj, GROUP_CONCAT(pred||'='||obj) ends FROM onto_edge WHERE pred IN "
                           "('exp:origin','exp:destination') GROUP BY subj"):
        ends = dict(x.split("=", 1) for x in r["ends"].split(","))
        if ends.get("exp:origin") in places and ends.get("exp:destination") in places and r["subj"] not in hits:
            n = ws.db.execute("SELECT label,class FROM onto_node WHERE id=?", (r["subj"],)).fetchone()
            hits[r["subj"]] = {"id": r["subj"], "label": n["label"], "class": n["class"], "match": 1.0, "via": "endpoints"}
    return sorted(hits.values(), key=lambda h: -h["match"])


# ───────────────────────── rules (R-box) ─────────────────────────
def apply_patch(data: dict, ops: list[dict]) -> list[str]:
    done = []
    for op in ops:
        sec = data.setdefault(op["section"], []) if op["op"] != "set" else None
        if op["op"] == "add":
            if any(x.get("id") == op["value"]["id"] for x in sec):
                raise ValueError(f"id exists: {op['value']['id']}")
            sec.append(op["value"]); done.append(f"add {op['section']}:{op['value']['id']}")
        elif op["op"] == "update":
            x = next((x for x in sec if x.get("id") == op["id"]), None)
            if not x:
                raise ValueError(f"unknown id {op['id']} in {op['section']}")
            for k, v in (op.get("set") or {}).items():
                x[k] = v
            for k in op.get("unset") or []:
                x.pop(k, None)
            done.append(f"update {op['section']}:{op['id']} {sorted(list(op.get('set') or {}) + list(op.get('unset') or []))}")
        elif op["op"] == "remove":
            n = len(sec); sec[:] = [x for x in sec if x.get("id") != op["id"]]
            if len(sec) == n:
                raise ValueError(f"unknown id {op['id']}")
            done.append(f"remove {op['section']}:{op['id']}")
        elif op["op"] == "set":   # top-level mapping, e.g. evolution thresholds
            data.setdefault(op["key"], {}).update(op["value"]); done.append(f"set {op['key']}")
        else:
            raise ValueError(f"bad op {op['op']}")
    return done


def open_reports(ws: WS) -> list[str]:
    return [r[0] for r in ws.db.execute("SELECT report_id FROM reports WHERE status NOT IN ('submitted')")]


def choose_rule_set(ws: WS, user: dict, rs_id: str, approval: str | None) -> dict:
    """Company rule set for a report. Eligible when the employee's grade (profile user.grade) reaches the set's
    min_grade; otherwise allowed only as a recorded exception with an approval reference (and, in the report, an
    approval voucher — see exception_policy)."""
    org = load_layer(ws, "org")
    sets = {s["id"]: s for s in rule_sets(org)}
    if rs_id not in sets:
        sys.exit(f"error: unknown rule set {rs_id} — company rule sets: {', '.join(sets)}")
    s = sets[rs_id]
    grades = (org.get("settings") or {}).get("grades") or ["staff"]
    rank = lambda g: grades.index(g) if g in grades else 0
    need, have = s.get("min_grade", "staff"), user.get("grade") or "staff"
    hdr = {"id": rs_id, "label": s.get("label", rs_id)}
    if rank(have) >= rank(need):
        return {**hdr, "basis": "grade", "grade": have}
    if not approval:
        sys.exit(f"error: rule set {rs_id} needs grade {need} (profile grade: {have}). Use it only as an approved exception: "
                 f"--exception-approval \"<审批单号/审批人>\" (an approval voucher must then be attached to the report)")
    return {**hdr, "basis": "exception", "grade": have, "exception": {"approval_ref": approval, "recorded_at": now()}}


def apply_report_rule_set(ws: WS, rid: str, rs_hdr: dict) -> dict:
    code, so, se = ws.run_pkg("set-header", ["--json", json.dumps({"rule_set": rs_hdr}, ensure_ascii=False)], rid)
    if code:
        sys.exit(se or so)
    synced = sync_policy(ws, [rid])
    ws.log("report_rule_set", {"report_id": rid, "rule_set": rs_hdr})
    return {"synced": synced}


def report_rule_set(ws: WS, rid: str) -> str:
    try:
        rep = json.loads((ws.draft(rid) / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "standard"
    return ((rep.get("report") or {}).get("rule_set") or {}).get("id", "standard")


def sync_policy(ws: WS, rids: list[str]) -> list[dict]:
    res = []
    for rid in rids:
        if not ws.draft(rid).exists():
            continue
        rs = report_rule_set(ws, rid)
        src = compiled_paths(ws, rs)[0]
        if not src.exists():                    # rule set withdrawn by the company → fall back to standard, visibly
            src = compiled_paths(ws)[0]
        shutil.copy2(src, ws.draft(rid) / "policy.json")
        code, so, _ = ws.run_pkg("gaps", [], rid)
        r = sync_report(ws, rid)
        res.append({"report_id": rid, "status": r["report"]["status"] if r else None,
                    "blockers": sum(1 for g in (r or {}).get("gaps", []) if g["severity"] == "blocker")})
    return res


def rule_set_errors(org: dict) -> list[str]:
    errs = []
    for s in rule_sets(org):
        try:
            apply_rule_set(org, s["id"])
        except ValueError as e:
            errs.append(f"org.rule_sets: {e}")
    return errs


def check_rbox(ws: WS) -> dict:
    errs = []
    tbox = load_box(ws, "tbox")
    schema_p = ws.root / "schemas" / "rbox-layer.schema.json"
    try:
        import jsonschema
        schema = json.loads(schema_p.read_text())
    except (ImportError, FileNotFoundError):
        jsonschema = None
    for layer in RBOX_LAYERS:
        d = load_layer(ws, layer)
        if jsonschema:
            for e in schema_validator(jsonschema, schema).iter_errors(d):
                errs.append(f"{layer}: /{'/'.join(map(str, e.path))}: {e.message}")
        for sec in ("rules", "recall", "prompts", "personal_checks", "inverses", "chains"):
            ids = [x.get("id") for x in d.get(sec, []) or []]
            dup = {i for i in ids if ids.count(i) > 1}
            if dup:
                errs.append(f"{layer}.{sec}: duplicate ids {sorted(dup)}")
        for r in d.get("personal_checks", []) or []:
            if r.get("kind") == "invoice_title" and not ws.db.execute(
                    "SELECT 1 FROM onto_node WHERE id=? AND class='exp:InvoiceTitle'", (r.get("title_ref"),)).fetchone():
                errs.append(f"{layer}.personal_checks {r['id']}: title_ref {r.get('title_ref')} not an exp:InvoiceTitle in the A-box")
        for r in d.get("recall", []) or []:
            if r["key_class"] not in tbox.get("classes", {}):
                errs.append(f"{layer}.recall {r['id']}: unknown class {r['key_class']}")
            for pth in r["path"]:
                if pth not in tbox.get("objectProperties", {}):
                    errs.append(f"{layer}.recall {r['id']}: unknown property {pth}")
    return {"valid": not errs, "errors": errs, "versions": layer_versions(ws)}


def cmd_rules(a):
    ws = resolve_ws(a.ws)
    if a.action == "show":
        if a.layer:
            print(layer_path(ws, a.layer).read_text(encoding="utf-8")); return
        org, per = load_layer(ws, "org"), load_layer(ws, "personal")
        out({"versions": layer_versions(ws), "org": {"source": org.get("source"), "rules": len(org.get("rules", [])),
             "settings": org.get("settings")},
             "personal": {"recall": {r["id"]: r["mode"] for r in per.get("recall", [])},
                          "prompts": [p["id"] for p in per.get("prompts", [])],
                          "personal_checks": [c["id"] for c in per.get("personal_checks", [])],
                          "evolution": per.get("evolution")}})
    elif a.action == "compile":
        out(compile_rules(ws))
    elif a.action == "sync":
        compile_rules(ws)
        out(sync_policy(ws, a.report or open_reports(ws)))
    elif a.action == "import-policy":
        pol = json.loads(Path(a.file).expanduser().read_text(encoding="utf-8"))
        layer_path(ws, "org").write_text(dump_yaml(policy_to_org(pol, f"import:{Path(a.file).name}")), encoding="utf-8")
        snapshot_layer(ws, "org", "org-import", a.note or f"import {Path(a.file).name}")
        company = seed_company(ws)
        c = compile_rules(ws); ws.log("rules_import", {"file": a.file, "compiled": c, "company": company})
        out({"compiled": c, "company": company, "synced": sync_policy(ws, open_reports(ws))})
    elif a.action == "patch":
        if a.layer == "core" or (a.layer == "org" and a.actor != "org-import"):
            sys.exit(f"error: the {a.layer} layer is read-only on the employee side "
                     "(company rules change only by `rules import-policy`; propose changes via the evolver outbox)")
        ops = json.loads(a.json)
        ops = ops if isinstance(ops, list) else [ops]
        data = load_layer(ws, a.layer)
        try:
            done = apply_patch(data, ops)
        except ValueError as e:
            sys.exit(f"error: {e}")
        v = save_layer(ws, a.layer, data, a.actor or "user", a.note or "; ".join(done))
        chk = check_rbox(ws)
        if not chk["valid"]:
            prev = ws.db.execute("SELECT content FROM rbox_versions WHERE layer=? AND version<? ORDER BY version DESC LIMIT 1",
                                 (a.layer, v)).fetchone()
            layer_path(ws, a.layer).write_text(prev["content"], encoding="utf-8")
            ws.db.execute("DELETE FROM rbox_versions WHERE layer=? AND version=?", (a.layer, v)); ws.db.commit()
            sys.exit(f"error: patch rejected, R-box invalid: {chk['errors'][:5]}")
        c = compile_rules(ws)
        ws.log("rules_patch", {"layer": a.layer, "version": v, "ops": done, "actor": a.actor or "user"})
        out({"layer": a.layer, "version": v, "applied": done, "compiled": c,
             "synced": sync_policy(ws, open_reports(ws)) if not a.no_sync else []})
    elif a.action == "history":
        q = "SELECT layer,version,sha256,actor,note,created_at FROM rbox_versions" + (" WHERE layer=?" if a.layer else "") + \
            " ORDER BY layer, version"
        out([dict(r) for r in ws.db.execute(q, (a.layer,) if a.layer else ())])
    elif a.action == "revert":
        if a.layer != "personal":
            sys.exit("error: only the personal layer can be reverted on the employee side")
        row = ws.db.execute("SELECT content FROM rbox_versions WHERE layer=? AND version=?", (a.layer, a.version)).fetchone()
        if not row:
            sys.exit("error: no such version")
        data = yaml.safe_load(row["content"]); data["version"] = int(load_layer(ws, a.layer)["version"])
        v = save_layer(ws, a.layer, data, a.actor or "user", f"revert to v{a.version}")
        c = compile_rules(ws); ws.log("rules_revert", {"layer": a.layer, "to": a.version, "new_version": v})
        out({"layer": a.layer, "version": v, "reverted_to": a.version, "compiled": c, "synced": sync_policy(ws, open_reports(ws))})
    elif a.action == "hints":
        attrs = {"category": a.category, "city": a.city, "method": a.method, "currency": a.currency}
        per, org = load_layer(ws, "personal"), load_layer(ws, "org")
        prompts = [p for p in per.get("prompts", []) if p.get("status", "active") == "active" and matches(p["applies_to"], attrs)]
        company = [r for r in org.get("rules", []) if r.get("status", "active") == "active"
                   and r["applies_to"].get("category") == a.category
                   and (not r["applies_to"].get("city") or (a.city and r["applies_to"]["city"] in a.city))]
        out({"ask_now": [{"id": p["id"], "ask": p["ask"], "text": p["text"]} for p in prompts],
             "company_requires": [{"id": r["id"], "kind": r["kind"], **{k: r[k] for k in ("field", "any_of", "amount", "per", "mode")
                                                                         if k in r}} for r in company]})
        ws.log("hints", {"attrs": attrs, "prompts": [p["id"] for p in prompts]})
    elif a.action == "check":
        out(check_rbox(ws))


# ───────────────────────── onto maintenance ─────────────────────────
def cmd_onto(a):
    ws = resolve_ws(a.ws)
    if a.action == "assert":
        if a.o_class:
            node_upsert(ws, a.o, a.o_class, a.o_label or a.o)
        edge_add(ws, a.s, a.p, a.o, "stated", a.source or f"user:{ws.meta('current_session') or dt.date.today()}",
                 1.0, json.loads(a.qualifiers) if a.qualifiers else None)
        ws.db.commit(); ws.log("onto_assert", {"s": a.s, "p": a.p, "o": a.o}); out({"ok": True})
    elif a.action == "retract":
        n = ws.db.execute("DELETE FROM onto_edge WHERE subj=? AND pred=? AND obj=? AND basis IN ('stated','observed')",
                          (a.s, a.p, a.o)).rowcount
        ws.db.commit(); ws.log("onto_retract", {"s": a.s, "p": a.p, "o": a.o}); out({"removed": n})
    elif a.action == "node":
        node_upsert(ws, a.id, a.cls, a.label, json.loads(a.props) if a.props else None, a.alias)
        ws.db.commit(); out(dict(ws.db.execute("SELECT * FROM onto_node WHERE id=?", (a.id,)).fetchone()))
    elif a.action == "show":
        n = ws.db.execute("SELECT * FROM onto_node WHERE id=?", (a.id,)).fetchone()
        if not n:
            sys.exit("error: unknown node")
        outs = [dict(r) for r in ws.db.execute("SELECT pred,obj,basis,COUNT(*) n,MAX(last_seen) last FROM onto_edge "
                                                "WHERE subj=? GROUP BY pred,obj,basis ORDER BY pred", (a.id,))]
        ins = [dict(r) for r in ws.db.execute("SELECT subj,pred,basis,COUNT(*) n FROM onto_edge "
                                               "WHERE obj=? GROUP BY subj,pred,basis ORDER BY pred", (a.id,))]
        out({"node": {**dict(n), "props": masked_props(json.loads(n["props"])), "aliases": json.loads(n["aliases"])},
             "out": outs, "in": ins})
    elif a.action == "search":
        out([{"id": r["id"], "class": r["class"], "label": r["label"]} for r in find_nodes(ws, a.q, a.cls, 20)])
    elif a.action == "infer":
        out({"inferred_edges": infer(ws)})
    elif a.action == "validate":
        out(validate_abox(ws))
    elif a.action == "stats":
        out({"nodes_by_class": {r[0]: r[1] for r in ws.db.execute("SELECT class,COUNT(*) FROM onto_node GROUP BY class")},
             "edges_by_pred": {r[0]: r[1] for r in ws.db.execute("SELECT pred,COUNT(*) FROM onto_edge GROUP BY pred")},
             "edges_by_basis": {r[0]: r[1] for r in ws.db.execute("SELECT basis,COUNT(*) FROM onto_edge GROUP BY basis")},
             "tbox": load_box(ws, "tbox").get("version"), "rbox": layer_versions(ws)})
    elif a.action == "export":
        out(export(ws, a.format, Path(a.dest).expanduser() if a.dest else None))


def validate_abox(ws: WS) -> dict:
    tbox = load_box(ws, "tbox")
    classes = set(tbox.get("classes", {}))
    props = tbox.get("objectProperties", {})
    cls_of = {r["id"]: r["class"] for r in ws.db.execute("SELECT id,class FROM onto_node")}
    errs = []
    for nid, c in cls_of.items():
        if c not in classes:
            errs.append(f"node {nid}: unknown class {c}")
    as_list = lambda v: v if isinstance(v, list) else [v]
    for r in ws.db.execute("SELECT DISTINCT subj,pred,obj FROM onto_edge"):
        s, p, o = r
        if p not in props:
            errs.append(f"edge {s} {p} {o}: unknown property"); continue
        for end, node, key in [("subject", s, "domain"), ("object", o, "range")]:
            if node not in cls_of:
                errs.append(f"edge {s} {p} {o}: {end} node missing"); continue
            allowed = as_list(props[p].get(key)) if props[p].get(key) else []
            if allowed and not set(allowed) & set(superclasses(tbox, cls_of[node])):
                errs.append(f"edge {s} {p} {o}: {end} class {cls_of[node]} ∉ {allowed}")
    return {"valid": not errs, "errors": errs[:200], "error_count": len(errs)}


def masked_props(p: dict) -> dict:
    """Personal account numbers never leave the local A-box unmasked (company invoicing accounts are public)."""
    return {k: (mask_account(v) if k == "account_number" else v) for k, v in p.items()}


def export(ws: WS, fmt: str, dest: Path | None) -> dict:
    nodes = {r["id"]: {**dict(r), "props": json.dumps(masked_props(json.loads(r["props"])), ensure_ascii=False)}
             for r in ws.db.execute("SELECT * FROM onto_node")}
    edges = [dict(r) for r in ws.db.execute(
        "SELECT subj,pred,obj,GROUP_CONCAT(DISTINCT basis) basis,COUNT(DISTINCT source) weight FROM onto_edge GROUP BY subj,pred,obj")]
    if fmt == "obsidian":
        dest = dest or ws.root / "ontology" / "obsidian"
        dest.mkdir(parents=True, exist_ok=True)              # notes are overwritten in place
        fname = lambda nid: re.sub(r'[\\/:*?"<>|#^\[\]]', "_", nid)
        outgoing: dict[str, list] = {}
        for e in edges:
            outgoing.setdefault(e["subj"], []).append(e)
        for nid, n in nodes.items():
            folder = dest / n["class"].split(":")[-1]; folder.mkdir(exist_ok=True)
            lines = ["---", f"id: \"{nid}\"", f"class: \"{n['class']}\"", f"aliases: {n['aliases']}",
                     f"props: {n['props']}", "segment: expense", "---", f"# {n['label']}", ""]
            for e in sorted(outgoing.get(nid, []), key=lambda e: e["pred"]):
                lines.append(f"- {e['pred']} → [[{fname(e['obj'])}|{nodes.get(e['obj'], {}).get('label', e['obj'])}]]"
                             f"  _(w={e['weight']}, {e['basis']})_")
            (folder / f"{fname(nid)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"format": "obsidian", "dest": str(dest), "notes": len(nodes)}
    dest = dest or ws.root / "ontology" / "abox.json"
    dest.write_text(json.dumps({"segment": "expense", "exported_at": now(), "nodes": list(nodes.values()), "edges": edges},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return {"format": "json", "dest": str(dest), "nodes": len(nodes), "edges": len(edges)}


# ───────────────────────── CLI ─────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(prog="expense_prep", description="Employee-side expense workspace")
    ap.add_argument("--ws", help="workspace folder (default: $EXPENSE_WS or ~/ExpenseWorkspace/<current user>)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("init"); s.set_defaults(fn=cmd_init)
    s.add_argument("--user", required=True); s.add_argument("--slug"); s.add_argument("--root", default=str(DEFAULT_ROOT))
    s.add_argument("--employee-id"); s.add_argument("--department"); s.add_argument("--email"); s.add_argument("--org")
    s.add_argument("--policy", help="policy path relative to the workspace"); s.add_argument("--pkg-skill")
    s.add_argument("--force", action="store_true")

    sp.add_parser("status").set_defaults(fn=cmd_status)

    s = sp.add_parser("session"); s.set_defaults(fn=cmd_session)
    s.add_argument("action", choices=["start", "log", "end", "list"]); s.add_argument("--goal"); s.add_argument("--kind", default="note")
    s.add_argument("--text"); s.add_argument("--json"); s.add_argument("--summary"); s.add_argument("--limit", type=int, default=10)
    s.add_argument("--force", action="store_true")

    s = sp.add_parser("inbox"); s.set_defaults(fn=cmd_inbox)
    s.add_argument("action", choices=["scan", "mark", "list"]); s.add_argument("--folder", action="append")
    s.add_argument("--sha"); s.add_argument("--status"); s.add_argument("--note")

    s = sp.add_parser("report"); s.set_defaults(fn=cmd_report)
    s.add_argument("action", choices=["new", "list", "show", "rule-set"]); s.add_argument("report_id", nargs="?")
    s.add_argument("--rule-set", help="company rule set: standard (default) | gm | vp …")
    s.add_argument("--charge-code", help="report-level charge code (default: from the project registry)")
    s.add_argument("--budget-id", help="report-level budget id (default: from the project registry)")
    s.add_argument("--exception-approval", help="approval reference when the employee's grade is below the rule set's")
    s.add_argument("--title"); s.add_argument("--project"); s.add_argument("--project-name"); s.add_argument("--purpose")
    s.add_argument("--period-start"); s.add_argument("--period-end"); s.add_argument("--status")

    s = sp.add_parser("pkg", help="pass-through: pkg REPORT_ID <expense_pkg subcommand> [args]"); s.set_defaults(fn=cmd_pkg)
    s.add_argument("report_id"); s.add_argument("rest", nargs=argparse.REMAINDER)

    s = sp.add_parser("publish"); s.set_defaults(fn=cmd_publish)
    s.add_argument("report_id"); s.add_argument("--note"); s.add_argument("--allow-incomplete", action="store_true")
    s.add_argument("--no-judge", action="store_true", help="skip the TypeSafe Jev judgments")
    s.add_argument("--no-learn", action="store_true")

    s = sp.add_parser("versions"); s.set_defaults(fn=cmd_versions); s.add_argument("report_id")
    s = sp.add_parser("diff"); s.set_defaults(fn=cmd_diff); s.add_argument("report_id")
    s.add_argument("a", type=int); s.add_argument("b", type=int)
    s = sp.add_parser("submit"); s.set_defaults(fn=cmd_submit); s.add_argument("report_id")
    s.add_argument("--version", type=int); s.add_argument("--force", action="store_true")

    s = sp.add_parser("learn"); s.set_defaults(fn=cmd_learn); s.add_argument("report_id")
    s.add_argument("--draft", action="store_true", help="learn from the live draft instead of the last published version")

    s = sp.add_parser("recall"); s.set_defaults(fn=cmd_recall)
    s.add_argument("--self", dest="self_", action="store_true"); s.add_argument("--node"); s.add_argument("--label")
    s.add_argument("--class", dest="cls"); s.add_argument("--route", nargs=2, metavar=("FROM", "TO"))
    s.add_argument("--text"); s.add_argument("--top", type=int, default=5)

    s = sp.add_parser("bip", help="record BIP / OA state of a report's invoices (查验结果, 保存, 报账状态, 发票明细, 转移)")
    s.set_defaults(fn=cmd_bip); s.add_argument("report_id")
    s.add_argument("--items", help="all (default) | vat | ITM-0001,ITM-0002")
    s.add_argument("--save-status", help="saved | failed | not_collected (or 已保存 / 保存失败)")
    s.add_argument("--verify", help="BIP 查验结果 verbatim, e.g. 相符 / 不符 / 查无此票")
    s.add_argument("--reimb-status", help="unreported | partially_reported | reported (or 未报账 / 部分已报账 / 已报账)")
    s.add_argument("--oa-detail", help="ok | empty (or 明细完整 / 明细为空)")
    s.add_argument("--reason", help="failure reason shown by BIP, e.g. 发票号已存在")
    s.add_argument("--warning", action="append", help="non-blocking BIP prompt, e.g. 序号角标提示")
    s.add_argument("--docs", action="append", help="associated document shown under 业务状态")
    s.add_argument("--transfer-to", help="colleague the invoice is transferred to (only 未报账)")
    s.add_argument("--note", help="the user's own words, kept as the source")

    s = sp.add_parser("project", help="project / charge-code / budget registry (A-box)"); s.set_defaults(fn=cmd_project)
    s.add_argument("action", choices=["set", "list", "show"]); s.add_argument("code", nargs="?"); s.add_argument("--json")

    s = sp.add_parser("ingest", help="one command per voucher: attach, parse 数电票 XML, create the item, judge")
    s.set_defaults(fn=cmd_ingest); s.add_argument("report_id"); s.add_argument("--file", action="append", required=True)
    s.add_argument("--kind"); s.add_argument("--item", help="attach as more evidence for this item instead of creating one")
    s.add_argument("--no-judge", action="store_true")

    s = sp.add_parser("secret", help="workspace secret store (TypeSafe Jev API key)"); s.set_defaults(fn=cmd_secret)
    s.add_argument("action", choices=["set", "status", "remove", "test"]); s.add_argument("name", nargs="?", default="typesafe")
    s.add_argument("--value", help=argparse.SUPPRESS)          # stdin is preferred so the key never appears in argv

    s = sp.add_parser("profile", help="employee profile and 发票抬头"); s.set_defaults(fn=cmd_profile)
    s.add_argument("action", choices=["show", "set"]); s.add_argument("--json")

    s = sp.add_parser("rules", help="R-box business/personal rules"); s.set_defaults(fn=cmd_rules)
    s.add_argument("action", choices=["show", "compile", "sync", "import-policy", "patch", "history", "revert", "hints", "check"])
    s.add_argument("--layer", choices=RBOX_LAYERS); s.add_argument("--report", action="append"); s.add_argument("--file")
    s.add_argument("--json"); s.add_argument("--actor"); s.add_argument("--note"); s.add_argument("--version", type=int)
    s.add_argument("--category"); s.add_argument("--city"); s.add_argument("--method"); s.add_argument("--currency")
    s.add_argument("--no-sync", action="store_true")

    s = sp.add_parser("onto"); s.set_defaults(fn=cmd_onto)
    s.add_argument("action", choices=["assert", "retract", "node", "show", "search", "infer", "validate", "stats", "export"])
    s.add_argument("--s"); s.add_argument("--p"); s.add_argument("--o"); s.add_argument("--o-class"); s.add_argument("--o-label")
    s.add_argument("--source"); s.add_argument("--qualifiers"); s.add_argument("--id"); s.add_argument("--class", dest="cls")
    s.add_argument("--label"); s.add_argument("--props"); s.add_argument("--alias", action="append"); s.add_argument("--q")
    s.add_argument("--format", choices=["obsidian", "json"], default="json"); s.add_argument("--dest")

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
