"""端到端冒烟：需求 A（制度问答）· B（本人单据）· D（财务复核）

跑通即证明骨架可用。运行：
    python tests/test_e2e.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from policy import PolicyRegistry, PolicyStore  # noqa: E402
from qa import PolicyQAService  # noqa: E402
from claim import ClaimService, Principal  # noqa: E402
from claim.adapter import MockClaimAdapter  # noqa: E402
from claim.authz import NotAuthenticated  # noqa: E402
from review.service import FactItem, ReviewService  # noqa: E402

DB = ROOT / "data" / "policy.db"
passed, failed = 0, 0


def expect(title: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        print(f"  ✓ {title}")
        passed += 1
    else:
        print(f"  ✗ {title} {extra}")
        failed += 1


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    if not DB.exists():
        print("请先运行 scripts/init_policy.py 初始化制度库")
        return 1

    store = PolicyStore(DB)
    registry = PolicyRegistry(store)
    qa = PolicyQAService(store, registry)

    print("\n【需求 A】自助查询有效制度")
    a1 = qa.answer("住宿费报销标准是多少")
    print("  Q: 住宿费报销标准是多少")
    print("  A:", a1.to_text().replace("\n", "\n     "))
    expect("有制度引用", bool(a1.citations))
    expect("未转人工", not a1.escalated)

    print("\n  Q: 北京住3晚900元能报多少")
    a2 = qa.answer("北京住3晚900元能报多少")
    print("  A:", a2.to_text().replace("\n", "\n     "))
    expect("走确定性计算器", a2.calculation is not None)
    expect("北京标准 800/晚 × 3 = 2400，未超标 900 → 可报 900",
           a2.calculation is not None and abs(a2.calculation.reimbursable - 900) < 0.01,
           f"实得 {a2.calculation.reimbursable if a2.calculation else None}")

    print("\n  Q: 北京住3晚3000元能报多少")
    a3 = qa.answer("北京住3晚3000元能报多少")
    print("  A:", a3.to_text().replace("\n", "\n     "))
    expect("超标按上限 2400 截断（mode=clip）",
           a3.calculation is not None and abs(a3.calculation.reimbursable - 2400) < 0.01,
           f"实得 {a3.calculation.reimbursable if a3.calculation else None}")

    print("\n  Q: 我的个税怎么算")
    a4 = qa.answer("我的个税怎么算")
    print("  A:", a4.to_text())
    expect("越界问题转人工", a4.escalated)

    print("\n  Q: 团建费用能报销吗（制度未覆盖）")
    a5 = qa.answer("团建费用能报销吗")
    print("  A:", a5.to_text())
    expect("制度未覆盖时拒答而非编造", a5.escalated)

    print("\n【需求 B】经授权查询本人单据状态")
    svc = ClaimService(MockClaimAdapter(), audit_path=ROOT / "data/audit/e2e.jsonl")
    me = Principal(user_id="E001", name="张三", channel="wecom")
    other = Principal(user_id="E002", name="李四", channel="wecom")
    fin = Principal(user_id="F001", name="王财务", roles={"finance"}, channel="wecom")

    mine = svc.my_claims(me)
    print("  我的单据：")
    print("   ", svc.list_text(mine).replace("\n", "\n    "))
    expect("只返回本人单据", all(r.owner_id == "E001" for r in mine))
    expect("本人有 3 张单", len(mine) == 3, f"实得 {len(mine)}")

    try:
        svc.my_claim(other, "RPT-2026-0001")
        expect("查他人单据被拦截", False)
    except LookupError as e:
        expect("查他人单据被拦截，且表述不泄露存在性", str(e).startswith("查无此单"), f"→ {e}")

    try:
        svc.my_claims(None)
        expect("无身份被拦截", False)
    except NotAuthenticated:
        expect("无身份被拦截", True)

    rec = svc.my_claim(me, "RPT-2026-0001")
    print("  单据详情：")
    print("   ", rec.to_text().replace("\n", "\n    "))
    expect("详情含状态与审批人", rec.status == "approving" and rec.current_approver != "")

    fin_view = svc.my_claims(fin)
    expect("财务角色可跨人查看（且已留痕）", len(fin_view) == 4, f"实得 {len(fin_view)}")
    audit_file = ROOT / "data" / "audit" / "e2e.jsonl"
    expect("跨人访问已写入审计日志",
           audit_file.exists() and audit_file.read_text(encoding="utf-8").count("\n") > 0)

    print("\n【需求 D】业务事实由财务助手核对")
    # 复核状态持久化，测试需幂等：每次清空脏数据
    facts_file = ROOT / "data" / "review" / "e2e_facts.json"
    review_log = ROOT / "data" / "review" / "review_log.jsonl"
    for f in (facts_file, review_log):
        if f.exists():
            f.unlink()
    rv = ReviewService(facts_file)
    facts = [
        FactItem("F1", "RPT-2026-0001", "ITM-1", "amount", "1280.00",
                 "xml", 0.99, "QH-EXP-001@v1 §2.1"),          # 权威来源，不需复核
        FactItem("F2", "RPT-2026-0001", "ITM-2", "merchant", "某某酒店",
                 "ocr", 0.62, "QH-EXP-001@v1 §2.1"),          # OCR，必须复核
        FactItem("F3", "RPT-2026-0001", "ITM-2", "category", "lodging",
                 "inferred", 0.55, "QH-EXP-001@v1 §2.1"),     # 模型推断，必须复核
    ]
    n = rv.ingest(facts)
    expect("只把需复核的事实入队（权威来源不占财务时间）", n == 2, f"入队 {n}")

    q = rv.queue("RPT-2026-0001")
    print("  待核队列（按风险排序）：")
    for f in q:
        print("   ", rv.item_text(f).replace("\n", "\n     "))
    expect("高风险（inferred）排最前", q[0].fact_id == "F3", f"实得 {q[0].fact_id}")

    ok, reason = rv.can_advance_to_payable("RPT-2026-0001")
    print(f"  付款闸门：{ok} —— {reason}")
    expect("未复核完阻断付款", not ok)

    rv.confirm("F3", reviewer="F001", comment="核实为住宿费，与酒店水单一致")
    ok2, reason2 = rv.can_advance_to_payable("RPT-2026-0001")
    expect("还剩一项未复核，仍然阻断", not ok2, reason2)

    rv.confirm("F2", reviewer="F001", comment="发票销售方名称核对无误")
    ok3, reason3 = rv.can_advance_to_payable("RPT-2026-0001")
    print(f"  付款闸门：{ok3} —— {reason3}")
    expect("全部复核后放行", ok3)

    rv.reject("F2", reviewer="F001", reason="与发票抬头不符，退回重开")
    ok4, reason4 = rv.can_advance_to_payable("RPT-2026-0001")
    expect("驳回后再次阻断", not ok4, reason4)

    expect("复核动作全部留痕",
           review_log.exists()
           and review_log.read_text(encoding="utf-8").count("\n") >= 3)

    store.close()
    print(f"\n{'=' * 52}\n通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
