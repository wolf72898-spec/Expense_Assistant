"""生成两张流程图：现状流程（人工）与数智化转型后流程。

同一份布局数据同时渲染 PNG（用于文档、汇报）与 SVG（用于网页、GitHub 预览）。
节点文字与证据编号均来自实验室发现报告的六人访谈事实，不自行编造节点。

用法：
    python scripts/gen_flowcharts.py
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon, FancyArrowPatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "fde-交付物"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# 配色
C_OP = "#EAF2FB"      # 操作节点
C_OP_EDGE = "#4A7FB5"
C_JD = "#FDF3E3"      # 判断节点
C_JD_EDGE = "#C58A2B"
C_BR = "#FBEDED"      # 异常分支
C_BR_EDGE = "#B3544F"
C_AI = "#E8F6EE"      # 智能体节点
C_AI_EDGE = "#2E8B57"
C_TEXT = "#1F2A37"
C_GREY = "#6B7280"

MAIN_X = 3.0          # 主列中心
MAIN_W = 5.4
BR_X = 9.7            # 分支列中心
BR_W = 4.6
NODE_H = 1.02
JD_H = 1.22
STEP = 1.62


def op(nid, title, sub="", ev=""):
    return {"id": nid, "kind": "op", "title": title, "sub": sub, "ev": ev}


def jd(nid, title, branch=None, branch_label="否", back_to=None, ev=""):
    return {"id": nid, "kind": "jd", "title": title, "branch": branch,
            "branch_label": branch_label, "back_to": back_to, "ev": ev}


def br(nid, title, sub="", ev=""):
    return {"id": nid, "kind": "br", "title": title, "sub": sub, "ev": ev}


def ai(nid, title, sub="", ev=""):
    return {"id": nid, "kind": "ai", "title": title, "sub": sub, "ev": ev}


# ───────────────────────── 图一：现状流程（人工） ─────────────────────────
CURRENT_NODES = [
    op("01", "现场发现问题并口头上报", "微信/电话上报，说明留在个人便签或群内", "建筑-023 · 建筑-004"),
    op("02", "设计澄清并上传新图", "新图进文档库，无强制交底动作", "建筑-008"),
    jd("03", "现场是否已收到最新版本\n并完成交底？", branch="04", ev="建筑-004"),
    br("04", "现场按旧图继续施工", "事后返工，月均返工损失 45 万元", "建筑-004 · 建筑-001"),
    op("05", "变更审批（项目经理 / 业主）", "变更确认平均 12 天，目标 6 天", "建筑-002 · 建筑-001"),
    op("06", "施工交底", "口头交底为主，未强制留痕", "建筑-023"),
    op("07", "签证计量与编制", "现场按完成量、造价按签证量、业主按审批版核价", "建筑-016 · 建筑-034"),
    jd("08", "三套计量口径能否对齐？", branch="09", ev="建筑-034 · 建筑-011"),
    br("09", "人工翻合同与图纸核对", "图纸编码不统一，发生日与确认日混用", "建筑-011 · 建筑-010"),
    op("10", "提交业主审核", "业主关注核价口径与依据完整性", "建筑-016"),
    jd("11", "依据与授权链是否完整？", branch="12", ev="建筑-005"),
    br("12", "口头增项被拒 / 退回补证", "无法确认授权链，事后难以追认", "建筑-005"),
    op("13", "造价整理与质量安全签认", "造价可整理校核，专业签核须由质量安全负责人完成", "建筑-009 · 建筑-013"),
    jd("14", "是否存在未闭合项？", branch="15", ev="建筑-022"),
    br("15", "关键字段人工复核", "高峰期离开现场补录会延误，说明靠事后回忆补进", "建筑-022 · 建筑-023"),
    jd("16", "正式结论是否经授权人员确认？", back_to="15", ev="建筑-013"),
    op("17", "结算与资料归档", "资料分散在 BIM 库、项目平台、造价软件，无统一视图", "建筑-010"),
]

# ───────────────────────── 图二：数智化转型后流程 ─────────────────────────
FUTURE_NODES = [
    op("01", "现场上报（终端暂存草稿）", "离场不丢数据，显示最后同步时间", "建筑-022"),
    ai("02", "版本核对智能体", "自动比对图纸版本与交底状态，提示版本不一致", "建筑-021 · 建筑-003"),
    jd("03", "是否检出版本不一致\n或未交底？", branch="04", branch_label="是", ev="建筑-003"),
    br("04", "阻断并推送交底提醒", "转人工处理，标注触发原因、接手人与确认时间", "建筑-032"),
    ai("05", "依据与授权链预检智能体", "校验主编号、事件时间、版本字段；歧义进人工修复队列", "建筑-028 · 建筑-033"),
    jd("06", "依据是否完整、授权链是否清晰？", branch="07", ev="建筑-032"),
    br("07", "阻断提交并转人工", "只提示不锁定会被绕过，缺依据一律不得进入现场", "建筑-032 · 建筑-044"),
    op("08", "变更审批（人工）", "系统只附依据引用，不代批、不代签", "建筑-012 · 建筑-021"),
    ai("09", "计量口径归一与差异定位", "三套口径按规则映射，差异点定位到字段", "建筑-034 · 建筑-016"),
    op("10", "造价编制辅助（人工校核）", "附依据来源、版本与时间；计价规则冲突不自行合并", "建筑-026 · 建筑-009"),
    op("11", "业主审核（人工）", "系统提供版本比对结果与依据引用", "建筑-016"),
    jd("12", "是否出现规则冲突？", branch="13", branch_label="是", ev="建筑-026"),
    br("13", "附两个版本交质量安全负责人", "不自行合并、不选较宽松条款", "建筑-026"),
    op("14", "质量安全签认（人工，不可替代）", "结构安全与设计变更须由合格人员审核", "建筑-013"),
    jd("15", "全部阻断项是否已清零？", back_to="07", ev="建筑-032"),
    ai("16", "留痕归档与过程计量", "保留业务编号、资料版本、事实引用、建议结果、人工改动、签核人", "建筑-033 · 建筑-031"),
    op("17", "结算与节点耗时计量", "按六节点分别记录等待与处理时间，验收 6 天目标", "建筑-001 · 建筑-002"),
]

ROLES = [
    ("顾明岚 · 项目经理", "业务目标、试点范围与预算（建筑-001 / 002 / 003 / 021）"),
    ("林亦舟 · 现场施工员", "一线操作与异常上报（建筑-004 / 005 / 022 / 023）"),
    ("唐映秋 · 造价工程师", "流程规则、计量口径与责任交接（建筑-007 / 008 / 009 / 026 / 027）"),
    ("秦芮 · 工程平台工程师", "系统、接口与数据口径治理（建筑-010 / 011 / 012 / 028）"),
    ("傅景 · 质量安全负责人", "签核权限、留痕与停止条件（建筑-013 / 031 / 032 / 033）"),
    ("叶书 · 业主项目代表", "业主核价口径与争议处理（建筑-016 / 034）"),
]

NOTES = [
    "实线为顺序与判断分支，虚线为补充信息或整改后返回。",
    "菱形为依据访谈口述整理的人工判断点，并非系统自动判定；阈值、时限与校验名单仍待现场核实。",
    "图中标出的职责与留痕要求不等于已落实，是否形成闭环仍需在试点中验证。",
]


def draw(ax, nodes, y_top):
    """按主列顺序绘制节点，返回 {id: (cx, cy, kind)}"""
    pos = {}
    cy = y_top
    for i, n in enumerate(nodes):
        kind = n["kind"]
        if kind == "br":
            continue  # 分支节点位置由判断节点决定
        h = JD_H if kind == "jd" else NODE_H
        cx = MAIN_X
        pos[n["id"]] = (cx, cy, kind)
        _shape(ax, n, cx, cy, MAIN_W, h)
        # 顺序箭头
        nxt = nodes[i + 1] if i + 1 < len(nodes) else None
        if nxt and nxt["kind"] != "br":
            ax.annotate(
                "", xy=(cx, cy - h / 2 - 0.16), xytext=(cx, cy - (JD_H if kind == "jd" else NODE_H) / 2),
                arrowprops=dict(arrowstyle="-|>", color=C_GREY, lw=1.4),
            )
        cy -= STEP

    # 分支节点与连线
    for n in nodes:
        if n["kind"] not in ("jd",):
            continue
        if not n.get("branch") and not n.get("back_to"):
            continue
        jx, jy, _ = pos[n["id"]]
        if n.get("branch"):
            b = next(x for x in nodes if x["id"] == n["branch"])
            bh = NODE_H
            bx, by = BR_X, jy
            pos[b["id"]] = (bx, by, "br")
            _shape(ax, b, bx, by, BR_W, bh)
            # 判断 → 分支（横向）
            ax.annotate(
                n.get("branch_label", "否"),
                xy=(bx - BR_W / 2 - 0.12, by), xytext=(jx + MAIN_W / 2 + 0.12, jy),
                arrowprops=dict(arrowstyle="-|>", color=C_BR_EDGE, lw=1.4,
                                connectionstyle="arc3,rad=0"),
                fontsize=9, color=C_BR_EDGE, va="center", ha="right",
            )
        if n.get("back_to") and n["back_to"] in pos:
            tx, ty, _ = pos[n["back_to"]]
            # 返回路径走右侧绕行（正交折线），避免斜穿主列节点
            lane = 12.32   # 绕行车道必须在分支框外侧，否则虚线会压住框内文字
            t_right = tx + (BR_W if pos[n["back_to"]][2] == "br" else MAIN_W) / 2
            for (x0, y0), (x1, y1) in [
                ((jx + MAIN_W / 2, jy), (lane, jy)),
                ((lane, jy), (lane, ty)),
            ]:
                ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                            arrowprops=dict(arrowstyle="-", color=C_GREY, lw=1.2,
                                            linestyle="dashed"))
            ax.annotate("", xy=(t_right + 0.1, ty), xytext=(lane, ty),
                        arrowprops=dict(arrowstyle="-|>", color=C_GREY, lw=1.2,
                                        linestyle="dashed"))
            ax.text(lane + 0.08, (jy + ty) / 2, f"否，返回 {n['back_to']}",
                    fontsize=8.5, color=C_GREY, va="center")
    return pos


def _shape(ax, n, cx, cy, w, h):
    kind = n["kind"]
    if kind == "jd":
        poly = Polygon(
            [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)],
            closed=True, facecolor=C_JD, edgecolor=C_JD_EDGE, lw=1.6, zorder=3,
        )
        ax.add_patch(poly)
        ax.text(cx, cy + 0.06, n["title"], ha="center", va="center",
                fontsize=10.5, color=C_TEXT, zorder=4, linespacing=1.35)
        if n.get("ev"):
            ax.text(cx, cy - h / 2 - 0.14, n["ev"], ha="center", va="top",
                    fontsize=8, color=C_GREY)
        return

    if kind == "op":
        fc, ec = C_OP, C_OP_EDGE
    elif kind == "ai":
        fc, ec = C_AI, C_AI_EDGE
    else:
        fc, ec = C_BR, C_BR_EDGE

    ax.add_patch(FancyBboxPatch(
        (cx - w / 2, cy - h / 2), w, h, boxstyle="round,pad=0.03,rounding_size=0.16",
        facecolor=fc, edgecolor=ec, lw=1.6, zorder=3,
    ))
    tag = "智能体" if kind == "ai" else ""
    ax.text(cx - w / 2 + 0.16, cy + h / 2 - 0.2, n["id"], fontsize=11,
            fontweight="bold", color=ec, va="center", zorder=4)
    if tag:
        ax.text(cx + w / 2 - 0.16, cy + h / 2 - 0.2, tag, fontsize=8.5,
                color=ec, ha="right", va="center", zorder=4)
    ax.text(cx - w / 2 + 0.68, cy + 0.14, n["title"], fontsize=10.5,
            fontweight="bold", color=C_TEXT, va="center", zorder=4)
    if n.get("sub"):
        ax.text(cx - w / 2 + 0.68, cy - 0.24, n["sub"], fontsize=8.5,
                color=C_GREY, va="center", zorder=4)
    if n.get("ev"):
        ax.text(cx + w / 2 - 0.16, cy - h / 2 + 0.18, n["ev"], fontsize=7.5,
                color=C_GREY, ha="right", va="center", zorder=4)


def legend(ax, x, y, with_ai: bool):
    items = [("操作 / 人工处理", C_OP, C_OP_EDGE), ("人工判断点", C_JD, C_JD_EDGE),
             ("异常分支 / 阻断", C_BR, C_BR_EDGE)]
    if with_ai:
        items.append(("智能体（自动）", C_AI, C_AI_EDGE))
    for i, (label, fc, ec) in enumerate(items):
        ax.add_patch(FancyBboxPatch(
            (x + i * 3.0, y), 0.5, 0.3, boxstyle="round,pad=0.02,rounding_size=0.06",
            facecolor=fc, edgecolor=ec, lw=1.3, zorder=5,
        ))
        ax.text(x + i * 3.0 + 0.62, y + 0.15, label, fontsize=9, color=C_TEXT,
                va="center", zorder=5)


def render(nodes, title, subtitle, scope, out_stem, with_ai=False):
    y_top = -1.0
    main_count = len([n for n in nodes if n["kind"] != "br"])
    bottom = y_top - main_count * STEP - 1.2
    fig_h = (1.0 - bottom) + 5.2
    fig, ax = plt.subplots(figsize=(14.4, fig_h * 0.62))
    ax.set_xlim(-0.4, 13.6)
    ax.set_ylim(bottom - 3.4, 1.4)
    ax.axis("off")

    # 标题区
    ax.text(0.0, 1.15, title, fontsize=17, fontweight="bold", color="#1C5FB0", va="center")
    ax.text(0.0, 0.72, subtitle, fontsize=10.5, color=C_GREY, va="center")
    ax.add_patch(FancyBboxPatch(
        (0.0, 0.02), 13.0, 0.5, boxstyle="round,pad=0.02,rounding_size=0.08",
        facecolor="#F5F7FA", edgecolor="#D5DCE5", lw=1.0, zorder=1,
    ))
    ax.text(0.18, 0.27, scope, fontsize=9.5, color=C_TEXT, va="center", zorder=2)

    draw(ax, nodes, y_top)

    # 图例
    legend(ax, 0.0, bottom + 0.55, with_ai)

    # 角色职责
    ry = bottom + 0.05
    ax.text(0.0, ry, "六位访谈对象在流程中的信息与职责", fontsize=11.5,
            fontweight="bold", color="#1C5FB0", va="center")
    for i, (name, duty) in enumerate(ROLES):
        yy = ry - 0.34 - i * 0.30
        ax.text(0.05, yy, f"· {name}", fontsize=9.5, color=C_TEXT, va="center")
        ax.text(3.6, yy, duty, fontsize=9, color=C_GREY, va="center")

    # 读图说明
    ny = ry - 0.34 - len(ROLES) * 0.30 - 0.32
    ax.text(0.0, ny, "读图说明", fontsize=11.5, fontweight="bold",
            color="#1C5FB0", va="center")
    for i, s in enumerate(NOTES):
        ax.text(0.05, ny - 0.32 - i * 0.28, f"{i + 1}. {s}", fontsize=9,
                color=C_GREY, va="center")
    ax.text(0.05, ny - 0.32 - len(NOTES) * 0.28 - 0.30,
            "证据编号形如 建筑-NNN，来源：实验室发现报告 discovery-report；"
            "事实为合成场景访谈所得，需求为待验证假设。",
            fontsize=8, color="#9AA3AF", va="center")

    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / f"{out_stem}.png"
    svg = OUT / f"{out_stem}.svg"
    fig.savefig(str(png), dpi=170, bbox_inches="tight", facecolor="white")
    fig.savefig(str(svg), bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"已生成 {png.name} / {svg.name}")


def main() -> None:
    n_op = len([n for n in CURRENT_NODES if n["kind"] == "op"])
    n_jd = len([n for n in CURRENT_NODES if n["kind"] == "jd"])
    n_br = len([n for n in CURRENT_NODES if n["kind"] == "br"])
    render(
        CURRENT_NODES,
        "远筑工程 · 工程变更与签证协同（现状）",
        f"六人访谈口述现状流程　{len(CURRENT_NODES)} 个节点 · {n_op} 个操作 · "
        f"{n_jd} 个判断 · {n_br} 个异常分支：人工处理与交接",
        "范围：现场问题 → 设计澄清 → 变更审批 → 施工交底 → 签证计量 → 结算；"
        "版本未核对、授权链与依据不完整时依赖人工判断或事后补证。",
        "03-现状流程图",
    )

    f_ai = len([n for n in FUTURE_NODES if n["kind"] == "ai"])
    f_op = len([n for n in FUTURE_NODES if n["kind"] == "op"])
    f_jd = len([n for n in FUTURE_NODES if n["kind"] == "jd"])
    f_br = len([n for n in FUTURE_NODES if n["kind"] == "br"])
    render(
        FUTURE_NODES,
        "远筑工程 · 工程变更与签证协同（数智化转型后）",
        f"{len(FUTURE_NODES)} 个节点 · {f_ai} 个智能体 · {f_op} 个人工环节 · "
        f"{f_jd} 个判断 · {f_br} 个阻断分支：自动校验在前，人工签认兜底",
        "范围：与现状同一业务链；新增版本核对、依据预检、口径归一与留痕归档四类智能体，"
        "审批、签核与结算仍由人工完成。",
        "04-数智化转型后流程图",
        with_ai=True,
    )


if __name__ == "__main__":
    main()
