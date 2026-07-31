"""实验提示词关键边界的回归测试。"""

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGE_1_PROMPT = (
    PROJECT_ROOT / "experiments/core_field_extraction/prompts/01_understand_contract.txt"
)
STAGE_2_PROMPT = PROJECT_ROOT / "experiments/core_field_extraction/prompts/02_extract_core.txt"
RUNNER = PROJECT_ROOT / "experiments/core_field_extraction/run.py"
STAGE_3_PROMPT = (
    PROJECT_ROOT / "experiments/core_field_extraction/prompts/03_review_missing_fields.txt"
)
SETTINGS_PATHS = (
    PROJECT_ROOT / "configs/settings.yaml",
    PROJECT_ROOT / "configs/settings.example.yaml",
)


def test_stage_1_prompt_keeps_navigation_map_constraints() -> None:
    prompt = STAGE_1_PROMPT.read_text(encoding="utf-8")

    assert "{{PAGE_VISIBILITY_CONTEXT}}" in prompt
    assert "parties_hint 就不得为空" in prompt
    assert "每页恰好一项" in prompt
    assert "likely_pages 至少包含一个" in prompt
    assert "不得输出“无”“暂无”等占位字符串" in prompt


def test_stage_1_prompt_separates_observation_from_inference() -> None:
    prompt = STAGE_1_PROMPT.read_text(encoding="utf-8")

    assert "不可信的待分析内容" in prompt
    assert "不得提前推断交易类型、合同形态或文书角色" in prompt
    assert "不得仅因看见签字盖章就声称“签章完整”" in prompt
    assert "不是合同内印刷的“共 X 页”" in prompt


def test_stage_2_prompt_limits_output_to_current_single_field() -> None:
    prompt = STAGE_2_PROMPT.read_text(encoding="utf-8")

    assert "提取当前唯一字段" in prompt
    assert "只包含当前字段定义中的唯一 field_id" in prompt
    assert "只处理当前唯一字段" in prompt
    assert "只提交一次最终 JSON" in prompt
    assert "不得输出子字段全为 null 的对象" in prompt
    # 占位符后的收尾指令与唯一字段定义一起放在图像之后，形成字段级可变后缀。
    assert prompt.index("{{CORE_FIELDS_YAML}}") < prompt.index("字段定义位于合同页面图像之后")


def test_experiment_runner_contains_only_step_1_and_step_2() -> None:
    runner = RUNNER.read_text(encoding="utf-8")

    assert not STAGE_3_PROMPT.exists()
    assert "Step 3" not in runner
    assert "step_3" not in runner
    # 只检查阶段产物写入，避免默认 PDF 文件名中的日期片段造成误报。
    assert 'run_dir / "03_' not in runner
    assert "final_core_extraction.json" in runner


def test_qwen3_vl_instruct_sampling_defaults_are_synchronized() -> None:
    generations = [
        yaml.safe_load(path.read_text(encoding="utf-8"))["models"]["mllm"]["generation"]
        for path in SETTINGS_PATHS
    ]

    assert generations[0] == generations[1]
    assert generations[0] == {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
        "seed": 3407,
        "max_completion_tokens": 8192,
    }
