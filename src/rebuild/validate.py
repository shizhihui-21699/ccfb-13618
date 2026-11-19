"""输出 PDF 安全校验 + 视觉差异量化（Stage D，可信侧）。

对应 architecture.md §8、§9。任何一项失败都阻断交付并转人工队列。
依赖：pikepdf、scikit-image(SSIM)、numpy、Pillow
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pikepdf
from PIL import Image

# 对象树中只要出现这些键即拒绝：可执行/外联/嵌入文件/表单提交
FORBIDDEN_KEYS = frozenset({
    "/JavaScript", "/JS", "/AA", "/Launch", "/EmbeddedFiles", "/FileSpec",
    "/RichMedia", "/Movie", "/Sound", "/GoToR", "/SubmitForm",
    "/ImportData", "/GoToE", "/MediaClip", "/3D",
})
# OpenAction 只允许内部 GoTo，单独检查
DEFAULT_SSIM_THRESHOLD = 0.99
DEFAULT_PIXEL_DIFF_RATIO = 0.01


@dataclass
class PageDiff:
    page: int
    ssim: float
    pixel_diff_ratio: float
    text_cer: float                      # 文本层字符错误率
    passed: bool


@dataclass
class ValidationReport:
    safe_object_tree: bool = False
    page_count_match: bool = False
    forbidden_hits: list[str] = field(default_factory=list)
    page_diffs: list[PageDiff] = field(default_factory=list)
    blocked_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def scan_object_tree(pdf_path: str) -> list[str]:
    """遍历全部间接对象，返回命中的危险键路径。"""
    hits: list[str] = []
    with pikepdf.open(pdf_path) as pdf:
        for obj_num, obj in enumerate(pdf.objects):
            if not isinstance(obj, pikepdf.Dictionary):
                continue
            for key in obj.keys():
                name = str(key)
                if name in FORBIDDEN_KEYS:
                    hits.append(f"obj {obj_num}: {name}")
                if name == "/OpenAction" and Name.S not in obj.OpenAction:
                    # 非内部 GoTo 的 OpenAction 一律视为危险
                    hits.append(f"obj {obj_num}: /OpenAction(non-GoTo)")
        # 根目录下不允许存在嵌入文件名字树
        if pikepdf.Name.EmbeddedFiles in pdf.Root.get("/Names", {}):
            hits.append("root:/Names/EmbeddedFiles")
    return hits


def _load_gray(path: str) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """结构相似度（生产实现调 skimage.metrics.structural_similarity）。

    这里保留判定骨架；真实实现使用 data_range=1.0、gaussian_weights=True。
    """
    from skimage.metrics import structural_similarity
    return float(structural_similarity(a, b, data_range=1.0, gaussian_weights=True))


def compare_page(reference_png: str, rebuilt_png: str,
                 source_text: str, rebuilt_text: str, page_no: int,
                 *, ssim_limit: float = DEFAULT_SSIM_THRESHOLD,
                 diff_limit: float = DEFAULT_PIXEL_DIFF_RATIO) -> PageDiff:
    ref, out = _load_gray(reference_png), _load_gray(rebuilt_png)
    if ref.shape != out.shape:
        return PageDiff(page_no, 0.0, 1.0, 1.0, False)

    score = ssim(ref, out)
    # 降采样后再算像素差，抑制抗锯齿造成的假性差异（架构文档 §8.1）
    small_ref = ref[::4, ::4]
    small_out = out[::4, ::4]
    diff_ratio = float(np.mean(np.abs(small_ref - small_out) > 12 / 255))
    cer = _char_error_rate(source_text, rebuilt_text)
    return PageDiff(
        page=page_no, ssim=score, pixel_diff_ratio=diff_ratio,
        text_cer=cer,
        passed=score >= ssim_limit and diff_ratio <= diff_limit and cer <= 0.01,
    )


def _char_error_rate(a: str, b: str) -> float:
    """Levenshtein 归一化；生产可换 rapidfuzz.distance.Levenshtein。"""
    if not a:
        return 0.0 if not b else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)


def validate(
    rebuilt_pdf: str,
    expected_pages: int,
    page_pairs: list[tuple[str, str, str, str]],  # (参考图, 重渲染图, 原文, 重建文本)
) -> ValidationReport:
    report = ValidationReport()
    report.forbidden_hits = scan_object_tree(rebuilt_pdf)
    report.safe_object_tree = not report.forbidden_hits

    with pikepdf.open(rebuilt_pdf) as pdf:
        report.page_count_match = len(pdf.pages) == expected_pages

    for i, (ref_png, out_png, src_text, out_text) in enumerate(page_pairs):
        report.page_diffs.append(
            compare_page(ref_png, out_png, src_text, out_text, i)
        )

    if not report.safe_object_tree:
        report.blocked_reason = "对象树存在危险键，拒绝交付"
    elif not report.page_count_match:
        report.blocked_reason = "页数与源文档不一致"
    elif bad := [p for p in report.page_diffs if not p.passed]:
        report.blocked_reason = f"{len(bad)} 页视觉/文本指标低于门槛"
    return report
