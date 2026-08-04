"""合同 PDF 的统一全页图像渲染。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import fitz


def _render_pdf_pages_sync(
    pdf_path: Path, *, max_pages: int
) -> tuple[list[dict[str, Any]], int]:
    """一次性渲染全部物理页，超过上下文上限时明确失败。"""

    document = fitz.open(pdf_path)
    try:
        page_count = document.page_count
        if page_count < 1:
            raise ValueError("PDF 不包含可渲染页面。")
        if page_count > max_pages:
            raise ValueError(
                f"PDF 共 {page_count} 页，超过配置上限 {max_pages} 页；"
                "统一工作流不会静默截断合同。"
            )
        images: list[dict[str, Any]] = []
        for index in range(page_count):
            # 约 144 DPI 在小型合同上兼顾可读性与视觉 token，并允许同合同任务复用字节。
            pixmap = document.load_page(index).get_pixmap(
                matrix=fitz.Matrix(2, 2), alpha=False
            )
            image_bytes = pixmap.tobytes("png")
            images.append(
                {
                    "page": index + 1,
                    "data_url": "data:image/png;base64,"
                    + base64.b64encode(image_bytes).decode("ascii"),
                    "image_bytes": len(image_bytes),
                }
            )
    finally:
        document.close()
    return images, page_count
