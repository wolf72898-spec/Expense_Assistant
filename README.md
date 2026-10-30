# Expense_Assistant · 报销助手 & FDE 交付物（远筑工程）

> **FDE 四件套已交付** → 点这里：[`docs/fde-交付物/`](docs/fde-交付物/)
> （业务挖掘报告 / 行业分析 / 现状与转型流程图 / 设计方案与报价单）
>
> 企业微信 / 钉钉 报销智能体：员工问制度、查自己的单据；财务核事实、管制度。

**一条总原则：AI 负责引制度、做初判、标来源；事实认定权与付款决定权归财务岗。**
AI 的输出是「建议」，不是「结论」。

---

## 仓库里有什么

| 部分 | 位置 | 说明 |
|---|---|---|
| 报销助手（代码） | `policy/` `qa/` `claim/` `review/` `bot/` `web/` | 可运行的企业报销智能体骨架 |
| FDE 交付物 | `docs/fde-交付物/` | 远筑工程选题的 6 份交付文件 + 原始访谈材料 |

两套内容共用同一套设计内核：**规则版本化、字段级溯源、硬阻断闸门、权限隔离**。
FDE 方案里的「规则中枢 / 溯源标签 / 阻断 / 权限」四项，直接复用报销助手已验证的实现。

---

## 它解决什么

| 角色 | 场景 | 助手做什么 |
|---|---|---|
| 员工 | 住宿费标准多少？北京住 3 晚 3000 能报多少？ | 引制度条款、算金额，**不编造没写进制度的东西** |
| 员工 | 我的报销单到哪一步了？为什么被驳回？ | 只查**本人**的单，附驳回原因和制度依据 |
| 财务 | 这笔票的金额/商户是不是真的？ | 把 AI 没把握的事实排成队列，逐项确认后放行付款 |
| 管理员 | 制度改了怎么发布？ | 起草 → 审批 → 发布，全程留痕，已发布不可改 |

---

## 四项能力

| 需求 | 模块 | 状态 | 关键设计 |
|---|---|---|---|
| **A** 自助查询有效制度 | `qa/` | 可跑 | 只检索**已发布且生效中**的条款；金额一律代码计算 |
| **B** 经授权查本人单据 | `claim/` | 待接入数据源 | 越权统一返回「查无此单」，不泄露单据是否存在 |
| **C** 制度维护 | `policy/` | 完成 | 状态机、已发布不可变、起草人≠审批人、字段级 diff 审计 |
| **D** 业务事实财务核对 | `review/` | 可跑 | 按溯源标签分风险排队，未复核**硬阻断**付款 |

---

## 快速开始

骨架模块（`policy/` `qa/` `claim/` `review/` `bot/`）**零第三方依赖**，Python 3.10+ 即可跑。
Web 看板（`fastapi`/`uvicorn`）与票据解析内核 `engine/`（`pyyaml`）需要安装依赖。

```bash
git clone https://github.com/wolf72898-spec/Expense_Assistant.git
cd Expense_Assistant

pip install -r requirements.txt      # 只跑骨架可跳过

python scripts/init_policy.py        # 初始化制度库（起草→审批→发布）
python scripts/demo.py               # 交互式体验，模拟企业微信对话
python tests/test_e2e.py             # 跑验证
```

### 跑一次看效果

```bash
python scripts/demo.py
```

```
[张三]   > 住宿费标准多少
           住宿费按实际入住夜数报销，标准 500 元/晚。北京、上海 800…（附 §2.1 引用）

[张三]   > 北京住3晚3000元能报多少
           可报 2400.00 元（北京 800/晚 × 3 = 上限 2400，超出部分按上限报销）

[张三]   > 团建费能报吗
           ⚠️ 制度未覆盖，请咨询财务（不编造）

[张三]   > 我的报销单
           RPT-2026-0001  9月深圳客户交流  3280.00元  [审批中] ⚠️待复核
           RPT-2026-0002  10月北京培训     1560.00元  [已通过]
           RPT-2026-0003  9月招待客户       890.00元  [已驳回]

[李四]   > RPT-2026-0001        → 查无此单（越权，且不透露这张单存在）
[李四]   > 待核队列              → ⛔ 该功能仅面向 finance 角色开放
[王财务] > 确认 F3               → 阻断：仍有 1 项业务事实未经财务复核
```

### Web 看板

```bash
python web/app.py       # → http://127.0.0.1:8777
```

浏览器里可在线试问制度、切换身份验证越权、操作复核队列看付款闸门、一键跑测试。

### 票据解析内核

