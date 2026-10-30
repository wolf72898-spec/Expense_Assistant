"""生成《建筑业工程变更与签证协同 · 行业分析报告》docx。

所有数字均来自公开检索结果，脚本内标注了出处；不同机构口径不一致的，
在正文中并列给出并注明差异，不擅自取单一数值充当结论。
"""

from __future__ import annotations

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "fde-交付物" / "02-行业分析报告-建筑业工程变更与签证协同.docx"

ACCENT = RGBColor(0x1C, 0x5F, 0xB0)
GREY = RGBColor(0x60, 0x60, 0x60)


def set_font(run, name: str = "微软雅黑", size: int = 10.5, bold=False, color=None):
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def para(doc, text, size=10.5, bold=False, color=None, space_after=6,
         align=None, indent=0):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    if indent:
        p.paragraph_format.left_indent = Cm(indent)
    p.paragraph_format.space_after = Pt(space_after)
    r = p.add_run(text)
    set_font(r, size=size, bold=bold, color=color)
    return p


def bullet(doc, text, size=10.5, level=0):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.left_indent = Cm(0.75 + level * 0.6)
    p.paragraph_format.space_after = Pt(3)
    r = p.add_run(text)
    set_font(r, size=size)
    return p


def heading(doc, text, level=1):
    h = doc.add_heading(level=level)
    h.paragraph_format.space_before = Pt(14 if level == 1 else 10)
    h.paragraph_format.space_after = Pt(6)
    r = h.add_run(text)
    set_font(r, name="微软雅黑", size=16 if level == 1 else 13, bold=True, color=ACCENT)
    return h


