"""初始化青禾制度库

把制度包与条款导入制度中枢，走完 起草 → 提交 → 审批 的完整内控流程，
再编译导出为引擎可用的 policy.json。

运行：
    python scripts/init_policy.py

注意：脚本中的起草人/审批人为占位账号，接入真实 SSO 后应改为实际用户标识，
且必须保证起草人与审批人不是同一人。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from policy import PolicyStore, PolicyRegistry, PolicyClause  # noqa: E402

PACK = ROOT / "policy" / "packs" / "qinghe.json"
CLAUSES = ROOT / "policy" / "packs" / "qinghe.clauses.json"
DB = ROOT / "data" / "policy.db"
OUT = ROOT / "data" / "compiled" / "policy.json"

POLICY_ID = "QH-EXP-001"
DRAFTER = "admin@qinghe"        # TODO: 接入 SSO 后替换为真实制度管理员
APPROVER = "finance@qinghe"     # TODO: 接入 SSO 后替换为真实财务负责人


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    content = json.loads(PACK.read_text(encoding="utf-8"))
    raw_clauses = json.loads(CLAUSES.read_text(encoding="utf-8"))

    store = PolicyStore(DB)
    registry = PolicyRegistry(store)

    reset = "--reset" in sys.argv
    exists = store.conn.execute(
        "SELECT 1 FROM policy_versions WHERE policy_id=?", (POLICY_ID,)).fetchone()
    if exists and not reset:
        print(f"制度 {POLICY_ID} 已存在，跳过导入。")
        print(f"  当前生效版本：{registry.effective_refs()}")
        print("  如需用模板重建，请运行：python scripts/init_policy.py --reset")
        store.close()
        return 0
    if exists and reset:
        store.conn.execute("DELETE FROM policy_versions WHERE policy_id=?", (POLICY_ID,))
        store.conn.execute("DELETE FROM policy_clauses WHERE policy_id=?", (POLICY_ID,))
        store.conn.execute("DELETE FROM policy_changes WHERE policy_id=?", (POLICY_ID,))
        store.conn.commit()
        print(f"已清除 {POLICY_ID} 的既有记录，重新导入")

    # 1. 起草
    pv = store.create(
        policy_id=POLICY_ID,
        title="青禾费用报销制度",
        content=content,
        effective_from="2026-01-01",
        actor=DRAFTER,
        summary="初始版本，由制度模板导入，数值待财务确认",
    )
    print(f"[1/5] 起草完成：{pv.ref}  状态={pv.status}")

    # 2. 条款切片（供需求 A 制度问答检索）
    clauses = [
        PolicyClause(
            clause_id=c["clause_id"], policy_id=POLICY_ID, version=pv.version,
            clause_no=c["clause_no"], title=c["title"], text=c["text"],
            category=c.get("category", ""), tags=c.get("tags", []),
        ) for c in raw_clauses
    ]
    store.replace_clauses(POLICY_ID, pv.version, clauses)
    print(f"[2/5] 条款切片入库：{len(clauses)} 条")

    # 3. 提交审批
    pv = store.submit(POLICY_ID, pv.version, actor=DRAFTER)
    print(f"[3/5] 已提交审批：{pv.ref}  状态={pv.status}")

    # 4. 财务审批（起草人与审批人不同，满足职责分离）
    pv = store.approve(POLICY_ID, pv.version, approver=APPROVER,
                       comment="模板导入，数值为占位，上线前需逐项确认")
    print(f"[4/5] 审批通过并发布：{pv.ref}  状态={pv.status}  审批人={pv.approved_by}")

    # 5. 编译导出
    path = registry.export(OUT)
    compiled = json.loads(path.read_text(encoding="utf-8"))
    print(f"[5/5] 已编译导出：{path}")

    # 同时导出到 company/policy.json —— 引擎 init 工作区时直接加载这份
    company_path = registry.export(ROOT / "company" / "policy.json")
    print(f"      引擎用制度包：{company_path}")

    print(f"      生效制度引用：{registry.effective_refs()}")
    print(f"      类目数：{len(compiled.get('categories', {}))}")
    print(f"      票据规则数：{len(compiled.get('receipt_rules', []))}")

    # 校验：是否能取到生效条款
    citations = registry.clause_citations(category="lodging")
    print(f"      住宿类目条款引用：{[c['citation'] for c in citations]}")

    # 审计轨迹
    trail = store.audit_trail(POLICY_ID)
    print(f"\n变更审计轨迹（{len(trail)} 条）：")
    for t in trail:
        print(f"  v{t['version']}  {t['op']:8} {t['actor']:16} ({t['actor_role']})  {t['at']}")

    print("\n⚠️  制度包内所有 PLACEHOLDER 数值均为占位，上线前必须经财务逐项确认。")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
