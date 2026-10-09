# 青禾报销助手_wolf

企业微信 / 钉钉 报销智能体。员工问制度、查自己的单据；财务核事实、管制度。

**一条总原则：AI 负责引制度、做初判、标来源；事实认定权与付款决定权归财务岗。**
AI 的输出是「建议」，不是「结论」。

---

## 快速开始

骨架模块（`policy/` `qa/` `claim/` `review/` `bot/`）**零第三方依赖**，装好 Python 3.10+ 即可跑。
`engine/`（票据解析内核）需要额外依赖。

```bash
# ① 装依赖（只跑骨架可跳过；跑票据解析必须）
pip install -r requirements.txt

# ② 初始化制度库（起草 → 提交 → 审批 → 编译导出）
python scripts/init_policy.py            # 重跑加 --reset

# ③ 交互式体验（模拟企业微信对话）
python scripts/demo.py

# ④ 跑验证
python tests/test_policy_guards.py       # 内控护栏 12 项
python tests/test_e2e.py                 # A/B/D 端到端 21 项
```

### 跑票据解析内核（engine/）

```bash
python engine/expense.py doctor          # 自检，ok 应为 true

python engine/expense.py prep init \
    --user "张三" --employee-id E001 \
    --root data/ws --pkg-skill engine/expense-report-package

python engine/expense.py prep --ws data/ws/张三 report new \
    --title "9月深圳出差" --project P-001 --purpose "客户交流"

python engine/expense.py prep --ws data/ws/张三 ingest <RPT单号> \
    --file <数电票>.xml                  # 支持 .xml / .ofd / .pdf

python engine/expense.py prep --ws data/ws/张三 pkg <RPT单号> gaps
```

实测可用（2026-10-09）：数电票 XML 正确解析出金额 600.0、类目 conference，并产出核对项。

> `engine/` 的已知缺陷与青禾修改见 `engine/CHANGES.md`，**上线前务必用青禾真实票据回归**。

---

## 四项能力

| 需求 | 模块 | 状态 |
|---|---|---|
| A 自助查询有效制度 | `qa/` | 骨架完成，可跑 |
| B 经授权查询本人单据状态 | `claim/` | 骨架完成，数据源待接入 |
| C 制度维护 | `policy/` | 完成（版本/生效期/审批/审计） |
| D 业务事实由财务助手核对 | `review/` | 骨架完成，可跑 |

---

## 目录

```
policy/     制度中枢（需求 C）—— 版本、生效期、审批发布、变更审计
qa/         制度问答（需求 A）—— 生效条款检索 + 确定性限额计算
claim/      单据查询（需求 B）—— 授权、状态适配器
review/     财务复核（需求 D）—— 待核队列、付款闸门、复核留痕
bot/        IM 入口 —— 意图路由 + 权限门
engine/     规则与凭证内核 —— 源自上游，见 engine/CHANGES.md
upstream/   上游参考代码（只读，不参与运行时）
docs/       调研报告与架构设计
```

---

## 不可妥协的四条

1. **制度未覆盖就拒答**，不编造条款
2. **金额一律代码计算**，不让模型心算
3. **越权一律「查无此单」**，不透露单据是否存在
4. **未复核完的事实阻断付款**，不是提示，是拒绝

对应测试在 `tests/`，改代码请一并维护。

---

## 开源合规

本项目在 `bonc-ai/expense-assistant`（Apache-2.0, Copyright 2026 BONC）基础上二次开发。

- 许可证义务见 `THIRD_PARTY_LICENSES.md`
- 版权与来源声明见 `NOTICE`
- 内核修改登记见 `engine/CHANGES.md`

> **新增第三方依赖前必须先核对 License。**
> 无 License 的仓库默认「保留所有权利」，其代码不得进入本仓库。

---

## 当前状态与下一步

已完成：调研选型、合规落地、制度中枢、A/B/D 骨架、IM 路由，33 项验证全通过。

**上线前必须完成：**

1. 单据数据源确认（接现有费控/ERP 还是自建）→ 影响需求 B
2. `policy/packs/qinghe.json` 中所有 `PLACEHOLDER` 数值由财务逐项确认
3. 用青禾真实数电票 XML/OFD 对 `engine/` 做回归测试
4. 企业微信/钉钉 消息收发与用户身份映射

详见 `docs/01-技术架构设计.md`。