def table(doc, headers, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = t.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ""
        r = hdr[i].paragraphs[0].add_run(h)
        set_font(r, size=9.5, bold=True)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            r = cells[i].paragraphs[0].add_run(str(v))
            set_font(r, size=9.5)
    if widths:
        for i, w in enumerate(widths):
            for row in t.rows:
                row.cells[i].width = Cm(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def caption(doc, text):
    para(doc, text, size=8.5, color=GREY, space_after=10)


def build() -> None:
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.4)

    # ── 封面 ──
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(60)
    r = p.add_run("建筑业工程变更与签证协同")
    set_font(r, size=26, bold=True, color=ACCENT)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("行业分析报告")
    set_font(r, size=18, bold=True, color=ACCENT)

    para(doc, "", space_after=18)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("远筑工程 · 工程变更与签证协同 AI 试点项目")
    set_font(r, size=12, color=GREY)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("配套交付物之一 · 用于业务挖掘阶段的行业基准对齐")
    set_font(r, size=10, color=GREY)

    para(doc, "", space_after=40)
    table(
        doc,
        ["项目", "内容"],
        [
            ["报告用途", "为「工程变更与签证协同」AI 试点提供外部基准与政策约束"],
            ["分析范围", "建筑业数字化、工程造价计价规则、变更与签证管理现状"],
            ["数据截止", "2026 年 10 月（公开检索所得最新数据）"],
            ["重要说明", "不同机构统计口径不一致处已并列标注，未做单一取值"],
        ],
        widths=[3.2, 12.0],
    )

    doc.add_page_break()

    # ── 一、结论摘要 ──
    heading(doc, "一、结论摘要", 1)
    para(doc,
         "本报告服务于远筑工程「工程变更与签证协同」的 AI 试点立项。核心判断有三条：")
    bullet(doc, "行业侧：建筑业总量进入存量阶段，2025 年总产值 30.38 万亿元、同比 -5.43%，"
                "首次出现年度负增长；降本增效从「可选项」变成「生存题」。")
    bullet(doc, "政策侧：GB/T 50500-2024《建设工程工程量清单计价标准》自 2025 年 9 月 1 日实施，"
                "删除原「工程签证」条款、新增工程变更计量规则，并推行施工过程结算。"
                "这意味着任何按旧口径建设的系统都面临规则失效风险。")
    bullet(doc, "能力侧：行业 AI 渗透率在设计与安全环节已突破 35%，但真正「数据驱动决策」的"
                "项目不足 4.2%。缺口不在模型，而在图纸版本、主编号、事件时间这些"
                "基础口径的统一与依据可追溯。")
    para(doc,
         "由此，本项目不建议直接上「自动审批 / 自动计价」，而应先做版本核对与缺依据提示"
         "（只读、可回退），把口径与留痕地基打好，再谈阻断与集成。",
         space_after=10)

    # ── 二、行业规模 ──
    heading(doc, "二、行业规模与数字化水平", 1)
    para(doc, "建筑业是国民经济的支柱产业，但数字化水平长期落后于制造业。下表汇总公开数据，"
              "同一指标存在多套口径，已在备注中说明。")

    table(
        doc,
        ["指标", "数值", "来源 / 口径说明"],
        [
            ["建筑业总产值", "30.38 万亿元（2025）\n同比 -5.43%，首次年度负增长",
             "中国建筑业协会。另有机构按 2026 年突破 31 万亿、+5.2% 口径统计，方向相反，需以官方年鉴为准"],
            ["建筑业信息化率", "0.1%（欧美约 1%）",
             "中国建筑业协会 2018 年数据，口径较旧但行业引用广泛"],
            ["行业数字化渗透率", "13.1%（较制造业低 17.8pct）\n另有 6.5% 口径",
             "前者为 2026 年行业数据；后者为「建筑业整体渗透率」口径，与电力 35%、航天 42% 对比"],
            ["建筑信息化市场规模", "303 亿元 → 1076 亿元\n（2020→2025，CAGR 约 24%）",
             "新点软件招股书引用"],
            ["BIM 市场规模", "约 194 亿元（2025）\n216.8 亿元（全链条口径）\n480 亿元（2026 预测）",
             "招商证券 / 共研产业研究院 / 市场研究机构。全链条口径含造价咨询等服务，差异主要来自统计边界"],
            ["BIM 应用率 vs 普及率", "应用率 74.1%\n常态化普及率约 22%",
             "应用率＝至少用过一次的项目占比；普及率＝适宜项目中常态化使用比例。房建大型项目使用率超 90%"],
            ["智能建造", "24 个试点城市（2022-2025）\n2026 年为全面推广元年",
             "住建部联合 13 部门；已发布 4 批可复制经验做法清单"],
            ["AI 渗透率", "设计审查 / 施工安全环节突破 35%\n施工管理大模型 29%\n数据驱动决策项目不足 4.2%",
             "住建部、中国建筑业协会 2026 年数据。最后一项是关键缺口"],
        ],
        widths=[3.0, 4.6, 7.6],
    )
    caption(doc, "数据来源：招商证券、赛迪顾问、中国建筑业协会、住建部，经公开检索汇总。"
                 "不同机构口径差异较大，对外引用前建议回溯原始报告核对。")

    para(doc, "三点解读：", bold=True, space_after=4)
    bullet(doc, "总量收缩 + 利润率长期 2%~4%，决定客户不会为「锦上添花」的功能付费，"
                "只认能够直接指向返工损失与结算周期的改善。")
    bullet(doc, "信息化率 0.1% 与渗透率 13.1% 的巨大落差说明：行业买了很多软件，"
                "但数据没有真正流动起来。这正是「版本不一致、编号不统一」的根源。")
    bullet(doc, "BIM 应用率高而普及率低，意味着多数项目只是「用过」，"
                "变更发生时模型并未同步更新，反而制造了新的版本混乱。")

    # ── 三、政策环境 ──
    heading(doc, "三、政策与计价规则变化（最关键变量）", 1)
    para(doc, "2024 年 12 月 30 日，住建部发布国家标准《建设工程工程量清单计价标准》"
              "GB/T 50500-2024，自 2025 年 9 月 1 日起实施，原 GB 50500-2013 同时废止。"
              "这不是一次技术性修订，而是计价逻辑的转向，对本项目有直接约束。")

    table(
        doc,
        ["变化", "内容", "对本项目的影响"],
        [
            ["性质由强制转推荐", "由 GB 强制性标准转为 GB/T 推荐性标准，定价权交还市场",
             "规则来源从「定额条文」变为「合同约定 + 市场数据」，系统必须能同时挂接合同版本与市场价"],
            ["删除「工程签证」条款", "新增工程变更计量规则，不再单列签证章节",
             "术语与流程须整体重构：系统字段、审批单名称、统计口径不能沿用旧分类"],
            ["计价依据市场化", "最高投标限价不再以政府定额、信息价为主要依据，改用市场询价、造价数据库",
             "计价依据需要标注来源与时间，AI 给建议时必须附依据引用，不能只给数字"],
            ["风险分担原则", "「谁的责任谁承担、谁的风控能力强谁承担」",
             "责任归属要能追溯到岗和人，留痕字段必须包含签核人与时间"],
            ["推行施工过程结算", "按约定节点及时结算，发包人可随时掌握预计结算价",
             "变更必须实时可计量，滞后补录的签证会直接造成结算偏差"],
            ["多元解纷机制", "建立争议评审、调解、仲裁或诉讼机制，倡导以争议评审为核心",
             "争议材料需完整可复现，「依据完整率」成为可验收指标"],
        ],
        widths=[3.0, 5.4, 6.8],
    )
    caption(doc, "来源：住建部公告（2024-12-30）、无锡市住建局实施意见、陕西省住建厅转载《中国建设报》解读。"
                 "江苏省自 2026 年 6 月 1 日起全省正式执行，其他地区以当地公告为准。")

    para(doc, "其他相关政策：", bold=True, space_after=4)
    bullet(doc, "住建部联合 13 部门明确 2026 年为智能建造全面推广元年，24 个试点城市已完成 3 年试点。")
    bullet(doc, "19 个省市已出台 BIM 计费标准，BIM 应用在很大程度上仍依赖政策强制推动。")
    bullet(doc, "住建部、国家数据局推进房屋建筑统一代码制度，目标 2027 年底新建房屋全覆盖——"
                "与本项目「统一主编号」的诉求方向一致。")

    # ── 四、痛点量化 ──
    heading(doc, "四、痛点与成本量化", 1)
    para(doc, "变更与签证是打破合同造价边界、引发结算纠纷的核心诱因。公开资料给出的量化口径如下：")

    table(
        doc,
        ["痛点", "量化口径", "备注"],
        [
            ["变更签证推高造价", "民用建筑项目变更签证引发造价增幅平均 8.3%\n中小民营项目违规签证导致超预算比例突破 15%",
             "引自住建部 2025 年建筑结算数据统计（期刊转引），建议在正式立项材料中回溯原件"],
            ["结算争议构成", "工程量认定差异 45%\n材料价差调整 25%\n质量扣款 15%\n措施费计取 10%\n工期延误索赔 5%",
             "行业实务统计。工程量认定（尤其是隐蔽工程与变更部位）是第一大争议源"],
            ["设计变更占超支比重", "某审计案例中设计修改与二次深化净增造价 622 万元，占超合同部分 42.1%\n签证合计 143 万元，占 9.7%",
             "单一项目案例，用于说明结构占比，不代表行业均值"],
            ["材料价格波动", "建材价格季度波动幅度维持 6%~10%",
             "滞后审批的变更签证无法匹配当期材料认价，进一步拉大造价缺口"],
            ["签证管理混乱", "盲目签证、重复签证造成诉讼中事实认定困难；签证不及时、要素不齐全",
             "福建高院建设工程合同纠纷审判白皮书（2019-2021）、辽宁法院审判梳理"],
        ],
        widths=[3.0, 5.6, 6.6],
    )

    para(doc, "结合远筑工程访谈所得的一线事实，上述行业级痛点在本项目中的具体表现为：", space_after=4)
    bullet(doc, "版本与交底脱节：设计新图已上传但无人交底，现场仍按旧图施工。")
    bullet(doc, "授权链缺失：口头要求增加管线，事后签证被拒，无法确认授权链。")
    bullet(doc, "口径不统一：图纸编码不统一、签证发生日与确认日混用、现场 / 造价 / 业主三套计量口径并存。")
    bullet(doc, "关键说明不随单据交接：说明留在个人便签或微信群，事后难以重建处理链。")

    # ── 五、供给格局 ──
    heading(doc, "五、供给格局与竞品", 1)
    para(doc, "国内建筑数字化供给已相对集中，但各家强项与本项目诉求并不完全重合。")
    table(
        doc,
        ["供给方", "主要能力", "与本项目诉求的差距"],
        [
            ["广联达", "造价算量、BIM 平台、数字施工；已推出「天工智算」AI 成本测算与决策助手",
             "强在造价计算与建模，弱在跨角色协同链的「谁交底、谁签认」追溯"],
            ["用友", "项目管理、ERP、财务一体化",
             "偏企业管理与财务口径，现场一线的即时性不足"],
            ["鲁班软件", "BIM 建模与协同",
             "以模型为中心，变更发生时的模型同步依赖人工"],
            ["头部厂商整体", "国内头部（广联达、用友、鲁班等）合计约 58% 市场份额；国产造价类 BIM 软件市占率超 80%",
             "市场集中度高，但变更—签证跨角色协同与依据溯源仍是普遍薄弱环节"],
            ["企业自研案例", "中国建筑推进 270 余项数字化场景；陕建物流「灵镜」大模型用于供应商管理与风险防控",
             "大企业自研为主，中型施工企业缺少可负担的轻量方案"],
        ],
        widths=[2.6, 6.2, 6.4],
    )
    caption(doc, "来源：公开行业研究汇总。份额与市占率为研究机构估算，非官方统计。")

    # ── 六、对本项目的启示 ──
    heading(doc, "六、对本项目的启示", 1)
    para(doc, "把行业基准与远筑工程的现场事实对照，可以推出四条对项目设计有直接约束的结论：")
    bullet(doc, "术语必须先对齐再建系统。新清单已删除「签证」条款，若系统固化旧分类，"
                "上线即与现行规则冲突。建议字段设计同时保留合同口径与新版计量口径。")
    bullet(doc, "先做只读、可回退的能力。行业数据驱动决策项目不足 4.2%，"
                "说明一次性上自动审批的失败概率高；版本核对与缺依据提示属于低不可逆风险切入点。")
    bullet(doc, "依据引用是刚需而非加分项。计价依据市场化之后，"
                "「给数字」没有价值，「给数字 + 给依据来源与时间」才有价值。")
    bullet(doc, "留痕要能重建整条处理链。争议评审机制要求材料完整可复现，"
                "留痕字段至少覆盖业务编号、资料版本、事实引用、建议结果、人工改动与签核人。")

    para(doc, "上述四条与实验室发现报告中的 REQ-1（版本核对与交底缺失提示）、"
              "REQ-2（缺依据或授权链不清时阻断）、REQ-3（统一主编号与事件时间口径）、"
              "REQ-6（审计留痕覆盖建议、人工改动与签核人）相互印证，"
              "构成后续设计方案与报价的立项依据。", space_after=10)

    # ── 七、数据来源 ──
    heading(doc, "七、数据来源与口径声明", 1)
    bullet(doc, "住建部《建设工程工程量清单计价标准》公告（2024-12-30 发布，2025-09-01 实施）。")
    bullet(doc, "无锡市住建局《（GB/T 50500-2024）及 9 本工程量计算标准的实施意见》解读（2026-05）。")
    bullet(doc, "陕西省住建厅转载《中国建设报》：新清单促进工程造价市场化改革（2025-12）。")
    bullet(doc, "福建省高级人民法院《涉建设工程合同纠纷审判白皮书（2019-2021 年）》。")
    bullet(doc, "中国建筑业协会、赛迪顾问、招商证券、住建部相关统计（经公开检索汇总）。")
    para(doc, "口径声明：本报告中的市场规模、渗透率、产值与增速，"
              "来自不同机构的公开报告，统计边界与取样范围不一致，同一指标出现多个数值时已并列标注，"
              "未做加权或单一取值。对外正式引用前，请回溯原始报告核对。",
         size=9.5, color=GREY)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    print(f"已生成：{OUT}")


if __name__ == "__main__":
    build()
