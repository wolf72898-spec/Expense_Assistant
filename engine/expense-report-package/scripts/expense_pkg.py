#!/usr/bin/env python3
"""expense_pkg — deterministic core of the expense-report skill.

The agent does perception (reading PDFs / images / speech) and decides WHAT an
item is. This script owns everything that must be exact and reproducible:
draft state, ids, evidence hashing, provenance, trace log, policy gaps, totals,
schema validation and the final Expense_Report_Package.zip.

Draft layout (a folder, one per report):
    <draft>/report.json      live report (always schema-shaped)
    <draft>/policy.json      policy snapshot used for this report
    <draft>/evidence/        vouchers copied in as EV-xxxx.<ext>

Stdlib only; `jsonschema` (optional) enables full schema validation;
`pypdf` (optional) enables text extraction in `scan`.
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
import argparse
import copy
import datetime as dt
import hashlib
import json
import mimetypes
import re
import secrets
import shutil
import sys
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SKILL_ROOT / "schemas" / "expense-report.schema.json"
DEFAULT_POLICY = SKILL_ROOT / "config" / "policy.default.json"
EVIDENCE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff", ".ofd", ".xml"}


# ───────────────────────── helpers ─────────────────────────
def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json_arg(value: str):
    """Accept inline JSON, @file.json, or a path to a .json file."""
    if value.startswith("@"):
        value = value[1:]
    p = Path(value)
    if p.suffix == ".json" and p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(value)


def get_path(obj: dict, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if cur not in ("", [], {}) else None


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict) and k != "amount":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def next_id(prefix: str, existing: list[str]) -> str:
    nums = [int(x.split("-")[-1]) for x in existing if x.startswith(prefix + "-") and x.split("-")[-1].isdigit()]
    return f"{prefix}-{(max(nums) + 1) if nums else 1:04d}"


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ───────────────────────── draft store ─────────────────────────
class Draft:
    def __init__(self, root: Path):
        self.root = root
        self.report_path = root / "report.json"
        self.policy_path = root / "policy.json"
        self.ev_dir = root / "evidence"

    def exists(self) -> bool:
        return self.report_path.exists()

    def load(self) -> dict:
        if not self.exists():
            sys.exit(f"error: no draft at {self.root} — run `init` first")
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def policy(self) -> dict:
        pol = json.loads(self.policy_path.read_text(encoding="utf-8"))
        ctx = self.root / "context.json"   # written by the workspace before each call: other reports' invoice numbers
        if ctx.exists():                   # (duplicate invoice rule) and the A-box project / charge-code / budget registry
            try:
                c = json.loads(ctx.read_text(encoding="utf-8"))
                pol["_ledger"], pol["_projects"] = c.get("ledger", {}), c.get("projects", {})
            except ValueError:
                pass
        return pol

    def save(self, r: dict) -> None:
        r["report"]["updated_at"] = now()
        tmp = self.report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.report_path)

    @staticmethod
    def log(r: dict, op: str, target: str | None = None, actor: str = "agent",
            source_ids=None, changes=None, note: str | None = None) -> None:
        ev = {"seq": len(r["trace"]["events"]) + 1, "at": now(), "actor": actor, "op": op}
        if target:
            ev["target"] = target
        if source_ids:
            ev["source_ids"] = list(source_ids)
        if changes:
            ev["changes"] = changes
        if note:
            ev["note"] = note
        r["trace"]["events"].append(ev)


def find(lst: list[dict], key: str, val: str) -> dict | None:
    return next((x for x in lst if x.get(key) == val), None)


def source_ids(r) -> list[str]:
    return [s["source_id"] for s in r["trace"]["sources"]]


# ───────────────────────── commands ─────────────────────────
def cmd_init(a):
    d = Draft(Path(a.draft))
    if d.exists() and not a.force:
        sys.exit(f"error: draft already exists at {d.root} (use --force to overwrite)")
    d.root.mkdir(parents=True, exist_ok=True)
    d.ev_dir.mkdir(exist_ok=True)
    policy = json.loads(Path(a.policy or DEFAULT_POLICY).read_text(encoding="utf-8"))
    d.policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    ts = now()
    header = {
        "report_id": f"RPT-{dt.date.today():%Y%m%d}-{secrets.token_hex(4)}",   # random: unique even within one second
        "title": a.title or "费用报销单",
        "status": "draft",
        "employee": {k: v for k, v in {"employee_id": a.employee_id, "name": a.employee,
                                       "department": a.department, "cost_center": a.cost_center}.items() if v},
        "currency": a.currency or policy["report_currency"],
        "policy_id": policy["policy_id"],
        "created_at": ts,
        "updated_at": ts,
    }
    if a.project:
        header["default_project"] = {"code": a.project, **({"name": a.project_name} if a.project_name else {})}
    if a.purpose:
        header["purpose"] = a.purpose
    if a.period_start and a.period_end:
        header["period"] = {"start": a.period_start, "end": a.period_end, "basis": "user"}
    r = {"schema_version": "1.0", "report": header, "items": [], "evidence": [], "gaps": [],
         "trace": {"sources": [], "events": []}}
    Draft.log(r, "init_report", header["report_id"], actor="user" if a.by_user else "agent")
    d.save(r)
    out({"report_id": header["report_id"], "draft": str(d.root)})


def cmd_set_header(a):
    d = Draft(Path(a.draft)); r = d.load()
    patch = load_json_arg(a.json)
    r["report"] = deep_merge(r["report"], patch)
    Draft.log(r, "update_item", r["report"]["report_id"], changes=patch, note="header")
    d.save(r); out(r["report"])


def cmd_add_source(a):
    d = Draft(Path(a.draft)); r = d.load()
    sid = next_id("SRC", source_ids(r))
    src = {"source_id": sid, "type": a.type, "received_at": now()}
    if a.ref:
        src["ref"] = a.ref
        p = Path(a.ref)
        if a.type == "file" and p.is_file():
            src["sha256"] = sha256_file(p)
            dup = next((s for s in r["trace"]["sources"] if s.get("sha256") == src["sha256"]), None)
            if dup:
                out({"source_id": dup["source_id"], "duplicate": True}); return
    if a.text:
        src["text"] = a.text
    r["trace"]["sources"].append(src)
    Draft.log(r, "add_source", sid, actor="user", source_ids=[sid])
    d.save(r); out({"source_id": sid})


def cmd_attach(a):
    d = Draft(Path(a.draft)); r = d.load()
    f = Path(a.file)
    if not f.is_file():
        sys.exit(f"error: file not found: {f}")
    digest = sha256_file(f)
    dup = find(r["evidence"], "sha256", digest)
    if dup:
        out({"evidence_id": dup["evidence_id"], "duplicate": True}); return
    src_id = a.source
    if not src_id:  # auto-register the file as a source
        src_id = next_id("SRC", source_ids(r))
        r["trace"]["sources"].append({"source_id": src_id, "type": "file", "ref": str(f),
                                      "sha256": digest, "received_at": now()})
        Draft.log(r, "add_source", src_id, actor="user", source_ids=[src_id])
    eid = next_id("EV", [e["evidence_id"] for e in r["evidence"]])
    dest = d.ev_dir / f"{eid}{f.suffix.lower()}"
    shutil.copy2(f, dest)
    ev = {"evidence_id": eid, "kind": a.kind, "path": f"evidence/{dest.name}", "original_name": f.name,
          "mime_type": mimetypes.guess_type(f.name)[0] or "application/octet-stream",
          "sha256": digest, "source_id": src_id, "item_ids": []}
    text_layer = None
    if f.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            rd = PdfReader(str(f)); ev["pages"] = len(rd.pages)
            text_layer = len("".join((pg.extract_text() or "") for pg in rd.pages[:2]).strip()) >= 30
        except Exception:
            pass
    ev["source_form"] = a.source_form or guess_source_form(f.suffix.lower(), text_layer)
    if a.extracted:
        ev["extracted"] = load_json_arg(a.extracted)
    r["evidence"].append(ev)
    Draft.log(r, "attach_evidence", eid, source_ids=[src_id], changes={"kind": a.kind, "path": ev["path"]})
    for iid in a.item or []:
        _link(r, iid, eid)
    d.save(r); out({"evidence_id": eid, "path": ev["path"], "source_id": src_id})


def guess_source_form(ext: str, text_layer: bool | None) -> str:
    """Ingestion routes (org pack): an issuer's original e-invoice file (ofd/xml, or a PDF with a text layer) vs a scan/photo.
    A text-layer PDF is only presumed original — the agent overrides with --source-form when it knows better."""
    if ext in (".ofd", ".xml"):
        return "original_electronic"
    if ext == ".pdf":
        return {True: "original_electronic", False: "non_source_pdf"}.get(text_layer, "unknown")
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tif", ".tiff"):
        return "photo"
    return "unknown"


def cmd_set_evidence(a):
    d = Draft(Path(a.draft)); r = d.load()
    ev = find(r["evidence"], "evidence_id", a.evidence)
    if not ev:
        sys.exit(f"error: unknown evidence {a.evidence}")
    changes = {}
    if a.source_form:
        changes["source_form"] = a.source_form
    if a.kind:
        changes["kind"] = a.kind
    ev.update(changes)
    Draft.log(r, "update_evidence", a.evidence, changes=changes)
    d.save(r); out({"evidence_id": a.evidence, **changes})


def _link(r, iid, eid):
    it = find(r["items"], "item_id", iid); ev = find(r["evidence"], "evidence_id", eid)
    if not it or not ev:
        sys.exit(f"error: unknown item {iid} or evidence {eid}")
    if eid not in it["evidence_ids"]:
        it["evidence_ids"].append(eid)
    ev.setdefault("item_ids", [])
    if iid not in ev["item_ids"]:
        ev["item_ids"].append(iid)
    Draft.log(r, "link_evidence", iid, changes={"evidence_id": eid})


def cmd_link(a):
    d = Draft(Path(a.draft)); r = d.load()
    for eid in a.evidence:
        _link(r, a.item, eid)
    d.save(r); out({"item_id": a.item, "evidence_ids": find(r["items"], "item_id", a.item)["evidence_ids"]})


def cmd_unlink(a):
    d = Draft(Path(a.draft)); r = d.load()
    it = find(r["items"], "item_id", a.item); ev = find(r["evidence"], "evidence_id", a.evidence)
    if it and a.evidence in it["evidence_ids"]:
        it["evidence_ids"].remove(a.evidence)
    if ev and a.item in ev.get("item_ids", []):
        ev["item_ids"].remove(a.item)
    Draft.log(r, "unlink_evidence", a.item, changes={"evidence_id": a.evidence})
    d.save(r); out({"ok": True})


def cmd_upsert(a):
    """Create or patch an item. Missing provenance for any provided field is
    filled from --source with --method, so every value stays traceable."""
    d = Draft(Path(a.draft)); r = d.load()
    patch = load_json_arg(a.json)
    if a.source and a.source not in source_ids(r):
        sys.exit(f"error: unknown source {a.source}")
    iid = patch.pop("item_id", None) or a.item
    existing = find(r["items"], "item_id", iid) if iid else None
    prov_patch = patch.pop("provenance", {}) or {}
    if a.source:
        for field in _leaf_fields(patch):
            prov_patch.setdefault(field, {"source_id": a.source, "method": a.method,
                                          **({"confidence": a.confidence} if a.confidence is not None else {})})
    if existing:
        merged = deep_merge(existing, patch)
        merged["provenance"] = {**existing.get("provenance", {}), **prov_patch}
        r["items"][r["items"].index(existing)] = merged
        Draft.log(r, "update_item", iid, source_ids=[a.source] if a.source else None, changes=patch)
        item = merged
    else:
        iid = next_id("ITM", [i["item_id"] for i in r["items"]])
        item = {"item_id": iid, "status": "incomplete", "evidence_ids": [], **patch, "provenance": prov_patch}
        if "amount" in item and "currency" not in item["amount"]:
            item["amount"]["currency"] = r["report"]["currency"]
        _derive(item)
        r["items"].append(item)
        Draft.log(r, "create_item", iid, source_ids=[a.source] if a.source else None, changes=patch)
    if isinstance(item.get("amount"), dict) and "currency" not in item["amount"]:   # a patched amount keeps a currency
        item["amount"]["currency"] = ((existing or {}).get("amount") or {}).get("currency") or r["report"]["currency"]
    _derive(item)
    for eid in a.evidence or []:
        _link(r, iid, eid)
    d.save(r); out({"item_id": iid})


def _leaf_fields(patch: dict) -> list[str]:
    fields = []
    for k, v in patch.items():
        if k in ("evidence_ids", "status", "notes"):
            continue
        if isinstance(v, dict) and k not in ("amount",):
            fields += [f"{k}.{kk}" for kk in v]
        else:
            fields.append(k)
    return fields


def _derive(item: dict) -> None:
    t = item.get("trip") or {}
    if item.get("category") == "lodging" and t.get("check_in") and t.get("check_out") and not t.get("nights"):
        n = (dt.date.fromisoformat(t["check_out"]) - dt.date.fromisoformat(t["check_in"])).days
        if n >= 1:
            t["nights"] = n
            src = (item.get("provenance") or {}).get("trip.check_out")
            if src:
                item["provenance"]["trip.nights"] = {**src, "method": "inferred", "locator": "check_out-check_in"}
    if item.get("category") == "lodging" and t.get("check_in") and not item.get("expense_date"):
        item["expense_date"] = t["check_in"]
        src = (item.get("provenance") or {}).get("trip.check_in")
        if src:
            item["provenance"]["expense_date"] = {**src, "method": "inferred", "locator": "trip.check_in"}


def cmd_delete(a):
    d = Draft(Path(a.draft)); r = d.load()
    it = find(r["items"], "item_id", a.item)
    if not it:
        sys.exit(f"error: unknown item {a.item}")
    r["items"].remove(it)
    for ev in r["evidence"]:
        if a.item in ev.get("item_ids", []):
            ev["item_ids"].remove(a.item)
    Draft.log(r, "delete_item", a.item, actor="user" if a.by_user else "agent", note=a.reason)
    d.save(r); out({"deleted": a.item})


def cmd_merge(a):
    """Merge duplicate items: keep --keep, fold evidence of --drop into it."""
    d = Draft(Path(a.draft)); r = d.load()
    keep = find(r["items"], "item_id", a.keep)
    for did in a.drop:
        dr = find(r["items"], "item_id", did)
        if not dr:
            continue
        for eid in dr["evidence_ids"]:
            _link(r, a.keep, eid)
        for k, v in dr.get("provenance", {}).items():
            keep["provenance"].setdefault(k, v)
        r["items"].remove(dr)
        for ev in r["evidence"]:
            if did in ev.get("item_ids", []):
                ev["item_ids"].remove(did)
    Draft.log(r, "merge_items", a.keep, changes={"merged": a.drop})
    d.save(r); out({"item_id": a.keep, "merged": a.drop})


# ───────────────────────── gap engine ─────────────────────────
def compute(r: dict, policy: dict) -> dict:
    cur = r["report"]["currency"]
    cats = policy.get("categories", {})
    gaps: list[dict] = []

    def gap(type_, severity, message, item_id=None, requirement=None, action=None):
        g = {"gap_id": f"GAP-{len(gaps) + 1:04d}", "type": type_, "severity": severity, "message": message}
        if item_id: g["item_id"] = item_id
        if requirement: g["requirement"] = requirement
        if action: g["action"] = action
        gaps.append(g)

    hdr = r["report"]
    rs_hdr, rs_pol = hdr.get("rule_set") or {"id": "standard"}, policy.get("rule_set") or {"id": "standard"}
    if rs_hdr["id"] != rs_pol["id"]:
        gap("rule_set", "warning", f"报销单规则集为 {rs_hdr['id']}，但当前校验使用 {rs_pol['id']} —— 请重新同步规则",
            requirement="rule_set", action="运行 rules sync")
    if rs_hdr.get("basis") == "exception":
        need = (policy.get("exception_policy") or {}).get("approval_evidence", "approval")
        if not any(e["kind"] == need for e in r["evidence"]):
            gap("rule_set", "blocker",
                f"按例外使用「{rs_hdr.get('label', rs_hdr['id'])}」（审批依据 {(rs_hdr.get('exception') or {}).get('approval_ref', '—')}），"
                f"需上传审批凭证", requirement=f"evidence:rule_set_approval", action=f"上传 {need} 类凭证（审批单截图/PDF）")
    if policy.get("require_purpose", True) and not hdr.get("purpose"):
        gap("report_field", "blocker", "报销单缺少出差/费用事由", requirement="field:report.purpose",
            action="补充一句本次出差或费用的业务目的")
    if not hdr["employee"].get("employee_id"):
        gap("report_field", "warning", "未填写员工工号", requirement="field:report.employee.employee_id")

    ev_by_id = {e["evidence_id"]: e for e in r["evidence"]}
    jd = judged(r, ev_by_id)
    policy["_judged_classes"] = {rec["evidence_id"]: rec["decision"] for (_, h), rec in jd.items()
                                 if h == "credential_class" and rec.get("band") == "auto" and rec.get("evidence_id")}
    today = dt.date.today()
    seen_inv, seen_key = {}, {}
    totals, by_cat, by_proj, by_acc, by_bud, by_cc = 0.0, {}, {}, {}, {}, {}

    for it in r["items"]:
        iid, cat = it["item_id"], it.get("category")
        rule = cats.get(cat, {})
        label = rule.get("label", cat)
        blockers_before = sum(1 for g in gaps if g["severity"] == "blocker")
        if it.get("status") == "excluded":
            continue

        # required core fields
        for f in ["category", "expense_date", "amount", "description"]:
            if get_path(it, f) is None:
                gap("missing_field", "blocker", f"{iid} 缺少 {f}", iid, f"field:{f}")
        for f in rule.get("required_fields", []):
            if get_path(it, f) is None:
                gap("missing_field", "blocker", f"{iid}（{label}）缺少 {f}", iid, f"field:{f}",
                    f"请补充 {f}")
        if policy.get("require_project", True) and not (it.get("project") or hdr.get("default_project")):
            gap("missing_field", "blocker", f"{iid} 未归属项目", iid, "field:project", "说明这笔费用属于哪个项目")

        # evidence
        kinds = {ev_by_id[e]["kind"] for e in it["evidence_ids"] if e in ev_by_id}
        for group in rule.get("required_evidence", []):
            if not kinds & set(group):
                gap("missing_evidence", "blocker",
                    f"{iid}（{label} {it.get('description', '')}）缺少凭证：{' 或 '.join(group)}",
                    iid, f"evidence:any_of[{','.join(group)}]", f"请提供 {' / '.join(group)} 之一")
        for group in rule.get("recommended_evidence", []):
            if not kinds & set(group):
                gap("missing_evidence", "info", f"{iid} 建议补充：{' 或 '.join(group)}", iid,
                    f"evidence:any_of[{','.join(group)}]")

        # money / fx / caps
        amt = it.get("amount")
        reimb = None
        if amt:
            if amt["currency"] != cur and not it.get("fx_rate"):
                gap("fx_missing", "blocker", f"{iid} 外币 {amt['currency']} 缺少汇率", iid, "field:fx_rate")
            else:
                reimb = round(amt["value"] * (it.get("fx_rate") or 1.0), 2)
                cap = rule.get("cap")
                if cap:
                    t = it.get("trip") or {}
                    limit = cap["amount"]
                    for city, v in (cap.get("by_city") or {}).items():
                        if city and city in (t.get("city") or ""):
                            limit = v
                    units = {"item": 1, "night": t.get("nights") or 1, "day": t.get("nights") or 1,
                             "person": max(1, len(it.get("attendees") or []))}[cap["per"]]
                    ceiling = round(limit * units, 2)
                    if reimb > ceiling:
                        if cap.get("mode") == "clip":
                            gap("policy_limit", "warning",
                                f"{iid} 超标：{reimb} > {limit}/{cap['per']}×{units}，按 {ceiling} 报销",
                                iid, f"cap:{limit}/{cap['per']}")
                            reimb = ceiling
                        else:
                            gap("policy_limit", "warning", f"{iid} 超出标准 {limit}/{cap['per']}（{reimb}）",
                                iid, f"cap:{limit}/{cap['per']}", "请补充超标说明或审批")
            it["amount_reimbursable"] = reimb if reimb is not None else 0

        # period / window
        d_ = it.get("expense_date")
        if d_:
            per = hdr.get("period")
            if per and per.get("basis") == "user" and not (per["start"] <= d_ <= per["end"]):
                gap("out_of_period", "warning", f"{iid} 日期 {d_} 不在报销期间 {per['start']}~{per['end']}", iid)
            win = policy.get("submission_window_days")
            if win and (today - dt.date.fromisoformat(d_)).days > win:
                gap("out_of_period", "warning", f"{iid} 已超过 {win} 天报销时限", iid)

        # 发票抬头: buyer on the invoice must be the company (checked when the buyer was read from the invoice)
        title = policy.get("invoice_title")
        inv_d = it.get("invoice") or {}
        # scope: Chinese 发票 only — listed invoice types, in the title's currency (CNY); foreign invoices are never checked
        cn_types = title.get("invoice_types") or ["vat_special", "vat_general", "e_invoice", "rail_e_ticket"] if title else []
        in_scope = bool(title) and inv_d.get("invoice_type") in cn_types and \
            (it.get("amount") or {}).get("currency", cur) == title.get("currency", "CNY")
        if in_scope:
            norm_s = lambda s: re.sub(r"[\s()（）]", "", s or "").upper()
            bn, bt = inv_d.get("buyer_name"), inv_d.get("buyer_tax_id")
            jb = jd.get((iid, "buyer_title"))
            allowed_ids = {norm_s(x["tax_id"]) for x in policy.get("invoice_titles", [])} | {norm_s(title["tax_id"])}
            allowed_names = {norm_s(x["name"]) for x in policy.get("invoice_titles", [])} | {norm_s(title["name"])}
            if bt and norm_s(bt) in allowed_ids:
                pass                                          # tax id matches a registered 抬头 — decided
            elif bn and not bt and norm_s(bn) not in allowed_names and jb and jb["band"] == "auto" and jb["decision"] is True:
                gap("judgment", "info", f"{iid} 购买方「{bn}」与抬头文字不同，Jev 判断为同一主体（置信 {jb['confidence']}）", iid,
                    "judgment:J-BUYER-TITLE")
            elif (bn and norm_s(bn) not in allowed_names) or (bt and norm_s(bt) not in allowed_ids):
                gap("invoice_title_mismatch", "blocker",
                    f"{iid} 发票抬头不符：{bn or '—'} / {bt or '—'}（应为 {title['name']} / {title['tax_id']}）", iid,
                    "field:invoice.buyer", "请商家按公司抬头重新开票" + (
                        f"（Jev：可能为同一主体，置信 {jb['confidence']}，请人工核对）" if jb and jb["decision"] is True else ""))
            elif not bn and not bt:
                gap("invoice_title_mismatch", "info", f"{iid} 未读取到发票购买方，请核对抬头是否为 {title['name']}", iid,
                    "field:invoice.buyer_name")

        judgment_effects(it, iid, jd, policy, gap)
        receipt_checks(it, iid, policy, cur, ev_by_id, gap)
        credential_checks(it, iid, policy, cur, ev_by_id, gap)
        resolve_accounting(it, iid, hdr, policy, gap)

        # duplicates
        inv = (it.get("invoice") or {}).get("invoice_no")
        if inv:
            if inv in seen_inv:
                gap("duplicate_suspect", "blocker", f"{iid} 与 {seen_inv[inv]} 发票号重复（{inv}）", iid,
                    action="确认后用 merge 或 delete 处理")
            seen_inv.setdefault(inv, iid)
        if amt and d_:
            key = (cat, d_, amt["value"])
            if key in seen_key:
                gap("duplicate_suspect", "warning", f"{iid} 与 {seen_key[key]} 类别/日期/金额相同，疑似重复", iid)
            seen_key.setdefault(key, iid)

        # item status
        item_blockers = sum(1 for g in gaps if g["severity"] == "blocker") - blockers_before
        if it.get("status") != "flagged":
            it["status"] = "incomplete" if item_blockers else "complete"

        if reimb is not None:
            totals += reimb
            by_cat[cat] = round(by_cat.get(cat, 0) + reimb, 2)
            acc = it.get("accounting") or {}
            pcode = acc.get("project_code") or "UNASSIGNED"
            by_proj[pcode] = round(by_proj.get(pcode, 0) + reimb, 2)
            for key, bucket in (("account_code", by_acc), ("budget_id", by_bud), ("charge_code", by_cc)):
                k = acc.get(key) or "UNASSIGNED"
                bucket[k] = round(bucket.get(k, 0) + reimb, 2)

    for ev in r["evidence"]:
        if not ev.get("item_ids"):
            gap("unlinked_evidence", "warning", f"凭证 {ev['evidence_id']}（{ev.get('original_name')}）未关联任何报销条目",
                requirement=f"evidence:{ev['evidence_id']}", action="关联到条目或确认无需报销")

    r["gaps"] = gaps
    wf = build_workflow(r, policy)
    if wf:
        r["workflow"] = wf
    else:
        r.pop("workflow", None)
    active = [i for i in r["items"] if i.get("status") != "excluded"]
    hdr["totals"] = {"grand_total": round(totals, 2), "item_count": len(active),
                     "by_category": by_cat, "by_project": by_proj,
                     **({"by_account": by_acc} if policy.get("coa") else {}),
                     **({"by_budget": by_bud} if any(k != "UNASSIGNED" for k in by_bud) else {}),
                     **({"by_charge_code": by_cc} if any(k != "UNASSIGNED" for k in by_cc) else {})}
    dates = sorted([i["expense_date"] for i in active if i.get("expense_date")] +
                   [(i.get("trip") or {})["check_out"] for i in active if (i.get("trip") or {}).get("check_out")])  # stays run to check-out
    if dates and (hdr.get("period") or {}).get("basis") != "user":
        hdr["period"] = {"start": dates[0], "end": dates[-1], "basis": "derived"}
    if hdr["status"] != "submitted":
        hdr["status"] = "draft" if not active else (
            "incomplete" if any(g["severity"] == "blocker" for g in gaps) else "ready")
    return r


# ───────────────────────── company receipt / invoice rules ─────────────────────────
VERIFY_LABEL = {"invoice_no": "发票号码", "invoice_code": "发票代码", "issue_date": "开票日期", "amount": "价税合计",
                "amount_excl_tax": "金额（不含税）", "check_code": "校验码"}
STATUS_LABEL = {"unreported": "未报账", "partially_reported": "部分已报账", "reported": "已报账",
                "saved": "已保存", "failed": "保存失败", "not_collected": "未采集"}
FIELD_LABEL = {"save_status": "发票系统保存状态", "verify_result": "查验结果", "reimb_status": "报账状态"}


# ───────────────────────── financial credentials (票据) ─────────────────────────
SUPPORTING_KINDS = {"payment_proof", "approval", "agenda", "boarding_pass", "other", "hotel_folio", "card_slip"}


def kind_class(policy: dict, ev: dict) -> str | None:
    if (ev.get("credential") or {}).get("credential_class"):
        return ev["credential"]["credential_class"]
    if ev["evidence_id"] in (policy.get("_judged_classes") or {}):      # confident Jev classification (J-CREDENTIAL-CLASS)
        return policy["_judged_classes"][ev["evidence_id"]]
    for c, spec in ((policy.get("credentials") or {}).get("classes") or {}).items():
        if ev["kind"] in spec.get("kinds", []):
            return c
    return {"e_invoice": "exp:DigitalInvoice", "invoice": "exp:VATOrdinaryInvoice"}.get(ev["kind"])


def is_overseas(it: dict, ev_by_id: dict, policy: dict, cur: str) -> bool:
    """Overseas expense = foreign currency, a trip country other than CN, or a credential issued outside China."""
    if (it.get("amount") or {}).get("currency", cur) != "CNY" or (it.get("trip") or {}).get("country") not in (None, "CN"):
        return True
    if (it.get("invoice") or {}).get("invoice_type") == "foreign_invoice":
        return True
    classes = (policy.get("credentials") or {}).get("classes") or {}
    for e in it.get("evidence_ids", []):
        ev = ev_by_id.get(e)
        if not ev:
            continue
        j = (ev.get("credential") or {}).get("jurisdiction") or classes.get(kind_class(policy, ev) or "", {}).get("jurisdiction")
        if j and j != "CN":
            return True
    return False


def _get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def credential_checks(it: dict, iid: str, policy: dict, cur: str, ev_by_id: dict, gap) -> None:
    cr = policy.get("credentials") or {}
    if not cr:
        return
    evs = [ev_by_id[e] for e in it.get("evidence_ids", []) if e in ev_by_id]
    primary = [(ev, kind_class(policy, ev)) for ev in evs if ev["kind"] not in SUPPORTING_KINDS]
    overseas = is_overseas(it, ev_by_id, policy, cur)
    acc = cr.get("acceptance")
    if acc and primary:
        side = acc["overseas" if overseas else "domestic"]
        ok = [c for _, c in primary if c in side["classes"]]
        if not ok:
            labels = "、".join(sorted({(cr["classes"].get(c) or {}).get("label", c or "未识别") for _, c in primary}))
            gap("credential_acceptance", side.get("severity", "warning"),
                f"{iid} {'境外' if overseas else '境内'}费用的票据类型（{labels}）不在公司可接受范围", iid, "credential:acceptance",
                side.get("text"))
        if overseas and side.get("recommended") and not {ev["kind"] for ev in evs} & set(side["recommended"]):
            gap("credential_acceptance", "info", f"{iid} 境外费用建议附银行卡账单 / 刷卡签购单", iid, "credential:acceptance")
    # structure + consistency of the credentials actually recorded
    creds = [(ev, ev["credential"]) for ev in evs if ev.get("credential")]
    primary_creds = [(ev, c) for ev, c in creds if ev["kind"] not in SUPPORTING_KINDS]   # a folio / card slip never adds to the face total
    for ev, c in creds:
        cls = c["credential_class"]
        for r in cr.get("rules", []):
            if r["kind"] != "required_elements" or cls not in r["applies_to"].get("credential_class", []):
                continue
            miss = [p for p in r["elements"] if _get(c, p) in (None, "", [])]
            if miss:
                gap("credential_schema", r.get("severity", "warning"),
                    f"{iid} 票据 {ev['evidence_id']} 缺少要素：{', '.join(miss)}（{r['id']}）", iid, f"credential:{r['id']}",
                    "从票面 / XML 补录")
        tot = c.get("totals") or {}
        rs = {r["check"]: r for r in cr.get("rules", []) if r["kind"] == "consistency"}
        r = rs.get("excl_plus_tax_eq_total")
        if r and None not in (tot.get("amount_excl_tax"), tot.get("tax_amount"), tot.get("total_amount")) \
                and abs(tot["amount_excl_tax"] + tot["tax_amount"] - tot["total_amount"]) > r.get("tolerance", 0.01):
            gap("credential_consistency", r["severity"], f"{iid} 票据 {ev['evidence_id']} 金额+税额≠价税合计", iid,
                f"credential:{r['id']}", "核对票面金额（识别错误或录入错误）")
        r, lines = rs.get("lines_sum_eq_excl"), c.get("line_items") or []
        if r and lines:
            for key, tk in (("amount", "amount_excl_tax"), ("tax_amount", "tax_amount")):
                vals = [li.get(key) for li in lines]
                if tot.get(tk) is not None and None not in vals and abs(sum(vals) - tot[tk]) > r.get("tolerance", 0.05):
                    gap("credential_consistency", r["severity"], f"{iid} 票据 {ev['evidence_id']} 明细合计与{'金额' if key == 'amount' else '税额'}不符",
                        iid, f"credential:{r['id']}")
        r = rs.get("invoice_number_matches_item")
        no_i, no_c = (it.get("invoice") or {}).get("invoice_no"), (c.get("identity") or {}).get("invoice_number")
        if r and no_i and no_c and no_i != no_c:
            gap("credential_consistency", r["severity"], f"{iid} 发票号 {no_i} 与票面 {no_c} 不一致", iid, f"credential:{r['id']}")
    r = next((x for x in cr.get("rules", []) if x.get("check") == "claim_le_credential_total"), None)
    amt = it.get("amount") or {}
    if r and primary_creds and amt.get("value") is not None:
        best = {}                                   # one credential per invoice number — the most trusted representation
        for ev, c in primary_creds:
            key = (c.get("identity") or {}).get("invoice_number") or ev["evidence_id"]
            rank = (c.get("source") or {}).get("trust_rank", 1)
            if key not in best or rank > best[key][0]:
                best[key] = (rank, c)
        same = [c for _, c in best.values() if (c.get("totals") or {}).get("total_amount") is not None
                and (c["totals"].get("currency") or ("CNY" if c.get("jurisdiction") == "CN" else amt.get("currency"))) == amt.get("currency", cur)]
        if same:
            face = round(sum(c["totals"]["total_amount"] for c in same), 2)
            if amt["value"] - face > r.get("tolerance", 0.01):
                gap("credential_consistency", r["severity"], f"{iid} 报销金额 {amt['value']} 超过票面合计 {face}", iid,
                    f"credential:{r['id']}", "按票面金额报销，或补充票据")
            elif face - amt["value"] > r.get("tolerance", 0.01):
                gap("credential_consistency", "info", f"{iid} 部分报销：{amt['value']} / 票面 {face}", iid, f"credential:{r['id']}")


# 数电票 national XML (EInvoice root, as issued by the 税务数字账户 / 电子发票服务平台). Tag names from real files.
_XML_PARTY = {"IdNum": "taxpayer_id", "Name": "name", "Addr": "address", "TelNum": "phone", "BankName": "bank_name",
              "BankAccNum": "bank_account"}
_XML_LINE = {"ItemName": "item_name", "SpecMod": "specification", "MeaUnits": "unit", "Quantity": "quantity",
             "UnPrice": "unit_price", "Amount": "amount", "TaxRate": "tax_rate", "ComTaxAm": "tax_amount"}


def parse_einvoice_xml(path: Path) -> dict:
    """Parse a 数电票 XML file into a FinancialCredential. Tolerant of namespaces and missing optional blocks;
    scenario-specific blocks (SpecificInformation) are kept as raw element → text pairs."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    local = lambda e: e.tag.rsplit("}", 1)[-1]
    if local(root) != "EInvoice":
        raise ValueError(f"not a 数电票 XML (root <{local(root)}>)")
    def first(node, name):
        return next((e for e in node.iter() if local(e) == name), None)
    def text(node, name):
        e = first(node, name) if node is not None else None
        return (e.text or "").strip() if e is not None and e.text and e.text.strip() else None
    num = lambda v: float(v) if v not in (None, "") else None
    data = first(root, "EInvoiceData") or root
    c = {"credential_class": "exp:DigitalInvoice", "jurisdiction": "CN"}
    labels = [text(first(root, n), "LabelName") for n in ("EInvoiceType", "GeneralOrSpecialVAT")]
    ident = {"invoice_number": text(first(root, "TaxSupervisionInfo"), "InvoiceNumber") or text(root, "EIid"),
             "issue_date": (text(first(root, "TaxSupervisionInfo"), "IssueTime") or text(data, "RequestTime") or "")[:10] or None,
             "type_label": "（".join(x for x in labels if x) + ("）" if all(labels) else "") if any(labels) else None}
    c["identity"] = {k: v for k, v in ident.items() if v}
    for side, prefix in (("seller", "Seller"), ("buyer", "Buyer")):
        blk = first(data, f"{prefix}Information")
        if blk is not None:
            party = {v: text(blk, prefix + k) for k, v in _XML_PARTY.items()}
            c[side] = {k: v for k, v in party.items() if v}
    basic = first(data, "BasicInformation")
    if basic is not None:
        tot = {"amount_excl_tax": num(text(basic, "TotalAmWithoutTax")), "tax_amount": num(text(basic, "TotalTaxAm")),
               "total_amount": num(text(basic, "TotalTax-includedAmount")),
               "total_amount_words": text(basic, "TotalTax-includedAmountInChinese"), "currency": "CNY"}
        c["totals"] = {k: v for k, v in tot.items() if v is not None}
        if text(basic, "Drawer"):
            c["issuer_name"] = text(basic, "Drawer")
    lines = []
    for li in (e for e in data.iter() if local(e) == "IssuItemInformation"):
        row = {v: (num(text(li, k)) if v not in ("item_name", "specification", "unit") else text(li, k)) for k, v in _XML_LINE.items()}
        lines.append({k: v for k, v in row.items() if v is not None})
    if lines:
        c["line_items"] = lines
    spec = first(data, "SpecificInformation")
    if spec is not None and len(spec):
        c["special"] = {"elements": {local(e): e.text.strip() for e in spec.iter() if e is not spec and e.text and e.text.strip()}}
    if text(data, "Remark"):
        c["remarks"] = text(data, "Remark")
    c["tax"] = {"tax_scheme": "vat_cn"}
    c["source"] = {"format": "xml", "extraction_method": "xml",
                   "digital_signature": "present_unverified" if first(root, "SignatureValue") is not None else "absent"}
    return c


