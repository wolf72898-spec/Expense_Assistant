"""制度中枢内控护栏测试

验证四条强制约束真的拦得住，而不是写在文档里好看：
  1. 起草人不能审批自己的制度（职责分离）
  2. 已发布版本不可编辑，只能发新版本
  3. 非法状态迁移被拒绝
  4. 未发布 / 已过期的制度不会被引擎引用
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from policy import PolicyStore, PolicyRegistry, PolicyError  # noqa: E402

DB = ROOT / "data" / "test_guards.db"
if DB.exists():
    DB.unlink()

CONTENT = {"categories": {"meals": {"label": "餐费", "cap": {"amount": 150, "per": "person"}}}}
CONTENT2 = {"categories": {"meals": {"label": "餐费", "cap": {"amount": 200, "per": "person"}}}}

passed, failed = 0, 0


def check(name: str, should_raise: bool, fn) -> None:
    global passed, failed
    try:
        fn()
        if should_raise:
            print(f"  ✗ {name} —— 本应被拦截但通过了")
            failed += 1
        else:
            print(f"  ✓ {name}")
            passed += 1
    except PolicyError as e:
        if should_raise:
            print(f"  ✓ {name} —— 已拦截：{e}")
            passed += 1
        else:
            print(f"  ✗ {name} —— 误拦截：{e}")
            failed += 1


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    store = PolicyStore(DB)
    registry = PolicyRegistry(store)

    print("\n【1】职责分离：起草人不得审批自己的制度")
    pv = store.create("T-001", "测试制度", CONTENT, "2026-01-01", actor="alice")
    store.submit("T-001", 1, actor="alice")
    check("起草人 alice 自我审批", should_raise=True,
          fn=lambda: store.approve("T-001", 1, approver="alice"))
    check("他人 bob 审批", should_raise=False,
          fn=lambda: store.approve("T-001", 1, approver="bob"))

    print("\n【2】已发布版本不可编辑")
    check("修改已发布版本", should_raise=True,
          fn=lambda: store.update_draft("T-001", 1, CONTENT2, actor="alice"))
    check("修订发新版本", should_raise=False,
          fn=lambda: store.revise("T-001", CONTENT2, "2026-07-01", actor="alice"))
    check("编辑新草稿", should_raise=False,
          fn=lambda: store.update_draft("T-001", 2, CONTENT2, actor="alice"))

    print("\n【3】状态机：非法迁移被拒")
    check("草稿直接发布（绕过审批）", should_raise=True,
          fn=lambda: store.approve("T-001", 2, approver="bob"))

    print("\n【4】版本取代：发布 v2 后 v1 自动作废")
    store.submit("T-001", 2, actor="alice")
    store.approve("T-001", 2, approver="bob")
    v1 = store.get("T-001", 1)
    v2 = store.get("T-001", 2)
    ok1 = v1.status == "superseded"
    ok2 = v2.status == "published"
    print(f"  {'✓' if ok1 else '✗'} v1 状态 = {v1.status}（期望 superseded）")
    print(f"  {'✓' if ok2 else '✗'} v2 状态 = {v2.status}（期望 published）")
    passed + ok1 + ok2 and None
    globals()["passed"] = passed + int(ok1) + int(ok2)
    globals()["failed"] = failed + int(not ok1) + int(not ok2)

    print("\n【5】生效版本解析：只取当前生效版本")
    eff = store.effective("T-001", day="2026-08-01")
    ok = eff is not None and eff.version == 2
    print(f"  {'✓' if ok else '✗'} 2026-08-01 生效版本 = v{eff.version if eff else None}（期望 v2）")
    globals()["passed"] = passed + int(ok)
    globals()["failed"] = failed + int(not ok)

    early = store.effective("T-001", day="2026-02-01")
    ok_e = early is not None and early.version == 1
    print(f"  {'✓' if ok_e else '✗'} 2026-02-01 生效版本 = v{early.version if early else None}（期望 v1，历史可追溯）")
    globals()["passed"] = passed + int(ok_e)
    globals()["failed"] = failed + int(not ok_e)

    print("\n【6】无生效制度时引擎拒绝编译")
    store2 = PolicyStore(ROOT / "data" / "test_empty.db")
    reg2 = PolicyRegistry(store2)
    check("空库编译", should_raise=True, fn=lambda: reg2.compile())

    print("\n【7】变更审计可追溯")
    trail = store.audit_trail("T-001")
    ok_t = len(trail) >= 6
    print(f"  {'✓' if ok_t else '✗'} 审计记录 {len(trail)} 条，含 diff：")
    for t in trail[:3]:
        d = t["detail"]
        keys = "字段变更" if "diff" in d or "diff_vs_current" in d else ",".join(d.keys())
        print(f"      v{t['version']} {t['op']:9} {t['actor']:6} {keys}")
    globals()["passed"] = passed + int(ok_t)
    globals()["failed"] = failed + int(not ok_t)

    store.close()
    store2.close()

    print(f"\n{'='*50}")
    print(f"通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
