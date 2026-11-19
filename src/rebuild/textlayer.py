"""双层 PDF 组装：可见图像层 + 不可见文本层，含竖排逐字定位。

对应 architecture.md §4。运行在沙箱外的可信进程（Stage C）。
依赖（镜像内固定版本）：
    pypdfium2  渲染与文本/bbox 抽取（沙箱内 Stage B 使用）
    pikepdf    受控组装
    Pillow     位图压缩
"""
from __future__ import annotations

from dataclasses import dataclass

import pikepdf
from pikepdf import Name, Pdf

# 不可见文本层使用的嵌入字体。Identity-H + 逐字 Tm 定位，
# 不依赖阅读器对竖排 writing mode 的支持差异（见架构文档 §4.3）。
CJK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# 竖排判定：连续字符沿 y 轴的推进显著大于 x 轴即视为竖排
VERTICAL_RUN_MIN_CHARS = 3


@dataclass(frozen=True)
class TextGlyph:
    codepoint: str
    x0: float
    y0: float
    x1: float
    y1: float          # pdfium 坐标原点在左下角，组装时按页高换算
    page_height: float


def is_vertical_run(glyphs: list[TextGlyph]) -> bool:
    if len(glyphs) < VERTICAL_RUN_MIN_CHARS:
        return False
    dy = sum(abs(glyphs[i + 1].y0 - glyphs[i].y0) for i in range(len(glyphs) - 1))
    dx = sum(abs(glyphs[i + 1].x0 - glyphs[i].x0) for i in range(len(glyphs) - 1))
    return dy > dx * 2


def _invisible_text_stream(glyphs: list[TextGlyph]) -> bytes:
    """生成 Tr 3（不可见）文本内容流，逐字 Tm 定位。

    真实实现中字符串按 Identity-H 编码为双字节 CID；这里保留结构与
    安全要点：文本只承载码点与位置，不继承原文档任何字体/动作对象。
    """
    lines = ["BT", "/F1 1 Tf", "3 Tr"]  # 3 Tr = 不可见渲染模式
    for g in glyphs:
        height = g.page_height
        # pdfium 左下原点 -> PDF 文本矩阵；竖排由逐字定位自然体现，
        # 无需 WMode 声明
        lines.append(f"1 0 0 1 {g.x0:.2f} {height - g.y1:.2f} Tm")
        # 生产代码：codepoint -> font cmap 查 CID -> <hhhh> Tj
        lines.append(f"<{ord(g.codepoint):04x}> Tj")
    lines.append("ET")
    return "\n".join(lines).encode("ascii")


def build_clean_pdf(
    page_images: list[str],
    page_glyphs: list[list[TextGlyph]],
    output_path: str,
) -> None:
    """全新写文件（非 incremental save），杜绝原签名字节段残留。

    page_images: 每页渲染位图路径（200/300 DPI，Flate/JPEG 自适应压缩）
    page_glyphs: 每页抽取的字形 bbox（扫描件则来自 Tesseract OCR）
    """
    if len(page_images) != len(page_glyphs):
        raise ValueError("图像页数与文本页数不一致，拒绝组装")

    out = Pdf.new()
    font = pikepdf.open(CJK_FONT_PATH)  # 本地可信字体，非文档内嵌字体
    for image_path, glyphs in zip(page_images, page_glyphs):
        page = out.add_blank_page(size=(595, 842))  # 尺寸由渲染元数据覆盖
        # 1) 图像层：整页位图，视觉 1:1；矢量攻击面在此物理消除
        page.add_overlay(page=out.make_page_with_image(image_path))
        # 2) 文本层：不可见、仅码点+位置，保证选中/搜索/读屏
        stream = pikepdf.Stream(out, _invisible_text_stream(glyphs))
        page.add_overlay(pikepdf.Page(out.make_indirect(
            pikepdf.Dictionary(
                Type=Name.Page,
                Contents=out.make_indirect(stream),
                Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
            )
        )))

    # 显式不带 AcroForm/Names/OpenAction/Metadata（除非调用方另行安全构建）
    out.save(
        output_path,
        linearize=False,
        deterministic_id=True,   # 相同输入产出可复现 ID，便于审计 diff
    )


# ---------------------------------------------------------------------------
# 沙箱内 Stage B 的抽取侧（伪代码，真实 worker 用 pypdfium2 实现）：
#
#   import pypdfium2 as pdfium
#   pdf = pdfium.PdfDocument(input_path, password=...)  # 密码走环境变量
#   for i in range(page_start, page_end):
#       page = pdf[i]
#       bitmap = page.render(scale=200/72)               # 200 DPI 图像层
#       textpage = page.get_textpage()
#       for obj in textpage.get_text_objects():
#           x0, y0, x1, y1 = obj.get_pos()              # 字符/span bbox
#           glyphs.append(TextGlyph(obj.get_text(), x0, y0, x1, y1,
#                                   page.get_height()))
#       # 同时抽取：批注类型/坐标/内容、链接（仅 http/https/mailto）、
#       # 书签页码、签名字段存在性 —— 全部以 JSON 形式作为纯数据输出
# ---------------------------------------------------------------------------