def item_sha(it: dict, ev_by_id: dict) -> str:
    """Fingerprint of what a judgment looked at: the item (minus derived fields) and its evidence files."""
    core = {k: v for k, v in it.items() if k not in ("status", "amount_reimbursable", "accounting", "provenance")}
    evs = sorted((e, ev_by_id[e]["sha256"], json.dumps(ev_by_id[e].get("credential") or {}, sort_keys=True, ensure_ascii=False))
                 for e in it.get("evidence_ids", []) if e in ev_by_id)
    return hashlib.sha256(json.dumps([core, evs], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def cmd_judge(a):
    """Ask TypeSafe Jev the R-box judgments code cannot decide (core R-box › judgments); store calibrated answers."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import jev_rbox
    d = Draft(Path(a.draft)); r = d.load(); policy = d.policy()
    if not policy.get("judgments"):
        out({"judged": 0, "note": "no judgments in the compiled policy"}); return
    res = jev_rbox.judge(r, policy, d.root, d.root / "jev_cache.json", model=a.model, force=a.force)
    ev_by_id = {e["evidence_id"]: e for e in r["evidence"]}
    gates = {j["id"]: j.get("gate", {"auto": 0.85, "floor": 0.6}) for j in policy["judgments"]}
    items = {i["item_id"]: i for i in r["items"]}
    for rec in res["records"]:
        rec["item_sha"] = item_sha(items[rec["item_id"]], ev_by_id)
        if rec.get("answer"):
            band, decision, conf = jev_rbox.gate_of(rec["answer"], gates[rec["judgment_id"]])
            rec.update(band=band, decision=decision, confidence=conf)
    r["judgments"] = {"model": res["model"], "run_at": now(), "records": res["records"],
                      **{k: res[k] for k in ("calls", "wall_ms", "input_tokens", "unavailable") if k in res}}
    Draft.log(r, "judge", r["report"]["report_id"], changes={"records": len(res["records"]), "calls": res.get("calls", 0),
                                                              "model": res["model"]})
    compute(r, policy)
    d.save(r)
    out({"judged": len(res["records"]), "calls": res.get("calls", 0), "wall_ms": res.get("wall_ms"),
         "api_ms": res.get("api_ms"), "input_tokens": res.get("input_tokens"), "model": res["model"],
         **({"unavailable": res["unavailable"]} if res.get("unavailable") else {}),
         "decisions": [{k: x.get(k) for k in ("item_id", "judgment_id", "decision", "confidence", "band", "cached")}
                       for x in res["records"] if x.get("answer")]})


# tax-classification keywords on 发票 line items → category (deterministic fallback when Jev is not confident / not configured)
CATEGORY_KEYWORDS = [("住宿", "lodging"), ("房费", "lodging"), ("餐饮", "meals"), ("餐费", "meals"), ("铁路", "transport.rail"),
                     ("航空", "transport.air"), ("机票", "transport.air"), ("客运", "transport.taxi"), ("出租", "transport.taxi"),
                     ("通行费", "transport.fuel_toll"), ("汽油", "transport.fuel_toll"), ("会展", "conference"), ("会议", "conference"),
                     ("培训", "conference"), ("电信", "communication"), ("通信", "communication"), ("办公", "office_supplies"),
                     ("纸", "office_supplies")]


def cmd_ingest_xml(a):
    """One call from a parsed 数电票 credential to a complete item: amount, invoice fields, merchant, category.
    Category: Jev (J-CATEGORY, auto band) → keyword map over the tax classification → left for the agent to ask."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    d = Draft(Path(a.draft)); r = d.load(); policy = d.policy()
    ev = find(r["evidence"], "evidence_id", a.evidence)
    c = (ev or {}).get("credential")
    if not c:
        sys.exit("error: evidence has no credential — run `credential --from-xml` first")
    ident, tot, seller, buyer = c.get("identity") or {}, c.get("totals") or {}, c.get("seller") or {}, c.get("buyer") or {}
    lines = [li.get("item_name") for li in c.get("line_items", []) if li.get("item_name")]
    clean = lambda s: re.sub(r"^\*[^*]*\*", "", s or "")
    cat, how, conf = None, None, None
    jcat = next((j for j in policy.get("judgments") or [] if j["handler"] == "category"), None)
    if jcat:
        import jev_rbox
        ok, _ = jev_rbox.available()
        if ok:
            state = {"line_items": lines, "seller": seller.get("name"), "type_label": ident.get("type_label")}
            import asyncio
            try:
                res = asyncio.run(jev_rbox._ask_all([("x", state, {"category": jev_rbox.question(jcat, policy)})], a.model))
            except Exception as ex:                                # fall back to the keyword map
                res = {"x": {"answers": {}, "model": None}}
                how = f"jev unavailable ({type(ex).__name__})"
            ans = res["x"]["answers"].get("category")
            if ans:
                band, decision, conf = jev_rbox.gate_of(ans, jcat["gate"])
                if band == "auto":
                    cat, how = decision, f"jev:{res['x']['model']}"
    if not cat:
        blob = " ".join(lines) + " " + (seller.get("name") or "")
        cat = next((c_ for k, c_ in CATEGORY_KEYWORDS if k in blob), None)
        how = "keywords" if cat else how
        if not cat and "酒店" in blob:
            cat, how = "lodging", "keywords"
    item = {"description": " / ".join(clean(x) for x in lines[:2]) or ident.get("type_label") or "发票",
            "merchant": seller.get("name"),
            "amount": {"value": tot.get("total_amount"), "currency": tot.get("currency", "CNY")},
            "invoice": {k: v for k, v in {
                "invoice_type": "e_invoice" if c["credential_class"] == "exp:DigitalInvoice" else None,
                "invoice_no": ident.get("invoice_number"), "issue_date": ident.get("issue_date"),
                "seller_name": seller.get("name"), "buyer_name": buyer.get("name"), "buyer_tax_id": buyer.get("taxpayer_id"),
                "tax_amount": tot.get("tax_amount"), "amount_excl_tax": tot.get("amount_excl_tax"),
                "line_items": [{k2: v2 for k2, v2 in {"name": li.get("item_name"), "amount": li.get("amount"), "tax_rate": li.get("tax_rate"),
                                                     "tax_amount": li.get("tax_amount")}.items() if v2 is not None} for li in c.get("line_items", [])]
            }.items() if v not in (None, [], "")}}
    if cat:
        item["category"] = cat
        if cat != "lodging" and ident.get("issue_date"):
            item["expense_date"] = ident["issue_date"]            # lodging dates come from the stay, not the issue date
    item = {k: v for k, v in item.items() if v not in (None, "", {})}
    iid = next_id("ITM", [i["item_id"] for i in r["items"]])
    m_ = (c.get("source") or {}).get("extraction_method", "xml")
    prov = {f: {"source_id": ev["source_id"], "method": m_ if m_ in ("xml", "ofd_structured") else "pdf_text",
                "confidence": 0.99 if m_ == "xml" else 0.97} for f in _leaf_fields(item)}
    if cat:
        prov["category"] = {"source_id": ev["source_id"], "method": "inferred" if how != "keywords" else "inferred",
                            "confidence": conf if conf is not None else 0.8}
    new = {"item_id": iid, "status": "incomplete", "evidence_ids": [], **item, "provenance": prov}
    _derive(new)
    r["items"].append(new)
    Draft.log(r, "create_item", iid, source_ids=[ev["source_id"]], changes=item, note=f"ingest-xml; category via {how or 'unknown'}")
    _link(r, iid, ev["evidence_id"])
    compute(r, policy)
    d.save(r)
    out({"item_id": iid, "category": cat, "category_by": how, "category_confidence": conf, "amount": tot.get("total_amount"),
         "needs": [g["message"] for g in r["gaps"] if g.get("item_id") == iid and g["severity"] == "blocker"]})


def judged(r: dict, ev_by_id: dict) -> dict:
    """Current (non-stale) judgment records keyed by (item_id, judgment handler)."""
    items = {i["item_id"]: i for i in r["items"]}
    outp = {}
    for rec in (r.get("judgments") or {}).get("records", []):
        it = items.get(rec["item_id"])
        if it and rec.get("answer") and rec.get("item_sha") == item_sha(it, ev_by_id):
            outp[(rec["item_id"], rec["handler"])] = rec
    return outp


def parse_einvoice_ofd(path: Path) -> dict:
    """Parse a 数电票 OFD (GB/T 33190 container). Invoice fields come from the semantic tags (Doc_*/Tags/CustomTag.xml,
    each naming the page text objects that carry a value); OFD.xml › CustomDatas (发票号码, 合计金额, 合计税额, 开票日期,
    纳税人识别号) cross-checks them. Anything inconsistent is reported under source.ofd_mismatch."""
    import xml.etree.ElementTree as ET
    import zipfile as zf_
    z = zf_.ZipFile(path)
    names = z.namelist()
    local = lambda e: e.tag.rsplit("}", 1)[-1]
    root_ofd = ET.fromstring(z.read("OFD.xml"))
    custom = {e.get("Name"): (e.text or "").strip() for e in root_ofd.iter() if local(e) == "CustomData"}
    if not any(k in custom for k in ("发票号码", "合计金额")) and not any(n.endswith("CustomTag.xml") for n in names):
        raise ValueError("not a 数电票 OFD (no invoice tags)")
    texts = {}                                           # text object id → text, across pages and templates
    for n in names:
        if n.endswith("Content.xml"):
            for obj in ET.fromstring(z.read(n)).iter():
                if local(obj) == "TextObject" and obj.get("ID"):
                    texts[obj.get("ID")] = "".join((tc.text or "") for tc in obj.iter() if local(tc) == "TextCode").strip()
    tpl_text = " ".join(v for n in names if "/Tpls/" in n and n.endswith("Content.xml")
                        for v in [((tc.text or "")) for tc in ET.fromstring(z.read(n)).iter() if local(tc) == "TextCode"])
    tags: dict[str, list[str]] = {}
    tag_file = next((n for n in names if n.endswith("Tags/CustomTag.xml")), None)
    if tag_file:
        troot = ET.fromstring(z.read(tag_file))
        for el in troot.iter():
            refs = [r.text.strip() for r in el if local(r) == "ObjectRef" and r.text]
            if refs:
                val = " ".join(texts.get(x, "") for x in refs).replace("¥", "").replace("￥", "").strip()
                tags.setdefault(local(el), []).append(val)
    one = lambda k: (tags.get(k) or [None])[0]
    def num(v):
        try:
            return round(float(str(v).replace(",", "").strip()), 2)
        except (TypeError, ValueError):
            return None
    def iso(v):
        m = re.match(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", v or "")
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None
    label = next((x for x in re.findall(r"电子发票（[^）]+）|增值税[^\s]*发票", tpl_text)), None)
    c = {"credential_class": "exp:DigitalInvoice", "jurisdiction": "CN",
         "identity": {k: v for k, v in {"invoice_number": one("InvoiceNo") or custom.get("发票号码"),
                                        "issue_date": iso(one("IssueDate") or custom.get("开票日期")), "type_label": label}.items() if v},
         "seller": {k: v for k, v in {"name": one("SellerName"), "taxpayer_id": one("SellerTaxID") or custom.get("销售方纳税人识别号")}.items() if v},
         "buyer": {k: v for k, v in {"name": one("BuyerName"), "taxpayer_id": one("BuyerTaxID") or custom.get("购买方纳税人识别号")}.items() if v},
         "totals": {k: v for k, v in {"amount_excl_tax": num(one("TaxExclusiveTotalAmount") or custom.get("合计金额")),
                                      "tax_amount": num(one("TaxTotalAmount") or custom.get("合计税额")),
                                      "total_amount": num(one("TaxInclusiveTotalAmount")), "currency": "CNY"}.items() if v is not None},
         "tax": {"tax_scheme": "vat_cn"}}
    if c["totals"].get("total_amount") is None and None not in (c["totals"].get("amount_excl_tax"), c["totals"].get("tax_amount")):
        c["totals"]["total_amount"] = round(c["totals"]["amount_excl_tax"] + c["totals"]["tax_amount"], 2)
    lines = []
    for i, name in enumerate(tags.get("Item", [])):
        rate = (tags.get("TaxScheme") or [None] * (i + 1))[i] if i < len(tags.get("TaxScheme", [])) else None
        pick = lambda k: tags[k][i] if i < len(tags.get(k, [])) else None
        row = {"item_name": name, "amount": num(pick("Amount")), "tax_amount": num(pick("TaxAmount")),
               "unit_price": num(pick("Price")), "quantity": num(pick("Quantity")),
               "tax_rate": round(float(rate.rstrip("%")) / 100, 4) if rate and rate.rstrip("%").replace(".", "").isdigit() else None}
        lines.append({k: v for k, v in row.items() if v not in (None, "")})
    if lines:
        c["line_items"] = lines
    if one("InvoiceClerk"):
        c["issuer_name"] = one("InvoiceClerk")
    if one("Note"):
        c["remarks"] = one("Note")
    mism = []                                            # tags vs OFD.xml CustomDatas
    for key, path_ in (("发票号码", ("identity", "invoice_number")), ("合计金额", ("totals", "amount_excl_tax")),
                       ("合计税额", ("totals", "tax_amount")), ("购买方纳税人识别号", ("buyer", "taxpayer_id"))):
        cv, tv = custom.get(key), (c.get(path_[0]) or {}).get(path_[1])
        if cv and tv is not None and (num(cv) != tv if isinstance(tv, float) else cv != tv):
            mism.append(f"{key}: {cv} ≠ {tv}")
    c["source"] = {"format": "ofd", "extraction_method": "ofd_structured",
                   "digital_signature": "present_unverified" if any("/Signs/" in n or n.endswith("Signatures.xml") for n in names) else "absent",
                   **({"ofd_mismatch": mism} if mism else {})}
    return c


def cmd_credential(a):
    """Record the structured content of one 票据 on its evidence file (merged). 证件号码 are dropped by design."""
    d = Draft(Path(a.draft)); r = d.load(); pol = d.policy()
    ev = find(r["evidence"], "evidence_id", a.evidence)
    if not ev:
        sys.exit(f"error: unknown evidence {a.evidence}")
    if a.from_xml:                                        # --from-xml / --from-source: parse the evidence file itself
        fpath = d.root / ev["path"]
        try:
            parsed = parse_einvoice_ofd(fpath) if fpath.suffix.lower() == ".ofd" else parse_einvoice_xml(fpath)
        except Exception as ex:
            sys.exit(f"error: {ex}")
        patch = deep_merge(parsed, load_json_arg(a.json) if a.json else {})
        kind_ = parsed["source"]["format"]
        a.format, a.method = a.format or kind_, a.method or ("xml" if kind_ == "xml" else "ofd_structured")
    else:
        if not a.json:
            sys.exit("error: --json or --from-xml required")
        patch = load_json_arg(a.json)
    cr = pol.get("credentials") or {}
    banned = [x.lower() for x in cr.get("privacy_excluded", [])]
    dropped = []

    def scrub(o, path=""):
        if isinstance(o, dict):
            for k in list(o):
                if any(b in k.lower() for b in banned):
                    o.pop(k); dropped.append(f"{path}{k}")
                else:
                    scrub(o[k], f"{path}{k}.")
    scrub(patch)
    c = deep_merge(ev.get("credential") or {}, patch)
    c.setdefault("credential_class", kind_class(pol, ev) or "exp:OtherReceipt")
    spec = (cr.get("classes") or {}).get(c["credential_class"], {})
    if spec.get("jurisdiction") == "CN":
        c.setdefault("jurisdiction", "CN")
        if spec.get("standards"):
            c.setdefault("standards", spec["standards"])
        c.setdefault("totals", {}).setdefault("currency", "CNY")
    src = c.setdefault("source", {})
    if a.format:
        src["format"] = a.format
    src.setdefault("format", {"original_electronic": "pdf", "photo": "image", "paper_scan": "image"}.get(ev.get("source_form"), "other")
                   if not ev["path"].endswith((".xml", ".ofd")) else ev["path"].rsplit(".", 1)[1])
    src["trust_rank"] = (cr.get("trust_ranking") or {}).get(src["format"], 1)
    if a.method:
        src["extraction_method"] = a.method
    ev["credential"] = c
    no = (c.get("identity") or {}).get("invoice_number")          # most trusted representation of the same invoice wins
    same = [e for e in r["evidence"] if no and ((e.get("credential") or {}).get("identity") or {}).get("invoice_number") == no]
    for e in same:
        e["credential"]["source"]["authoritative"] = e is max(same, key=lambda x: x["credential"]["source"].get("trust_rank", 1))
    Draft.log(r, "credential", a.evidence, changes={"credential_class": c["credential_class"], "fields": len(_leaf_fields(patch))},
              note=f"dropped (privacy): {', '.join(dropped)}" if dropped else None)
    d.save(r); out({"evidence_id": a.evidence, "credential_class": c["credential_class"], "trust_rank": src["trust_rank"],
                    **({"parsed": {"invoice_number": (c.get("identity") or {}).get("invoice_number"),
                                   "total": (c.get("totals") or {}).get("total_amount"), "lines": len(c.get("line_items") or [])}}
                       if a.from_xml else {}),
                    **({"dropped_for_privacy": dropped} if dropped else {})})


def judgment_effects(it: dict, iid: str, jd: dict, policy: dict, gap) -> None:
    """R-box effects of stored Jev answers (pure; the API is only called by `judge`)."""
    sev = {j["handler"]: j.get("severity", "warning") for j in policy.get("judgments") or []}
    labels = {c: s.get("label", c) for c, s in (policy.get("categories") or {}).items()}
    rec = jd.get((iid, "category"))
    if rec and rec["decision"] and rec["decision"] != it.get("category") and rec["band"] in ("auto", "confirm"):
        gap("judgment", sev["category"] if rec["band"] == "auto" else "info",
            f"{iid} 票面内容更像「{labels.get(rec['decision'], rec['decision'])}」而非「{labels.get(it.get('category'), it.get('category'))}」"
            f"（Jev 置信 {rec['confidence']}）", iid, "judgment:J-CATEGORY", "确认类别；类别决定适用的公司规则")
    rec = jd.get((iid, "personal_spend"))
    if rec and rec["decision"] is True and rec["band"] in ("auto", "confirm"):
        gap("judgment", sev["personal_spend"] if rec["band"] == "auto" else "info",
            f"{iid} 票据可能含个人或不可报销消费（Jev 置信 {rec['confidence']}）", iid, "judgment:J-PERSONAL-SPEND",
            "确认后把个人部分从报销金额中扣除")
    rec = jd.get((iid, "folio_match"))
    if rec and rec["decision"] is False and rec["band"] in ("auto", "confirm"):
        gap("judgment", sev["folio_match"] if rec["band"] == "auto" else "info",
            f"{iid} 水单与发票可能不是同一次住宿或金额不符（Jev 置信 {rec['confidence']}）", iid, "judgment:J-FOLIO-MATCH",
            "核对水单日期、酒店与发票金额")
    rec = jd.get((iid, "credential_class"))
    if rec and rec["band"] == "confirm":
        gap("judgment", "info", f"{iid} 票据类别可能为 {rec['decision']}（Jev 置信 {rec['confidence']}），请确认", iid,
            "judgment:J-CREDENTIAL-CLASS")


ALLOC_FIELDS = ("name", "charge_code", "budget_id", "cost_center")


def resolve_accounting(it: dict, iid: str, hdr: dict, policy: dict, gap) -> None:
    """One accounting line per item: cost allocation resolved item → report → employee default (the report header
    carries the default with origin=profile_default), gaps filled from the A-box project registry, then the
    Chart of Accounts line of the report's rule set."""
    rep, own = hdr.get("default_project") or {}, it.get("project") or {}
    base = own or rep
    level = "item" if own else ("profile_default" if rep.get("origin") == "profile_default" else "report") if rep else None
    acc = {}
    if base.get("code"):
        acc["project_code"] = base["code"]
        inherit = rep if own and rep.get("code") == own.get("code") else {}     # same project → report-level codes apply
        reg = (policy.get("_projects") or {}).get(base["code"], {})
        filled = []
        for f in ALLOC_FIELDS:
            v = base.get(f) or inherit.get(f)
            if not v and reg.get(f):
                v = reg[f]; filled.append(f)
            if v:
                acc["project_name" if f == "name" else f] = v
        if filled:
            acc["filled_from_abox"] = filled
        acc["level"] = level
        bud = acc.get("budget_id")
        if bud and reg.get("budget_id") and bud != reg["budget_id"] and "budget_id" not in filled:
            gap("allocation", "warning", f"{iid} 预算 {bud} 与项目 {base['code']} 登记的预算 {reg['budget_id']} 不一致", iid,
                "allocation:budget_id", "确认预算编号")
        if bud and (policy.get("_projects") or {}).get("__budgets__", {}).get(bud, {}).get("status") in ("closed", "frozen"):
            gap("allocation", "blocker", f"{iid} 预算 {bud} 已{'关闭' if policy['_projects']['__budgets__'][bud]['status'] == 'closed' else '冻结'}",
                iid, "allocation:budget_id", "换用有效预算或联系成本归属负责人")
    al = policy.get("allocation") or {}
    for f, label in (("charge_code", "费用归集码"), ("budget_id", "预算编号")):
        if al.get(f"require_{f}") and base.get("code") and not acc.get(f):
            gap("missing_field", "blocker", f"{iid} 缺少{label}（项目 {base['code']}）", iid, f"field:allocation.{f}",
                f"补充{label}，或在项目登记中设置")
    coa = policy.get("coa")
    if coa:
        pc, cat = acc.get("project_code") or "", it.get("category")
        line = next((m for m in coa["map"] if (cat in m["categories"] or "*" in m["categories"])
                     and (not m.get("project_prefix") or pc.startswith(m["project_prefix"]))), None)
        if line:
            acc.update({"account_code": line["account"], "account_name": coa["accounts"].get(line["account"], ""),
                        "coa_line": line["id"]})
        else:
            gap("unmapped_account", "warning", f"{iid} 类别 {cat} 无对应会计科目", iid, "coa", "请财务补充科目映射")
    rs = (policy.get("rule_set") or {}).get("id")
    if rs and rs != "standard" and acc:
        acc["rule_set"] = rs
    if acc:
        it["accounting"] = acc
    else:
        it.pop("accounting", None)


def is_cn_invoice(it: dict, types: list, cur: str, currency: str = "CNY") -> bool:
    inv = it.get("invoice") or {}
    return inv.get("invoice_type") in types and (it.get("amount") or {}).get("currency", cur) == currency


def receipt_checks(it: dict, iid: str, policy: dict, cur: str, ev_by_id: dict, gap) -> None:
    """Checks the employee-side package can run for the company receipt R-box. Backend-only steps (the e-invoice system's 查验 itself,
    the reimbursement-system import) are not simulated: their *results*, once the employee reports them in invoice.bip, are checked."""
    rc = policy.get("receipt") or {}
    inv = it.get("invoice") or {}
    bip = inv.get("bip") or {}
    ver = rc.get("verification")
    if ver and is_cn_invoice(it, ver["invoice_types"], cur, ver.get("currency", "CNY")):
        need = ver["verify_fields"].get(inv["invoice_type"], [])
        have = lambda f: (it.get("amount") or {}).get("value") is not None if f == "amount" else inv.get(f) not in (None, "")
        missing = [f for f in need if not have(f)]
        if missing:
            gap("invoice_verification", ver["severity"],
                f"{iid} 增值税发票查验要素不全：缺 {'、'.join(VERIFY_LABEL.get(f, f) for f in missing)}", iid, "receipt:verify",
                "从发票票面补录（发票系统查验需要这些要素）")
    for s in rc.get("status_rules", []):
        if s.get("action"):                      # e.g. transfer — checked when the action is requested
            continue
        val = bip.get(s["field"])
        if s["field"] == "warnings":
            for w in val or []:
                gap("invoice_status", s.get("severity", "info"), f"{iid} 发票系统提示：{w}（仅供参考，可继续保存）", iid,
                    "status:warnings")
            continue
        if val is None or not s.get("severity"):
            continue
        bad = (s.get("blocked") and val in s["blocked"]) or (s.get("allowed") and not s.get("blocked") and val not in s["allowed"])
        if bad:
            reason = f"：{bip['failure_reason']}" if s["field"] == "save_status" and bip.get("failure_reason") else ""
            gap("invoice_status", s["severity"],
                f"{iid} {FIELD_LABEL.get(s['field'], s['field'])} = {STATUS_LABEL.get(val, val)}{reason}（{s['id']}）", iid,
                f"status:{s['field']}", s.get("text"))
    li = rc.get("line_items")
    if li and is_cn_invoice(it, li["invoice_types"], cur):
        if bip.get("oa_detail") == "empty" or ("line_items" in inv and not inv["line_items"]):
            gap("invoice_detail", li["severity"], f"{iid} 发票明细为空", iid, "field:invoice.line_items", li.get("remediation"))
    uq, inv_no = rc.get("unique"), inv.get("invoice_no")
    if uq and inv_no and inv_no in (policy.get("_ledger") or {}):
        other = policy["_ledger"][inv_no]
        gap("duplicate_suspect", uq["severity"], f"{iid} 发票号 {inv_no} 已用于报销单 {other}", iid, "unique:invoice_no",
            "确认是否重复报销；如需改到本单，先从原报销单移除")
    routes = rc.get("routes")
    if routes and inv.get("invoice_type") in ("e_invoice", "rail_e_ticket"):     # 数电票: the e-invoice system expects the source file
        forms = {ev_by_id[e].get("source_form") for e in it.get("evidence_ids", []) if e in ev_by_id
                 and ev_by_id[e]["kind"] in ("e_invoice", "invoice", "rail_ticket")}
        if forms and "original_electronic" not in forms and forms - {"unknown"}:
            src = next((x for x in routes if x.get("formats")), routes[0])
            gap("source_file", "warning", f"{iid} 数电票只有扫描件/截图，上传发票系统需要原始文件", iid, "source_file",
                f"从开票平台或邮件下载原始 {'/'.join(src.get('formats', ['pdf', 'ofd', 'xml']))} 文件")


def _route(it: dict, ev_by_id: dict, rc: dict) -> dict:
    forms = [ev_by_id[e].get("source_form") for e in it.get("evidence_ids", []) if e in ev_by_id
             and ev_by_id[e]["kind"] in ("e_invoice", "invoice", "rail_ticket", "taxi_receipt", "receipt", "air_itinerary")]
    by = {x.get("source_form"): x for x in rc.get("routes", [])}
    if "original_electronic" in forms:
        return {"method": by.get("original_electronic", {}).get("method", "源文件上传"), "rule": by.get("original_electronic", {}).get("id")}
    if forms and set(forms) <= {"photo", "paper_scan", "non_source_pdf"}:
        if (it.get("invoice") or {}).get("invoice_type") in ("e_invoice", "rail_e_ticket"):
            return {"method": "先取得源文件（pdf/ofd/xml）再【源文件上传】", "rule": next((x["id"] for x in rc.get("routes", []) if x.get("formats")), None)}
        return {"method": by.get("paper_scan", {}).get("method", "图像识别") + "（保存前核对识别字段）", "rule": by.get("paper_scan", {}).get("id")}
    return {"method": "源文件上传 / 图像识别（按文件类型）", "rule": None}


def build_workflow(r: dict, policy: dict) -> list:
    """E-invoice-system / reimbursement-system submission steps for this report: which invoice goes through which backend step, with done-state
    taken from invoice.bip. Derived only from company rules — no rules, no workflow."""
    rc, wf = policy.get("receipt") or {}, policy.get("workflow") or []
    if not rc and not wf:
        return []
    org, title = policy.get("organization") or {}, policy.get("invoice_title") or {}
    cur = r["report"]["currency"]
    ev_by_id = {e["evidence_id"]: e for e in r["evidence"]}
    ver = rc.get("verification") or {}
    vat_types = ver.get("invoice_types") or []
    success = ver.get("success_state", "相符")
    live = [i for i in r["items"] if i.get("status") != "excluded"]
    abroad = [i for i in live if is_overseas(i, ev_by_id, policy, cur)] if policy.get("credentials") else \
        [i for i in live if (i.get("invoice") or {}).get("invoice_type") == "foreign_invoice"]
    items = [i for i in live if i not in abroad and (i.get("invoice") or {}).get("invoice_type") not in (None, "none", "foreign_invoice")]
    ids = lambda mod: [w["id"] for w in wf if w.get("module") == mod]
    hdr = r["report"]
    tmpl = (rc.get("summary_name") or {}).get("template")
    summ = lambda i: (i.get("invoice") or {}).get("summary_name") or (tmpl.format(
        project_code=((i.get("project") or hdr.get("default_project") or {}).get("code") or ""),
        report_title=hdr.get("title", ""), report_id=hdr["report_id"], category=i.get("category", "")).strip() if tmpl else None)
    bip = lambda i: (i.get("invoice") or {}).get("bip") or {}
    steps = []

    def step(module, text, its=None, done=None, extra_ids=()):
        steps.append({"step": len(steps) + 1, "module": module, "rule_ids": [*ids(module), *extra_ids],
                      "enforced_by": "bip" if module not in ("reimbursement", "oa_integrity") else "oa", "text": text,
                      **({"items": its} if its is not None else {}), "done": done})

    if org.get("bip_tenant"):
        step("tenant", f"登录电子发票系统，确认所属组织为“{org['bip_tenant']}”")
    if items:
        col = [{"item_id": i["item_id"], "invoice_no": (i.get("invoice") or {}).get("invoice_no"), **_route(i, ev_by_id, rc),
                **({"summary_name": summ(i)} if summ(i) else {}), "done": bip(i).get("save_status") == "saved"} for i in items]
        entry = (org.get("systems") or {}).get("bip")
        step("ingestion", "采集发票" + (f"（{entry}）" if entry else "") + "；按文件类型选择采集方式", col,
             all(c["done"] for c in col))
        vat = [i for i in items if (i.get("invoice") or {}).get("invoice_type") in vat_types]
        if vat:
            v = [{"item_id": i["item_id"], "invoice_no": (i.get("invoice") or {}).get("invoice_no"),
                  "done": bip(i).get("verify_result") == success} for i in vat]
            step("verification", f"增值税发票逐张查验，结果须为“{success}”；系统参考性提示不阻断保存", v, all(x["done"] for x in v))
        sv = [{"item_id": i["item_id"], "done": bip(i).get("save_status") == "saved"} for i in items]
        t = f"保存：税务组织选“{title['name']}”" if title.get("name") else "保存：按发票抬头选择税务组织"
        step("save", t + ("；摘要名称按上表填写，便于日后搜索" if tmpl else ""), sv, all(x["done"] for x in sv),
             extra_ids=[(rc.get("summary_name") or {}).get("rule_id")] if tmpl else [])
        sel = [{"item_id": i["item_id"], "reimb_status": bip(i).get("reimb_status")} for i in items]
        step("reimbursement", "在报销系统中查询发票，筛选报账状态为 未报账 / 部分已报账 的发票并选择", sel,
             None)
        if vat:
            d_ = [{"item_id": i["item_id"], "done": bip(i).get("oa_detail") == "ok"} for i in vat]
            step("oa_integrity", "增值税发票导入报销系统后检查发票明细；为空则在发票系统删除后重新采集导入", d_, all(x["done"] for x in d_))
    if abroad:
        steps.append({"step": len(steps) + 1, "module": "ingestion", "rule_ids": [((policy.get("credentials") or {}).get("acceptance") or {}).get("id", "ORG-CRED-ACCEPT")],
                      "enforced_by": "oa", "text": "境外票据（外国发票、收据、小票、签购单）不适用中国税务查验与发票抬头：作为附件随报销单提交（以报销系统实际要求为准）",
                      "items": [{"item_id": i["item_id"], "done": None} for i in abroad], "done": None})
    return steps


def cmd_gaps(a):
    d = Draft(Path(a.draft)); r = d.load()
    compute(r, d.policy())
    Draft.log(r, "compute_gaps", r["report"]["report_id"],
              changes={"status": r["report"]["status"], "gap_count": len(r["gaps"])})
    d.save(r)
    out({"status": r["report"]["status"], "totals": r["report"]["totals"], "gaps": r["gaps"]})


# ───────────────────────── validation ─────────────────────────
def validate(r: dict, draft: Draft | None = None) -> list[str]:
    errs: list[str] = []
    try:
        import jsonschema
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls = getattr(jsonschema, "Draft202012Validator", None)
        if cls is None:   # jsonschema 3.x: drop the urn $id so local "#/$defs/…" refs resolve in-document
            schema = {k: v for k, v in schema.items() if k != "$id"}
            cls = jsonschema.validators.validator_for(schema)
        v = cls(schema, format_checker=jsonschema.FormatChecker())
        for e in sorted(v.iter_errors(r), key=lambda e: list(e.path)):
            errs.append(f"schema: /{'/'.join(map(str, e.path))}: {e.message}")
    except ImportError:
        errs.append("warning: jsonschema not installed — structural checks only")
    # referential integrity
    ev_ids = {e["evidence_id"] for e in r["evidence"]}
    it_ids = {i["item_id"] for i in r["items"]}
    src = set(source_ids(r))
    for i in r["items"]:
        for e in i["evidence_ids"]:
            if e not in ev_ids: errs.append(f"ref: {i['item_id']} → missing evidence {e}")
        prov = i.get("provenance", {})
        for f in _leaf_fields({k: v for k, v in i.items() if k not in (
                "item_id", "provenance", "amount_reimbursable", "accounting", "category", "description")}):
            if f not in prov and not any(f.startswith(k + ".") for k in prov):
                errs.append(f"provenance: {i['item_id']}.{f} has no source")
        for f, p in prov.items():
            if p["source_id"] not in src: errs.append(f"ref: {i['item_id']}.provenance.{f} → unknown source {p['source_id']}")
    for e in r["evidence"]:
        for iid in e.get("item_ids", []):
            if iid not in it_ids: errs.append(f"ref: {e['evidence_id']} → missing item {iid}")
        if e["source_id"] not in src: errs.append(f"ref: {e['evidence_id']} → unknown source {e['source_id']}")
        if draft:
            p = draft.root / e["path"]
            if not p.exists(): errs.append(f"file: {e['path']} missing")
            elif sha256_file(p) != e["sha256"]: errs.append(f"file: {e['path']} hash mismatch")
    return errs


def cmd_validate(a):
    d = Draft(Path(a.draft)); r = d.load()
    errs = validate(r, d)
    out({"valid": not [e for e in errs if not e.startswith("warning")], "errors": errs})


# ───────────────────────── summary & package ─────────────────────────
def summary_md(r: dict, policy: dict) -> str:
    h = r["report"]; cats = policy.get("categories", {})
    L = [f"# {h['title']}", "",
         f"- 报销单号：{h['report_id']}", f"- 员工：{h['employee'].get('name')} {h['employee'].get('employee_id', '')}".rstrip(),
         f"- 项目：{(h.get('default_project') or {}).get('code', '—')} {(h.get('default_project') or {}).get('name', '')}".rstrip(),
         f"- 事由：{h.get('purpose', '—')}",
         *([f"- 规则集：{h['rule_set'].get('label', h['rule_set']['id'])}"
            + (f"（例外，审批 {h['rule_set']['exception'].get('approval_ref')}）" if h['rule_set'].get('basis') == 'exception' else "")]
           if (h.get("rule_set") or {}).get("id", "standard") != "standard" else []),
         *([f"- 收款：{h['payee']['name']} · {h['payee']['bank']}{h['payee'].get('branch', '')} · {h['payee'].get('account_masked', '')}"]
           if h.get("payee") else []),
         f"- 期间：{(h.get('period') or {}).get('start', '—')} ~ {(h.get('period') or {}).get('end', '—')}",
         f"- 状态：**{h['status']}**　合计：**{h['currency']} {h.get('totals', {}).get('grand_total', 0):,.2f}**", "",
         "## 明细", "", "| # | 日期 | 类别 | 说明 | 原币金额 | 可报销 | 凭证 | 状态 |", "|---|---|---|---|---|---|---|---|"]
    for i in sorted(r["items"], key=lambda x: x.get("expense_date", "")):
        amt = i.get("amount") or {}
        L.append(f"| {i['item_id']} | {i.get('expense_date', '')} | {cats.get(i.get('category'), {}).get('label', i.get('category'))} "
                 f"| {i.get('description', '')} | {amt.get('currency', '')} {amt.get('value', 0):,.2f} "
                 f"| {i.get('amount_reimbursable', 0):,.2f} | {', '.join(i['evidence_ids']) or '—'} | {i['status']} |")
    blockers = [g for g in r["gaps"] if g["severity"] == "blocker"]
    warns = [g for g in r["gaps"] if g["severity"] == "warning"]
    if blockers or warns:
        L += ["", "## 待补材料 / 问题", ""]
        L += [f"- ⛔ {g['message']}" + (f" → {g['action']}" if g.get("action") else "") for g in blockers]
        L += [f"- ⚠️ {g['message']}" + (f" → {g['action']}" if g.get("action") else "") for g in warns]
    acc_rows = {}
    for i in r["items"]:
        a = i.get("accounting")
        if a and i.get("status") != "excluded":
            k = (a.get("account_code", "—"), a.get("account_name", ""), a.get("project_code", "—"), a.get("charge_code", "—"),
                 a.get("budget_id", "—"))
            acc_rows[k] = round(acc_rows.get(k, 0) + (i.get("amount_reimbursable") or 0), 2)
    if acc_rows:
        L += ["", "## 会计科目 / 成本归属", "", "| 科目 | 项目 | 归集码 | 预算 | 金额 |", "|---|---|---|---|---|"]
        L += [f"| {c} {n} | {p} | {cc} | {b} | {v:,.2f} |" for (c, n, p, cc, b), v in sorted(acc_rows.items())]
    wf = r.get("workflow") or []
    if wf:
        L += ["", "## 提交步骤（发票系统 / 报销系统）", ""]
        for s in wf:
            mark = {True: "✅", False: "⬜", None: "▫️"}[s.get("done")]
            L.append(f"{s['step']}. {mark} {s['text']}" + (f"  _({', '.join(x for x in s['rule_ids'] if x)})_" if s.get("rule_ids") else ""))
            for x in s.get("items") or []:
                bits = [x["item_id"], x.get("invoice_no") or "", x.get("method") or "",
                        f"摘要：{x['summary_name']}" if x.get("summary_name") else "",
                        STATUS_LABEL.get(x.get("reimb_status"), "") if "reimb_status" in x else ""]
                L.append(f"   - {'✅' if x.get('done') else '⬜'} " + " · ".join(b for b in bits if b))
    return "\n".join(L) + "\n"


def cmd_summary(a):
    d = Draft(Path(a.draft)); r = d.load()
    compute(r, d.policy()); d.save(r)
    print(summary_md(r, d.policy()))


def cmd_package(a):
    d = Draft(Path(a.draft)); r = d.load(); policy = d.policy()
    compute(r, policy)
    blockers = [g for g in r["gaps"] if g["severity"] == "blocker"]
    if blockers and not a.allow_incomplete:
        d.save(r)
        out({"packaged": False, "reason": "blocker gaps remain (use --allow-incomplete for a draft package)",
             "blockers": blockers}); sys.exit(2)
    files = []
    for e in r["evidence"]:
        p = d.root / e["path"]
        files.append({"path": e["path"], "sha256": sha256_file(p), "bytes": p.stat().st_size})
    pkg_id = f"PKG-{dt.datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(2)}"
    r["package"] = {"package_id": pkg_id, "created_at": now(), "policy_id": policy["policy_id"], "files": files}
    Draft.log(r, "package", pkg_id, changes={"status": r["report"]["status"], "files": len(files)})
    errs = [e for e in validate(r, d) if not e.startswith("warning")]
    if errs:
        d.save(r); out({"packaged": False, "errors": errs}); sys.exit(3)
    d.save(r)
    dest = Path(a.out) if a.out else d.root / "Expense_Report_Package.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.json", json.dumps(r, ensure_ascii=False, indent=2))
        z.writestr("summary.md", summary_md(r, policy))
        for e in r["evidence"]:
            z.write(d.root / e["path"], e["path"])
    out({"packaged": True, "zip": str(dest), "status": r["report"]["status"],
         "grand_total": r["report"]["totals"]["grand_total"], "items": len(r["items"]),
         "evidence": len(files), "open_gaps": len(r["gaps"])})


def cmd_scan(a):
    """List candidate vouchers in a folder with hash + PDF text so the agent can classify them."""
    root = Path(a.folder)
    rows = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EVIDENCE_EXTS:
            continue
        try:
            row = {"file": str(p), "ext": p.suffix.lower(), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
        except OSError as ex:           # e.g. a cloud-only (not downloaded) iCloud / OneDrive file
            rows.append({"file": str(p), "ext": p.suffix.lower(), "error": f"unreadable: {ex.strerror or ex}",
                         "hint": "file may be cloud-only — download it (or open it once) and scan again"})
            continue
        if p.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader
                rd = PdfReader(str(p))
                row["pages"] = len(rd.pages)
                text = "\n".join((pg.extract_text() or "") for pg in rd.pages[:3]).strip()
                row["text"] = text[: a.chars]
                row["needs_vision"] = len(text) < 30  # scanned PDF → read the page image
            except Exception as ex:
                row["error"] = str(ex); row["needs_vision"] = True
        else:
            row["needs_vision"] = True
        rows.append(row)
    seen = {}
    for row in rows:
        if "sha256" not in row:
            continue
        if row["sha256"] in seen:
            row["duplicate_of"] = seen[row["sha256"]]
        seen.setdefault(row["sha256"], row["file"])
    out({"folder": str(root), "count": len(rows), "files": rows})


# ───────────────────────── CLI ─────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(prog="expense_pkg", description=__doc__.splitlines()[0])
    sp = ap.add_subparsers(dest="cmd", required=True)

    def p(name, fn, draft=True, **kw):
        s = sp.add_parser(name, **kw)
        if draft:
            s.add_argument("--draft", required=True, help="draft folder")
        s.set_defaults(fn=fn)
        return s

    s = p("init", cmd_init, help="create a new draft report")
    s.add_argument("--employee", required=True); s.add_argument("--employee-id"); s.add_argument("--department")
    s.add_argument("--cost-center"); s.add_argument("--title"); s.add_argument("--project"); s.add_argument("--project-name")
    s.add_argument("--purpose"); s.add_argument("--period-start"); s.add_argument("--period-end")
    s.add_argument("--currency"); s.add_argument("--policy"); s.add_argument("--force", action="store_true")
    s.add_argument("--by-user", action="store_true")

    s = p("set-header", cmd_set_header, help="patch report header fields (JSON)"); s.add_argument("--json", required=True)

    s = p("add-source", cmd_add_source, help="register a raw input (file/folder/utterance)")
    s.add_argument("--type", required=True, choices=["file", "folder", "utterance", "system"])
    s.add_argument("--ref"); s.add_argument("--text")

    s = p("attach", cmd_attach, help="copy a voucher into evidence/")
    s.add_argument("--file", required=True); s.add_argument("--kind", required=True)
    s.add_argument("--source"); s.add_argument("--extracted"); s.add_argument("--item", action="append")
    FORMS = ["original_electronic", "non_source_pdf", "paper_scan", "photo", "unknown"]
    s.add_argument("--source-form", choices=FORMS, help="original e-invoice file vs scan/photo (default: guessed from the file)")
    s = p("credential", cmd_credential, help="record a 票据's structured content (GB/T 42965-aligned for 中国发票; any receipt abroad)")
    s.add_argument("--evidence", required=True); s.add_argument("--json")
    s.add_argument("--from-xml", "--from-source", dest="from_xml", action="store_true",
                   help="parse the evidence file itself: 数电票 national XML or OFD")
    s.add_argument("--format", choices=["xml", "ofd", "pdf", "email_html", "image", "paper", "other"])
    s.add_argument("--method", choices=["xml", "ofd_structured", "pdf_text", "ocr", "manual"])
    s = p("ingest-xml", cmd_ingest_xml, help="create a complete item from a parsed 数电票 credential (category via Jev)")
    s.add_argument("--evidence", required=True); s.add_argument("--model")
    s = p("judge", cmd_judge, help="ask TypeSafe Jev the R-box judgments code cannot decide (needs TYPESAFE_API_KEY)")
    s.add_argument("--model", help="Jev model, e.g. jev-1.13.0 (default: TYPESAFE_DEFAULT_MODEL or jev-latest)")
    s.add_argument("--force", action="store_true", help="ignore the answer cache")
    s = p("set-evidence", cmd_set_evidence, help="correct an evidence file's kind or source form")
    s.add_argument("--evidence", required=True); s.add_argument("--source-form", choices=FORMS); s.add_argument("--kind")

    s = p("upsert", cmd_upsert, help="create or patch an expense item")
    s.add_argument("--json", required=True); s.add_argument("--item"); s.add_argument("--source")
    s.add_argument("--method", default="user_stated",
                   choices=["xml", "ofd_structured", "ocr", "pdf_text", "user_stated", "inferred", "policy_default", "user_confirmed"])
    s.add_argument("--confidence", type=float); s.add_argument("--evidence", action="append")

    s = p("link", cmd_link, help="link evidence to an item"); s.add_argument("--item", required=True)
    s.add_argument("--evidence", action="append", required=True)
    s = p("unlink", cmd_unlink); s.add_argument("--item", required=True); s.add_argument("--evidence", required=True)
    s = p("delete", cmd_delete); s.add_argument("--item", required=True); s.add_argument("--reason")
    s.add_argument("--by-user", action="store_true")
    s = p("merge", cmd_merge); s.add_argument("--keep", required=True); s.add_argument("--drop", action="append", required=True)

    p("gaps", cmd_gaps, help="recompute gaps, totals, status")
    p("validate", cmd_validate, help="schema + referential + file-hash checks")
    p("summary", cmd_summary, help="print human summary (markdown)")
    s = p("package", cmd_package, help="build Expense_Report_Package.zip")
    s.add_argument("--out"); s.add_argument("--allow-incomplete", action="store_true")

    s = p("scan", cmd_scan, draft=False, help="inventory a folder of vouchers")
    s.add_argument("--folder", required=True); s.add_argument("--chars", type=int, default=1500)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
