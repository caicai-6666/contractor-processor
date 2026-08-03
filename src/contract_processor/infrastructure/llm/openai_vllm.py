"""使用 OpenAI Python SDK 访问本地 vLLM。"""

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any, Sequence

import httpx

from contract_processor.async_utils import run_blocking


def _encode_image_sync(image_path: Path) -> tuple[str, str]:
    """在线程中完成 MIME 初始化、文件读取和 Base64 编码。"""

    mime_type, _ = mimetypes.guess_type(image_path.name)
    encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return mime_type or "image/png", encoded_image


class OpenAIVllmVisionClient:
    """将 OpenAI 兼容响应转换为应用层需要的 JSON 字典。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        # 延迟导入可避免未安装可选依赖时影响领域测试与 CLI 帮助命令。
        from openai import AsyncOpenAI

        # 本地 vLLM 不应继承系统 HTTP(S)_PROXY，否则 localhost 请求可能被送往代理。
        self._http_client = httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=self._http_client,
        )
        self._model = model

    async def generate_json(
        self, *, prompt: str, image_paths: Sequence[Path]
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        encoded_images = await asyncio.gather(
            *(run_blocking(_encode_image_sync, path) for path in image_paths)
        )
        for mime_type, encoded_image in encoded_images:
            # 使用 data URL 传递渲染后的合同页，避免适配器依赖临时 HTTP 文件服务。
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{encoded_image}"
                    },
                }
            )

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
        raw_content = response.choices[0].message.content or "{}"
        return json.loads(raw_content)

    async def close(self) -> None:
        await self._client.close()
        if not self._http_client.is_closed:
            await self._http_client.aclose()
