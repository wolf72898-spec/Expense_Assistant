"""青禾报销助手 · 交互式演示（模拟企业微信/钉钉对话）

一条命令体验四项能力。切换身份可验证权限边界。

    python scripts/demo.py

内置命令：
    :help      帮助
    :who       查看当前身份
    :emp       切换为员工 E001
    :other     切换为另一名员工 E002（用于验证越权拦截）
    :fin       切换为财务 F001
    :quit      退出

试试这些：
    住宿费标准多少
    北京住3晚900元能报多少
    团建费能报吗          → 制度未覆盖，会拒答
    我的报销单
    RPT-2026-0001 什么状态
    RPT-2026-0001        → 用 E002 身份查，会被拦截
    待核队列              → 员工身份会被拒，:fin 后可看
    确认 F3              → 财务复核
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from policy import PolicyRegistry, PolicyStore  # noqa: E402
from qa import PolicyQAService  # noqa: E402
from claim import ClaimService, Principal  # noqa: E402
from claim.adapter import MockClaimAdapter  # noqa: E402
from bot.router import HELP_TEXT, FINANCE_HELP, Intent, route  # noqa: E402
from review.service import FactItem, ReviewService  # noqa: E402

DB = ROOT / "data" / "policy.db"
FACTS = ROOT / "data" / "review" / "demo_facts.json"

IDENTITIES = {
    ":emp": Principal("E001", "张三", {"employee"}, "wecom"),
    ":other": Principal("E002", "李四", {"employee"}, "wecom"),
    ":fin": Principal("F001", "王财务", {"finance"}, "wecom"),
    ":admin": Principal("A001", "制度管理员", {"admin"}, "wecom"),
}


def bootstrap():
    if not DB.exists():
        print("× 制度库未初始化。请先执行：python scripts/init_policy.py\n")
        sys.exit(1)
    store = PolicyStore(DB)
    registry = PolicyRegistry(store)
    qa = PolicyQAService(store, registry)
    claims = ClaimService(MockClaimAdapter(),
                          audit_path=ROOT / "data/audit" / "demo.jsonl")

    # 演示用待核事实：每次启动重置，保证可重复演示
    if FACTS.exists():
        FACTS.unlink()
    rv = ReviewService(FACTS)
    rv.ingest([
        FactItem("F1", "RPT-2026-0001", "ITM-1", "amount", "1280.00",
                 "xml", 0.99, "QH-EXP-001@v1 §2.1"),
        FactItem("F2", "RPT-2026-0001", "ITM-2", "merchant", "深圳某某酒店",
                 "ocr", 0.62, "QH-EXP-001@v1 §2.1"),
        FactItem("F3", "RPT-2026-0001", "ITM-2", "category", "lodging",
                 "inferred", 0.55, "QH-EXP-001@v1 §2.1"),
        FactItem("F4", "RPT-2026-0004", "ITM-1", "amount", "5400.00",
                 "ocr", 0.71, "QH-EXP-001@v1 §2.1"),
    ])
    return qa, claims, rv


def handle(text: str, me: Principal, qa, claims, rv) -> str:
    r = route(text, me)
    if r.denied:
        return f"⛔ {r.deny_reason}"

    if r.intent is Intent.HELP:
        return HELP_TEXT + (FINANCE_HELP if me.is_finance else "")

    if r.intent is Intent.POLICY_QA:
        return qa.answer(text).to_text()

    if r.intent is Intent.CLAIM_LIST:
        return claims.list_text(claims.my_claims(me))

    if r.intent is Intent.CLAIM_STATUS:
        cid = r.claim_id
        if cid:
            try:
                return claims.my_claim(me, cid).to_text()
            except LookupError as e:
                return str(e)
        recs = claims.my_claims(me)
        if not recs:
            return "您当前没有报销单据。"
        return claims.list_text(recs) + "\n\n（说出单号可看详情，如 RPT-2026-0001）"

    if r.intent is Intent.REVIEW_QUEUE:
        q = rv.queue()
        if not q:
            return "待核队列已清空，所有业务事实均已核实。"
        lines = [f"待核事实 {len(q)} 项（按风险排序）："]
        lines += [rv.item_text(f) for f in q]
        return "\n".join(lines)

    if r.intent is Intent.REVIEW_CONFIRM:
        import re
        m = re.search(r"F\d+", text, re.I)
        if not m:
            return "请指定事实编号，如：确认 F3"
        fid = m.group(0).upper()
        if "驳回" in text or "拒绝" in text:
            reason = text.split(fid, 1)[-1].strip(" 的，,。") or "未说明理由"
            try:
                f = rv.reject(fid, me.user_id, reason)
            except Exception as e:
                return f"× {e}"
            ok, why = rv.can_advance_to_payable(f.claim_id)
            return (f"✓ 已驳回 {fid}，理由：{reason}\n"
                    f"  付款闸门：{'放行' if ok else '阻断'} —— {why}")
        try:
            rv.confirm(fid, me.user_id, "演示确认")
        except Exception as e:
            return f"× {e}"
        ok, why = rv.can_advance_to_payable("RPT-2026-0001")
        return f"✓ 已确认 {fid}\n  付款闸门：{'放行' if ok else '阻断'} —— {why}"

    if r.intent is Intent.POLICY_ADMIN:
        return ("制度维护入口尚未实现（骨架阶段）。\n"
                "制度中枢 API 已就绪，见 policy/store.py："
                "create → submit → approve → registry.export()")

    return "没太理解，输入 :help 看看我能做什么。"


def main() -> int:
    qa, claims, rv = bootstrap()
    me = IDENTITIES[":emp"]

    print("=" * 58)
    print(" 青禾报销助手_wolf · 交互式演示（模拟企业微信对话）")
    print("=" * 58)
    print(f"当前身份：{me.name}（{me.user_id}）")
    print(f"生效制度：{qa.registry.effective_refs()}")
    print("输入 :help 看用法。退出：quit / exit / :quit ｜ 身份：:emp :fin :admin :other\n")

    while True:
        try:
            text = input(f"[{me.name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")
            return 0
        if not text:
            continue
        # 退出词：带冒号或不带都认，避免用户猜「quit」却被当成提问
        if text.lower() in (":quit", ":q", ":exit", "quit", "exit", "q", "bye",
                            "退出", "再见"):
            print("\n已退出")
            return 0
        if text == ":help":
            print(HELP_TEXT + (FINANCE_HELP if me.is_finance else ""))
            print("\n身份切换：:emp / :other / :fin / :admin ｜ 查看身份：:who")
            print("退出：quit / exit / :quit / 退出")
            continue
        if text == ":who":
            print(f"  身份：{me.name}（{me.user_id}）角色：{sorted(me.roles)}")
            continue
        if text in IDENTITIES:
            me = IDENTITIES[text]
            print(f"  → 已切换为 {me.name}（{me.user_id}），角色 {sorted(me.roles)}")
            continue
        print(handle(text, me, qa, claims, rv))
        print()


if __name__ == "__main__":
    raise SystemExit(main())
