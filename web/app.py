"""青禾报销助手 · Web 看板

把「运行结果」和「四项能力」搬进浏览器：
  · 在线试问制度（需求 A）
  · 切换身份查单据，看越权拦截（需求 B）
  · 财务复核队列与付款闸门（需求 D）
  · 制度版本与变更审计（需求 C）

启动：
    python web/app.py
    → http://127.0.0.1:8777
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from policy import PolicyRegistry, PolicyStore  # noqa: E402
from qa import PolicyQAService  # noqa: E402
from claim import ClaimService, Principal  # noqa: E402
from claim.adapter import MockClaimAdapter, ClaimStatus  # noqa: E402
from claim.authz import NotAuthenticated  # noqa: E402
from review.service import FactItem, ReviewService  # noqa: E402
from bot.router import route  # noqa: E402

DB = ROOT / "data" / "policy.db"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="青禾报销助手_wolf · 运行看板")

# ── 运行时（演示态：进程内单例，生产应改为连接池 + 真实身份源）──
store = PolicyStore(DB)
registry = PolicyRegistry(store)
qa = PolicyQAService(store, registry)
claims = ClaimService(MockClaimAdapter(), audit_path=ROOT / "data/audit" / "web.jsonl")
review = ReviewService(ROOT / "data/review" / "web_facts.json")

# 演示用待核事实：首次启动播种
if not review.facts:
    review.ingest([
        FactItem("F1", "RPT-2026-0001", "ITM-1", "amount", "1280.00",
                 "xml", 0.99, "QH-EXP-001@v1 §2.1"),
        FactItem("F2", "RPT-2026-0001", "ITM-2", "merchant", "深圳某某酒店",
                 "ocr", 0.62, "QH-EXP-001@v1 §2.1"),
        FactItem("F3", "RPT-2026-0001", "ITM-2", "category", "lodging",
                 "inferred", 0.55, "QH-EXP-001@v1 §2.1"),
        FactItem("F4", "RPT-2026-0004", "ITM-1", "amount", "5400.00",
                 "ocr", 0.71, "QH-EXP-001@v1 §2.1"),
    ])

# 演示身份：真实环境应来自企业微信/钉钉 OAuth
IDENTITIES = {
    "E001": Principal("E001", "张三", {"employee"}, "wecom"),
    "E002": Principal("E002", "李四", {"employee"}, "wecom"),
    "F001": Principal("F001", "王财务", {"finance"}, "wecom"),
}


# ─────────────────────── 接口 ───────────────────────

class AskIn(BaseModel):
    question: str
    user: str = "E001"


class ReviewIn(BaseModel):
    fact_id: str
    action: str          # confirm / reject
    reason: str = ""
    user: str = "F001"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    """看板顶部：项目与制度状态"""
    hist = store.history("QH-EXP-001")
    return {
        "policy_refs": registry.effective_refs(),
        "versions": [{"ref": p.ref, "status": p.status,
                      "effective_from": p.effective_from,
                      "effective_to": p.effective_to,
                      "approved_by": p.approved_by} for p in hist],
        "audit_count": len(store.audit_trail("QH-EXP-001")),
        "clause_count": len(store.clauses()),
        "pending_review": len(review.queue()),
        "identities": [{"id": k, "name": v.name, "roles": sorted(v.roles)}
                       for k, v in IDENTITIES.items()],
    }


@app.post("/api/ask")
def ask(body: AskIn):
    """需求 A：在线试问制度"""
    a = qa.answer(body.question)
    return {
        "question": a.question,
        "answer": a.to_text(),
        "citations": a.citations,
        "escalated": a.escalated,
        "escalation_reason": a.escalation_reason,
        "amount": (a.calculation.reimbursable if a.calculation else None),
        "route": route(body.question, IDENTITIES.get(body.user)).intent.value,
    }


@app.get("/api/claims")
def list_claims(user: str = "E001"):
    """需求 B：本人单据列表"""
    p = IDENTITIES.get(user)
    try:
        recs = claims.my_claims(p)
    except NotAuthenticated as e:
        raise HTTPException(401, str(e)) from e
    return {
        "user": user,
        "records": [
            {"claim_id": r.claim_id, "title": r.title, "amount": r.amount,
             "status": r.status, "status_label": r.status_label,
             "owner_id": r.owner_id, "current_approver": r.current_approver,
             "reject_reason": r.reject_reason, "pending_review": r.pending_review,
             "policy_ref": r.policy_ref}
            for r in recs
        ],
    }


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: str, user: str = "E001"):
    """需求 B：单据详情。越权一律「查无此单」"""
    p = IDENTITIES.get(user)
    try:
        r = claims.my_claim(p, claim_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except NotAuthenticated as e:
        raise HTTPException(401, str(e)) from e
    return {"claim_id": r.claim_id, "title": r.title, "amount": r.amount,
            "status": r.status, "status_label": r.status_label,
            "text": r.to_text()}


@app.get("/api/review/queue")
def review_queue(user: str = "F001"):
    """需求 D：待核队列"""
    p = IDENTITIES.get(user)
    if not (p and p.is_finance):
        raise HTTPException(403, "该功能仅面向 finance 角色开放")
    items = review.queue()
    return {
        "items": [
            {"fact_id": f.fact_id, "claim_id": f.claim_id, "field": f.field,
             "value": f.value, "provenance": f.provenance,
             "trust": f.trust.value, "confidence": f.confidence,
             "risk": f.risk_score(), "policy_ref": f.policy_ref,
             "status": f.status}
            for f in items
        ],
    }


@app.post("/api/review/act")
def review_act(body: ReviewIn):
    """需求 D：财务确认 / 驳回"""
    p = IDENTITIES.get(body.user)
    if not (p and p.is_finance):
        raise HTTPException(403, "该功能仅面向 finance 角色开放")
    try:
        if body.action == "confirm":
            f = review.confirm(body.fact_id, p.user_id, body.reason or "看板确认")
        elif body.action == "reject":
            if not body.reason.strip():
                raise HTTPException(400, "驳回必须填写理由")
            f = review.reject(body.fact_id, p.user_id, body.reason)
        else:
            raise HTTPException(400, "action 只能是 confirm / reject")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    ok, why = review.can_advance_to_payable(f.claim_id)
    return {"fact_id": f.fact_id, "status": f.status,
            "claim_id": f.claim_id, "payable": ok, "gate_reason": why}


@app.get("/api/policy/categories")
def categories():
    """需求 C：生效制度类目一览"""
    pol = registry.compile()
    return {
        "policy_refs": registry.effective_refs(),
        "categories": [
            {"key": k, "label": v.get("label", k),
             "cap": v.get("cap", {}).get("amount"),
             "per": v.get("cap", {}).get("per"),
             "by_city": v.get("cap", {}).get("by_city", {})}
            for k, v in pol.get("categories", {}).items()
        ],
    }


@app.get("/api/runtests")
def run_tests():
    """在线跑验证，返回真实输出"""
    out = {}
    for name, script in (("guards", "tests/test_policy_guards.py"),
                         ("e2e", "tests/test_e2e.py")):
        r = subprocess.run([sys.executable, str(ROOT / script)],
                           capture_output=True, text=True, cwd=str(ROOT),
                           encoding="utf-8", errors="replace", timeout=180)
        out[name] = {"code": r.returncode, "output": (r.stdout or "")[-4000:]}
    # 清理测试副产物
    for f in ("data/test_guards.db", "data/test_empty.db"):
        p = ROOT / f
        if p.exists():
            p.unlink()
    return out


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    print("青禾报销助手 · 运行看板 → http://127.0.0.1:8777")
    uvicorn.run(app, host="127.0.0.1", port=8777, log_level="warning")