```bash
python engine/expense.py doctor                       # 自检
python engine/expense.py prep init --user "张三" \
    --employee-id E001 --root data/ws \
    --pkg-skill engine/expense-report-package
python engine/expense.py prep --ws data/ws/张三 report new --title "9月深圳" --project P-001
python engine/expense.py prep --ws data/ws/张三 ingest <单号> --file <数电票>.xml
python engine/expense.py prep --ws data/ws/张三 pkg <单号> gaps
```

实测：数电票 XML 解析出金额 600.0、类目 conference；OFD 解析出 300.0、类目 meals。

---

## 目录

```
policy/     制度中枢（C）  版本、生效期、审批发布、变更审计
qa/         制度问答（A）  生效条款检索 + 确定性限额计算
claim/      单据查询（B）  授权、状态适配器（当前为 Mock）
review/     财务复核（D）  待核队列、付款闸门、复核留痕
bot/        IM 入口        意图路由 + 权限门（规则匹配优先）
web/        运行看板       FastAPI + 单页前端
engine/     规则与凭证内核  源自上游，见 engine/CHANGES.md
docs/       调研选型、架构设计、结果报表
```

> `upstream/`（上游仓库完整副本）与 `data/`（运行时产物）不入库，见 `.gitignore`。
> 真正参与构建的是 `engine/`，其内已附 `LICENSE-UPSTREAM` 与 `CHANGES.md`。

---

## 四条不可妥协

1. **制度未覆盖就拒答** —— 宁可说"不知道"，不可编造条款
2. **金额一律代码计算** —— 不让模型心算，算错一次就是钱的问题
3. **越权一律「查无此单」** —— 不透露单据是否存在，防单号枚举探测
4. **未复核完的事实阻断付款** —— 不是提示，是拒绝

对应测试在 `tests/`，改代码请一并维护。

### 为什么"业务事实由财务核对"能落地

基座提供了**字段级溯源**：每个字段都带来源标签。系统据此分风险等级：

| 溯源来源 | 风险 | 是否必须人工核 |
|---|:--:|:--:|
| `xml` / `ofd_structured` | 权威 | 否，直接采信 |
| `user_stated` | 员工自述 | 否（抽查） |
| `pdf_text` / `ocr` | 文档抽取 | **是** |
| `inferred` / `policy_default` | 模型推断 | **是**，且不得作为最终依据 |

确认后溯源标签转为 `finance_confirmed`；只要还有未复核项，付款闸门就返回 False。

---

## 验证

```
tests/test_policy_guards.py   12/12   内控护栏
tests/test_e2e.py             21/21   A/B/D 端到端
bot/router.py                  9/9    意图路由与权限门
engine doctor                  ok     票据内核自检
```

覆盖：职责分离、已发布不可变、历史版本追溯、越权拦截、跨人读取留痕、付款闸门、制度未覆盖拒答。

---

## 开源合规

在 [`bonc-ai/expense-assistant`](https://github.com/bonc-ai/expense-assistant)
（Apache-2.0 · Copyright 2026 BONC）基础上二次开发。

- 许可证义务 → `THIRD_PARTY_LICENSES.md`
- 版权与来源声明 → `NOTICE`
- 内核修改登记 → `engine/CHANGES.md`

> **新增依赖前必须先核对 License。** 无 License 的仓库默认「保留所有权利」。

调研过的 4 个仓库（RAG 问答、审计 Agent、Spring Boot 发票审计等）虽更贴近部分需求，
但**均未声明 License**，故仅参考其公开设计理念，未引入任何代码。

---

## 当前状态与下一步

已完成：调研选型、合规落地、制度中枢、A/B/D 模块、IM 路由、Web 看板，42 项验证全通过。

**上线前必须完成：**

1. **确认单据数据源** —— 接现有费控/ERP 还是自建（当前 `claim/adapter.py` 为 Mock）
2. **补齐制度数值** —— `policy/packs/qinghe.json` 中所有 `PLACEHOLDER` 需财务逐项确认
3. **真实票据回归** —— 用青禾真实数电票 XML/OFD 验证 `engine/` 解析准确率
4. **接入 IM** —— 企业微信/钉钉消息收发与用户身份映射（当前身份为演示固定账号）

### 已知风险

基座 star 0、仅 1 次提交、无社区验证。已修复其 4 处会导致崩溃的缺陷
（详见 `engine/CHANGES.md`），并发现其自带 schema 与自带制度包字段命名不一致。
**票据解析准确率必须用真实票据验证后才可采信。**

---

## License

本项目代码采用 Apache-2.0（`engine/` 继承上游）。上游版权归 BONC 所有，见 `NOTICE`。
