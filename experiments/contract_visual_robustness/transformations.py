"""可复现的 PDF 视觉扰动生成器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz


@dataclass(frozen=True, slots=True)
class TransformationResult:
    name: str
    source_page_count: int
    transformed_page_count: int
    details: str


def transform_pdf_sync(source_pdf: Path, target_pdf: Path) -> TransformationResult:
    """多页删最后一页；单页顺时针 90° 并横向缩小 10%。"""

    source = fitz.open(source_pdf)
    try:
        count = source.page_count
        if count < 1:
            raise ValueError("源 PDF 不包含页面。")
        if count > 1:
            source.delete_page(count - 1)
            source.save(target_pdf, garbage=4, deflate=True)
            return TransformationResult("delete_last_page_v1", count, count - 1, "删除原合同最后一页。")
        rect = source.load_page(0).rect
        output = fitz.open()
        try:
            page = output.new_page(width=rect.width, height=rect.height)
            page.show_pdf_page(
                fitz.Rect(rect.width * 0.05, 0, rect.width * 0.95, rect.height),
                source, 0, rotate=90, keep_proportion=False,
            )
            output.save(target_pdf, garbage=4, deflate=True)
        finally:
            output.close()
        return TransformationResult(
            "rotate_90_clockwise_scale_x_0.9_y_1.0_v1", 1, 1,
            "顺时针旋转 90°，横向缩小 10%，纵向保持 100%。",
        )
    finally:
        source.close()
