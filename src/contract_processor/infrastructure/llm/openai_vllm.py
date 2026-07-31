"""使用 OpenAI Python SDK 访问本地 vLLM。"""

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any, Sequence


class OpenAIVllmVisionClient:
    """将 OpenAI 兼容响应转换为应用层需要的 JSON 字典。"""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_seconds: float) -> None:
        # 延迟导入可避免未安装可选依赖时影响领域测试与 CLI 帮助命令。
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_seconds)
        self._model = model

    def generate_json(self, *, prompt: str, image_paths: Sequence[Path]) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            mime_type, _ = mimetypes.guess_type(image_path.name)
            encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
            # 使用 data URL 传递渲染后的合同页，避免适配器依赖临时 HTTP 文件服务。
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type or 'image/png'};base64,{encoded_image}"
                    },
                }
            )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
        raw_content = response.choices[0].message.content or "{}"
        return json.loads(raw_content)
