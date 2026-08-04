"""字段发现正式提示词的严格模板加载与渲染。"""

from __future__ import annotations

from pathlib import Path
import re


DISCOVERY_PROMPT_ROOT = (
    Path(__file__).resolve().parents[1] / "extraction/discovery/prompts"
)
_PROMPT_TEMPLATES = {
    path.name: path.read_text(encoding="utf-8").strip()
    for path in DISCOVERY_PROMPT_ROOT.glob("*.txt")
}


def render_discovery_prompt(name: str, replacements: dict[str, str]) -> str:
    """渲染随包分发的发现提示词，并拒绝缺失、重复或遗留占位符。"""

    try:
        template = _PROMPT_TEMPLATES[name]
    except KeyError as error:
        raise FileNotFoundError(f"字段发现 Prompt 不存在：{name}") from error
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise RuntimeError(f"发现 Prompt {name} 必须且只能包含一次占位符 {marker}。")
        template = template.replace(marker, value)
    unresolved = re.findall(r"\{\{[A-Z0-9_]+\}\}", template)
    if unresolved:
        raise RuntimeError(f"发现 Prompt {name} 存在未渲染占位符：{unresolved}。")
    return template
