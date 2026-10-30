"""驱动 FDE 业务挖掘实验室完成一轮完整访谈，并导出发现报告。

用法：
    python scripts/fde_interview.py --scenario construction-change

两个关键约束（踩过坑，别改回去）：
1. 会话必须保持 cookie。否则实验室报「案例已切换，请刷新后重试」。
2. /api/switch 会把采集进度清零。因此所有问题必须在同一次会话里问完，
   不能分轮执行；脚本只 switch 一次，之后一路 ask 到底。

事实由实验室系统按其内置证据库判定是否「发现」，本脚本只负责提出有
针对性的问题并把全过程落盘，不代写任何事实。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

BASE = "https://fde-discovery-lab-67046.app.workbuddy.host"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "fde-交付物" / "_raw"
TMP = ROOT / ".tmp"

# 每个角色的问题按「流程 → 异常 → 指标样本 → 数据/规则 → 权限与边界 → 复核/交接/故障」
# 逐层设计，覆盖实验室列出的全部证据缺口：预算、决策、指标、复核、交接、
# 更新、故障、权限、边界、投诉、反馈。
QUESTION_BANK: dict[str, list[str]] = {
    "r0": [  # 顾明岚 · 项目经理（Buyer）
        "请按环节描述一个工程变更从提出、评审、图纸更新、现场交底到签证确认的完整流程，每个环节由谁负责、需要多久？",
        "最近一次因变更导致的返工或争议是怎样的？根因是什么？当时现场拿到的是哪一版图纸？",
        "目前变更与签证有没有可量化的基线数据，比如一次通过率、签证闭环周期、争议率？由谁统计、口径是什么？",
        "如果引入 AI 辅助，哪些环节允许系统自动处理，哪些必须人工确认？试点验收标准应该怎么定？",
        "试点是否明确只覆盖只读查询与提醒，不做跨系统状态同步？能否给出书面确认？",
        "12天降到6天的目标，是否接受按六个节点（现场问题、设计澄清、变更审批、施工交底、签证计量、结算）分别计量验收，而不是只看总天数？",
        "各节点目前的等待时间和处理时间，有没有任何可用的记录或粗略估算？",
        "这次试点的预算大概是多少？成本怎么核算，谁签字？",
        "这个方案谁参与决策？谁有权否决？",
        "试点期间的反馈怎么收集？怎么形成改进闭环？",
    ],
    "r1": [  # 林亦舟 · 现场施工员（Operator）
        "你在现场是怎么拿到施工图和变更通知的？拿到之后怎么确认这就是最新版本？",
        "有没有遇到过拿着旧图施工的情况？当时是怎么发现的、造成了什么损失、后续怎么处理的？",
        "现场发现问题后你通过什么渠道上报？上报之后多久能得到答复？有没有不了了之的情况？",
        "你最希望系统帮上什么忙？哪些信息你必须当场人工确认、不能只信系统？",
        "现场终端能否暂存草稿并显示最后同步时间？高峰期补录一份记录实际要花多少分钟？",
        "除了拿旧图施工，还有哪些一线异常最容易演变成签证争议？请举最近发生的具体例子。",
        "微信群里的口头指令后来有没有补成正式记录？补不上的大概占多少？",
        "你手上一个月大概要处理多少张变更或签证相关的单？平均每张花多久？有没有统计样本？",
        "系统给出的辅助结果你怎么复核？发现错了怎么办、找谁？",
    ],
    "r2": [  # 唐映秋 · 造价工程师
        "变更的造价测算依据什么？合同清单、定额、市场价分别在什么情况下使用？口径冲突时怎么判定？",
        "签证单上的工程量和单价是怎么核对的？最常见的退回或被打回重做的原因是什么？",
        "一份签证从编制到业主确认平均要多久？主要卡在哪个环节？手上积压多少？",
        "哪些计价规则是合同或定额里写得明明白白可以查的，哪些其实靠经验判断？能否列出依据条款？",
        "签证依据完整率的统计分母按单据数还是字段数？退回原因是否必须保留原步骤？",
        "上个月被退回的签证单里，退回原因分别是哪几类、各占多少？",
        "签证资料里哪些是结构化可以直接查的，哪些是扫描件或照片只能人工翻？比例大概多少？",
        "交接给下一环节时需要什么？责任从什么时候开始转移？",
        "计价规则多久更新一次？过期或失效的版本怎么识别？",
    ],
    "r3": [  # 秦芮 · 工程平台工程师
        "现在有哪些系统在用？BIM、项目管理系统、OA、档案系统各自存什么数据？能不能打通？接口和权限现状如何？",
        "图纸版本号、变更单号在各个系统里是不是一致？有没有统一的主数据或唯一标识？",
        "目前哪些数据是结构化的、可以直接做自动校验？哪些还停留在扫描件、照片、聊天记录里？",
        "如果做试点，最小可行的系统集成范围是什么？数据脱敏、操作留痕和审计要求怎么满足？",
        "现有平台能否对缺签变更做到阻断提交，而不只是提示？需要哪些前置条件？",
        "各系统里结构化数据和非结构化材料的分布大概是什么比例？哪些字段可以直接做自动校验？",
        "脱敏规则由谁制定，平台只执行还是参与制定？",
        "各系统数据多久同步一次？过期信息怎么识别？",
        "系统故障或接口中断时怎么办？有没有降级方案？",
    ],
    "r4": [  # 傅景 · 质量安全负责人
        "变更涉及质量或安全风险时，需要走哪些核验和签认？缺了会有什么后果？",
        "有没有因为变更交底不到位引发的质量或安全事件？复盘结论是什么、整改有没有闭环？",
        "哪些环节必须留痕留证？现在的留痕方式（纸质签字、照片、微信群）有什么问题？",
        "从合规角度看，AI 给出的建议能不能直接作为质量或安全判定依据？边界应该划在哪里？",
        "缺依据、缺签、版本冲突的判定清单和停止条件，能否先给出一版草案？",
        "过去一年有没有因变更交底不到位引发的质量或安全事件？复盘结论是什么、整改有没有闭环？",
        "哪些环节属于强制留证项？留痕保留期限组织上有没有明确规定？",
        "哪些操作必须人工审批？谁有权限？能否列一份权限清单？",
    ],
    "r5": [  # 叶书 · 业主项目代表
        "业主方审核签证时最关注什么？最常见的争议点集中在哪些地方？",
        "签证资料提交后，你们内部的审核流程和时限是怎样的？退回最常见的原因是什么？",
        "双方对同一份变更的理解不一致时通常怎么解决？有没有事先书面约定以哪一版为准？",
        "如果系统能自动做版本比对并给出依据引用，你们最想先解决哪个问题？",
        "审批版优先是否已有书面约定？统计周期与排除规则由谁定？",
        "业主方退回签证最常见的原因有哪几类、各占多少？",
        "双方对变更的争议最后一般怎么收场？走争议评审、调解还是诉讼？",
        "你们一个月审多少张签证？平均多久出结论？有没有统计样本？",
        "本次合作的范围边界是什么？哪些事情明确不能自动做？",
        "签证争议引发的投诉或反馈是怎么处理的？",
    ],
}


class Lab:
    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        # 本机有系统代理会拦截 https；实验室是公网地址，强制直连
        self.opener.add_handler(urllib.request.ProxyHandler({}))

    def post(self, path: str, payload: dict, retries: int = 6) -> dict:
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(
                    BASE + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with self.opener.open(req, timeout=180) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e
                # 5xx 为网关抖动，退避后重试；4xx 为业务错误，直接抛
                if e.code < 500:
                    raise
                time.sleep(5 * (i + 1))
            except Exception as e:  # 网络抖动
                last = e
                time.sleep(5 * (i + 1))
        raise RuntimeError(f"请求失败：{path} · {last}")

    def get(self, path: str) -> str:
        with self.opener.open(BASE + path, timeout=120) as r:
            return r.read().decode("utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="construction-change")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)

    lab = Lab()
    state = lab.post("/api/switch", {"id": args.scenario})
    sc = state["scenario"]
    total = sc.get("total", 36)
    chars = sc.get("characters", [])
    print(f"场景：{sc.get('company')} · {sc.get('title')}（目标 {total} 条事实）")

    transcript: list[dict] = []
    done = False
    for c in chars:
        if done:
            break
        cid = c["id"]
        key = "r" + cid.rsplit("-r", 1)[-1]
        for q in QUESTION_BANK.get(key, []):
            before = len(state.get("found") or [])
            try:
                st = lab.post(
                    "/api/ask",
                    {"scenarioId": sc["id"], "characterId": cid, "question": q},
                )
            except Exception as e:  # 单条失败不中断整轮，跳过继续
                print(f"  [跳过] {c.get('name')} 请求失败：{e}")
                continue
            state = st
            hist = st.get("history") or []
            reply = hist[-1].get("text", "") if hist else ""
            new_ids = st.get("newFactIds") or []
            transcript.append(
                {
                    "characterId": cid,
                    "name": c.get("name"),
                    "role": c.get("role"),
                    "question": q,
                    "answer": reply,
                    "newFactIds": new_ids,
                }
            )
            found = len(st.get("found") or [])
            print(f"  [{found}/{total}] {c.get('name')} +{len(new_ids)}  {q[:26]}…")
            # 逐条落盘，避免中途异常丢失全部访谈内容
            (OUT / "访谈记录.json").write_text(
                json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (OUT / "事实清单.json").write_text(
                json.dumps(st.get("found") or [], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if found >= total:
                done = True
                break
            time.sleep(0.4)

    found = len(state.get("found") or [])
    print(f"\n采集完成：{found}/{total}")

    (TMP / "lab_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "访谈记录.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "事实清单.json").write_text(
        json.dumps(state.get("found") or [], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md = lab.get("/api/report?format=md")
    (OUT / "发现报告.md").write_text(md, encoding="utf-8")
    print(f"发现报告已导出：{OUT / '发现报告.md'}（{len(md)} 字符）")


if __name__ == "__main__":
    main()
