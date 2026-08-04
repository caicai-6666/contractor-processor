"""字段发现第二阶段的单合同、单候选字段提取服务。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
from math import isclose
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from contract_processor.application.prompts.pdf_prefix import build_common_prefix
from contract_processor.application.schemas.core_extraction import (
    build_field_extraction_schema,
    output_definition_to_json_schema,
)
from contract_processor.async_utils import run_blocking
from contract_processor.domain.enums import FieldKind
from contract_processor.domain.models import FieldDefinition
from contract_processor.infrastructure.extraction.attribute.pipeline import (
    StructuredOutputError,
    aggregate_attempt_metrics,
    build_attribute_field_prompt,
    invoke_json,
    render_retry_feedback,
    validate_attribute_business_rules,
)
from contract_processor.infrastructure.extraction.field_values import (
    ObjectFieldValue,
    ScalarFieldValue,
    aggregate_object_status,
    finalize_candidate_field,
    validate_extracted_field,
)
from contract_processor.infrastructure.llm.request_limiter import ModelRequestLimiter
from contract_processor.infrastructure.pdf.rendering import _render_pdf_pages_sync
from contract_processor.infrastructure.persistence.yaml_field_catalog import (
    YamlFieldCatalog,
)
from contract_processor.settings import ProjectSettings


class CandidateFieldExtractionError(RuntimeError):
    """保留单字段两次尝试的审计指标，交由应用层隔离为失败观察。"""

    def __init__(
        self,
        message: str,
        *,
        attempt_count: int,
        metrics: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.attempt_count = attempt_count
        self.metrics = metrics


def _parse_frozen_definition(record: Any) -> tuple[dict[str, Any], FieldDefinition]:
    """冻结字段已经过组级门禁；回扫仍在边界处重新编译完整递归契约。"""

    if not isinstance(record, dict):
        raise TypeError("冻结候选 definition 必须是对象。")
    normalized = dict(record)
    definition = YamlFieldCatalog.parse_definition_record(
        normalized, FieldKind.ATTRIBUTE
    )
    return normalized, definition


def _generation_definition(record: dict[str, Any]) -> dict[str, Any]:
    """从受控生成 Schema 移除动态正则，避免 xgrammar 提前结束字符串。

    pattern 仍保留在字段 Prompt 和冻结定义中，并在响应解析后由程序执行，因此不会放松
    最终值契约。对象 properties 与数组 items 中的 pattern 同样递归处理。
    """

    generated = deepcopy(record)

    def strip_pattern(output: Any) -> None:
        if not isinstance(output, dict):
            return
        output.pop("pattern", None)
        properties = output.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                strip_pattern(child)
        strip_pattern(output.get("items"))

    strip_pattern(generated.get("output"))
    return generated


def _validate_frozen_output_constraints(
    definition: dict[str, Any], field: ScalarFieldValue | ObjectFieldValue
) -> list[str]:
    """在结构化响应完成后执行冻结 output 的递归值约束。"""

    output = definition["output"]
    targets: list[tuple[str, Any, dict[str, Any]]] = []
    if isinstance(field, ScalarFieldValue):
        if field.status == "found":
            targets.append((definition["field_id"], field.value, output))
    else:
        properties = output.get("properties", {})
        for name, property_value in field.properties.items():
            if property_value.status == "found" and name in properties:
                targets.append((name, property_value.value, properties[name]))

    errors: list[str] = []
    for path, value, output_definition in targets:
        schema = output_definition_to_json_schema(output_definition)
        try:
            Draft202012Validator(schema).validate(value)
        except JsonSchemaValidationError as error:
            leaf_errors: list[JsonSchemaValidationError] = []

            def collect_leaf(item: JsonSchemaValidationError) -> None:
                if not item.context:
                    leaf_errors.append(item)
                    return
                for child in item.context:
                    collect_leaf(child)

            collect_leaf(error)
            # anyOf 的 null 分支常产生无行动价值的 type 错误；优先反馈业务约束。
            actionable = next(
                (
                    item
                    for item in leaf_errors
                    if item.validator in {"pattern", "enum", "minimum", "maximum"}
                ),
                leaf_errors[0] if leaf_errors else error,
            )
            nested_path = ".".join(str(item) for item in error.absolute_path)
            rendered_path = f"{path}.{nested_path}" if nested_path else path
            errors.append(
                f"{rendered_path}: 规范值未通过冻结 output 的 "
                f"{actionable.validator} 约束，约束值="
                f"{repr(actionable.validator_value)[:300]}"
            )
    return errors


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}
_DENOMINATOR_PERCENT_PATTERN = re.compile(
    r"(?P<scale>[百千万])分之\s*(?P<number>[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千]+)"
)
_PERCENT_SYMBOL_PATTERN = re.compile(
    r"(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<symbol>[%％‰‱])"
)
_SPECIFIC_DELIVERY_METHOD_PATTERN = re.compile(
    r"快递|快运|物流|货拉拉|送货|配送|运输|自提|提货|邮寄|空运|海运|"
    r"陆运|铁路|专车|上门|电子(?:邮件|传输|交付)|现场交付"
)
_PAYMENT_METHOD_MARKER_PATTERN = re.compile(
    r"电子转账|银行转账|电汇|汇款|现金|支票|信用证|承兑|网银|"
    r"支付宝|微信支付|月结|托收|代扣|款到发货|款到订货|货到付款|"
    r"先款后货|先货后款"
)
_PAYMENT_METHOD_EXTRANEOUS_PATTERN = re.compile(
    r"合同.{0,12}(?:回传有效|作废)|过期.{0,8}作废|取消本合同"
)
_INVOICE_CONTEXT_PATTERN = re.compile(r"发票|开票|票面|票据|注明|备注")
_BUYER_PARTY_PATTERN = re.compile(r"买方|甲方|需方|采购方")
_SELLER_PARTY_PATTERN = re.compile(r"卖方|乙方|供方|供应方")


def _parse_ratio_number(value: str) -> float | None:
    """解析比例短语中的阿拉伯数字或常见千以内中文数字。"""

    try:
        return float(value)
    except ValueError:
        pass
    if not value or any(
        character not in _CHINESE_DIGITS and character not in _CHINESE_UNITS
        for character in value
    ):
        return None
    if all(character in _CHINESE_DIGITS for character in value):
        digits = "".join(str(_CHINESE_DIGITS[character]) for character in value)
        return float(int(digits))
    total = 0
    current = 0
    for character in value:
        if character in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[character]
            continue
        unit = _CHINESE_UNITS[character]
        total += (current or 1) * unit
        current = 0
    return float(total + current)


def _percent_values_from_raw(raw_value: str) -> list[float]:
    """把原文中的显式比例统一换算为 unit=percent 的百分数数值。"""

    values: list[float] = []
    scale_factors = {"百": 1.0, "千": 0.1, "万": 0.01}
    for match in _DENOMINATOR_PERCENT_PATTERN.finditer(raw_value):
        numerator = _parse_ratio_number(match.group("number"))
        if numerator is not None:
            values.append(numerator * scale_factors[match.group("scale")])
    symbol_factors = {"%": 1.0, "％": 1.0, "‰": 0.1, "‱": 0.01}
    for match in _PERCENT_SYMBOL_PATTERN.finditer(raw_value):
        values.append(float(match.group("number")) * symbol_factors[match.group("symbol")])
    return list(dict.fromkeys(values))


def _validate_candidate_business_rules(
    definition: dict[str, Any], field: ScalarFieldValue | ObjectFieldValue
) -> list[str]:
    """校验冻结候选通用单位口径及少量可确定的字段语义边界。"""

    targets: list[tuple[str, Any, str]] = []

    def collect_percent_targets(
        *, path: str, output_definition: dict[str, Any], value: Any, raw_value: str
    ) -> None:
        output_type = output_definition.get("type")
        if output_definition.get("unit") == "percent":
            targets.append((path, value, raw_value))
            return
        if output_type == "object" and isinstance(value, dict):
            properties = output_definition.get("properties", {})
            for name, child_value in value.items():
                child_output = properties.get(name)
                if isinstance(child_output, dict):
                    collect_percent_targets(
                        path=f"{path}.{name}",
                        output_definition=child_output,
                        value=child_value,
                        raw_value=raw_value,
                    )
        elif output_type == "array" and isinstance(value, list):
            item_output = output_definition.get("items")
            if isinstance(item_output, dict):
                for index, child_value in enumerate(value):
                    collect_percent_targets(
                        path=f"{path}[{index}]",
                        output_definition=item_output,
                        value=child_value,
                        raw_value=raw_value,
                    )

    output = definition["output"]
    if isinstance(field, ScalarFieldValue):
        if field.status == "found" and isinstance(field.raw_value, str):
            collect_percent_targets(
                path=definition["field_id"],
                output_definition=output,
                value=field.value,
                raw_value=field.raw_value,
            )
    else:
        properties = output.get("properties", {})
        for name, property_value in field.properties.items():
            child_output = properties.get(name)
            if (
                property_value.status == "found"
                and isinstance(child_output, dict)
                and isinstance(property_value.raw_value, str)
            ):
                collect_percent_targets(
                    path=name,
                    output_definition=child_output,
                    value=property_value.value,
                    raw_value=property_value.raw_value,
                )

    errors: list[str] = []
    for path, value, raw_value in targets:
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            expected_values = _percent_values_from_raw(raw_value)
            if expected_values and not any(
                isclose(
                    float(value),
                    expected,
                    rel_tol=1e-7,
                    abs_tol=1e-9,
                )
                for expected in expected_values
            ):
                errors.append(
                    f"{path}: output.unit=percent 时 value 必须使用百分数数值；"
                    f"原文显式比例对应={expected_values}，实际={value}"
                )

    field_id = str(definition.get("field_id", ""))
    field_name = str(definition.get("name", ""))
    if (
        isinstance(field, ScalarFieldValue)
        and field.status == "found"
        and (
            field_id.endswith(("delivery_method", "shipping_method"))
            or any(term in field_name for term in ("交付方式", "交货方式", "运输方式"))
        )
    ):
        raw_value = field.raw_value or ""
        if not _SPECIFIC_DELIVERY_METHOD_PATTERN.search(raw_value):
            errors.append(
                f"{field_id}: 原文只出现泛化的‘发货/交付’动作，未给出快递、物流、"
                "送货、自提或其他具体运输/传递机制，不能判为交付方式 found"
            )
    if (
        isinstance(field, ScalarFieldValue)
        and field.status == "found"
        and (
            field_id == "payment_method"
            or any(term in field_name for term in ("付款方式", "支付方式"))
        )
    ):
        raw_value = field.raw_value or ""
        normalized_value = field.value if isinstance(field.value, str) else ""
        evidence_text = f"{raw_value}\n{normalized_value}"
        ratio_mention_count = len(_DENOMINATOR_PERCENT_PATTERN.findall(raw_value)) + len(
            _PERCENT_SYMBOL_PATTERN.findall(raw_value)
        )
        if ratio_mention_count >= 2:
            errors.append(
                f"{field_id}: 原文包含多个付款比例，属于分期付款安排；"
                "付款方式只能保留独立支付工具或结算机制，不能复制整段阶段安排"
            )
        if _PAYMENT_METHOD_EXTRANEOUS_PATTERN.search(evidence_text):
            errors.append(
                f"{field_id}: raw_value/value 混入合同回传、生效、作废或取消等"
                "相邻事实；必须缩减为直接定义付款方式的最小必要原文"
            )
        if not _PAYMENT_METHOD_MARKER_PATTERN.search(evidence_text):
            errors.append(
                f"{field_id}: 未找到转账、汇款、现金、信用证、款到发货等"
                "独立支付工具或结算机制，不能仅凭普通付款叙述判为 found"
            )
    if (
        isinstance(field, ObjectFieldValue)
        and (
            field_id in {"invoice_requirement", "invoice_specification"}
            or any(term in field_name for term in ("发票要求", "开票要求", "发票开具"))
        )
    ):
        for property_id, property_value in field.properties.items():
            if property_value.status != "found":
                continue
            raw_value = property_value.raw_value or ""
            normalized_value = (
                property_value.value if isinstance(property_value.value, str) else ""
            )
            if property_id in {"invoice_type", "type"}:
                if (
                    re.search(r"普通(?:发票|票)", raw_value)
                    and "专用" in normalized_value
                ) or (
                    "专用" in raw_value and "普通" in normalized_value
                ):
                    errors.append(
                        f"{property_id}: 发票类型规范值与原文的普通/专用类别矛盾，"
                        f"raw_value={raw_value!r}，value={normalized_value!r}"
                    )
            if property_id in {"tax_rate", "invoice_tax_rate"} and not re.search(
                r"发票|开票|增值税", raw_value
            ):
                errors.append(
                    f"{property_id}: 税率原文必须保留发票、开票或增值税语境；"
                    "只有含税价格或孤立百分比不能证明发票税率"
                )
            if property_id in {"invoice_notes", "notes", "remark"} and not (
                _INVOICE_CONTEXT_PATTERN.search(raw_value)
            ):
                errors.append(
                    f"{property_id}: 备注原文必须明确与发票票面、开票备注或需注明内容绑定；"
                    "运费、物流或其他相邻约定不能作为发票备注"
                )
    if (
        isinstance(field, ObjectFieldValue)
        and field.status == "found"
        and (
            field_id == "penalty_for_late_delivery"
            or "逾期交货违约金" in field_name
        )
    ):
        # 该字段描述可执行的违约金计算机制。只有责任方而没有比例、基数或计罚方式，
        # 只能证明存在逾期责任，不能把终止、退款或一般损失赔偿误报为违约金。
        required_properties = ("party", "rate", "base_amount", "daily_basis")
        missing = [
            property_id
            for property_id in required_properties
            if field.properties.get(property_id) is None
            or field.properties[property_id].status != "found"
        ]
        if missing:
            errors.append(
                f"{field_id}: 判为 found 必须同时形成责任方、违约金比例、计费基数和"
                "计罚方式，当前缺少=" + ",".join(missing)
            )
        party = field.properties.get("party")
        if party is not None and party.status == "found":
            raw_party = party.raw_value or ""
            normalized_party = party.value if isinstance(party.value, str) else ""
            raw_is_buyer = bool(_BUYER_PARTY_PATTERN.search(raw_party))
            raw_is_seller = bool(_SELLER_PARTY_PATTERN.search(raw_party))
            value_is_buyer = bool(_BUYER_PARTY_PATTERN.search(normalized_party))
            value_is_seller = bool(_SELLER_PARTY_PATTERN.search(normalized_party))
            if (raw_is_buyer and not raw_is_seller and value_is_seller) or (
                raw_is_seller and not raw_is_buyer and value_is_buyer
            ):
                errors.append(
                    f"{field_id}.party: 规范责任方与最小原文主体矛盾，"
                    f"raw_value={raw_party!r}，value={normalized_party!r}"
                )
    return errors


def _normalize_candidate_business_result(
    definition: dict[str, Any], field: ScalarFieldValue | ObjectFieldValue
) -> ScalarFieldValue | ObjectFieldValue:
    """只对能够由字段定义确定性判空的窄场景执行安全降级。"""

    field_id = str(definition.get("field_id", ""))
    field_name = str(definition.get("name", ""))
    if not (
        isinstance(field, ObjectFieldValue)
        and field.status == "found"
        and (
            field_id == "penalty_for_late_delivery"
            or "逾期交货违约金" in field_name
        )
    ):
        return field
    party = field.properties.get("party")
    calculation_parts = [
        field.properties.get(property_id)
        for property_id in ("rate", "base_amount", "daily_basis")
    ]
    if party is None or party.status != "found" or any(
        part is None or part.status != "not_found" for part in calculation_parts
    ):
        return field

    # 只有逾期责任主体、没有任何违约金计算要素时，原文最多证明一般逾期责任。
    # 将唯一 found 子字段降级后重算外层状态，避免对象通用聚合规则制造假阳性。
    properties = dict(field.properties)
    properties["party"] = party.model_copy(
        update={
            "reason": "原文仅能识别一般逾期责任主体，未形成违约金比例、计费基数或计罚方式。",
            "status": "not_found",
            "value": None,
        }
    )
    return ObjectFieldValue(
        status=aggregate_object_status(properties),
        properties=properties,
    )


class CandidateFieldExtractionService:
    """共享页面缓存和模型限流器，并保持每次请求只有一个候选字段。"""

    def __init__(
        self,
        *,
        project_root: Path,
        settings: ProjectSettings,
        max_retries_per_field: int = 1,
    ) -> None:
        if max_retries_per_field < 0:
            raise ValueError("max_retries_per_field 必须是非负整数。")
        self._project_root = project_root
        self._settings = settings
        self._max_retries_per_field = max_retries_per_field
        self._limiter = ModelRequestLimiter(
            settings.models.mllm.max_concurrent_requests
        )
        self._client: AsyncOpenAI | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._render_tasks: dict[
            Path, asyncio.Task[tuple[list[dict[str, Any]], int]]
        ] = {}
        self._common_prefixes: dict[int, str] = {}
        self._prompt_template: str | None = None

    async def _ensure_client(self) -> AsyncOpenAI:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            await run_blocking(load_dotenv, self._project_root / ".env")
            mllm = self._settings.models.mllm
            http_client = httpx.AsyncClient(
                timeout=mllm.timeout_seconds,
                trust_env=False,
            )
            client = AsyncOpenAI(
                base_url=mllm.base_url,
                api_key=os.getenv(mllm.api_key_env) or "EMPTY",
                http_client=http_client,
            )
            try:
                await client.models.list()
            except Exception:
                await http_client.aclose()
                raise
            self._http_client = http_client
            self._client = client
            return client

    async def _render_document(
        self, pdf_path: Path
    ) -> tuple[list[dict[str, Any]], int]:
        resolved = await run_blocking(pdf_path.resolve)
        async with self._render_lock:
            task = self._render_tasks.get(resolved)
            if task is None:
                task = asyncio.create_task(
                    run_blocking(
                        _render_pdf_pages_sync,
                        resolved,
                        max_pages=(
                            self._settings.models.mllm.vision.max_pages_per_request
                        ),
                    )
                )
                self._render_tasks[resolved] = task
        # 一个字段任务被取消时不能取消同合同其他字段共享的渲染任务。
        return await asyncio.shield(task)

    async def _build_prompt(
        self, page_count: int, definition: FieldDefinition
    ) -> tuple[str, str]:
        if self._prompt_template is None:
            path = (
                Path(__file__).parents[1]
                / "extraction/discovery/prompts/02_extract_candidate_field.txt"
            )
            self._prompt_template = await run_blocking(
                path.read_text, encoding="utf-8"
            )
        marker = "{{CANDIDATE_FIELD_DEFINITION}}"
        if self._prompt_template.count(marker) != 1:
            raise ValueError("候选字段回扫提示词必须包含一次字段定义占位符。")
        common_prefix = self._common_prefixes.get(page_count)
        if common_prefix is None:
            common_prefix = await build_common_prefix(page_count)
            self._common_prefixes[page_count] = common_prefix
        suffix = self._prompt_template.replace(
            marker, build_attribute_field_prompt(definition)
        ).strip()
        return common_prefix, suffix

    async def extract(self, task: dict[str, Any]) -> dict[str, Any]:
        """对一个 candidate_ref 与一个 document_id 执行独立结构化提取。"""

        definition_record, definition = _parse_frozen_definition(task["definition"])
        schema = build_field_extraction_schema(
            [_generation_definition(definition_record)],
            field_set_name="DiscoveryCandidate",
        )
        images, page_count = await self._render_document(Path(task["contract_path"]))
        client = await self._ensure_client()
        prompt, prompt_suffix = await self._build_prompt(page_count, definition)
        attempts: list[dict[str, Any]] = []
        last_error: StructuredOutputError | None = None
        schema_identity = sha256(
            f"{task['candidate_ref']}\0{task['document_id']}".encode("utf-8")
        ).hexdigest()[:12]

        for attempt in range(1, self._max_retries_per_field + 2):
            retry_suffix = ""
            if last_error is not None:
                retry_suffix = (
                    "\n\n【本次单字段重试的校验反馈】\n"
                    + render_retry_feedback([str(last_error)])
                    + "\n请重新阅读当前合同全部 PDF 页面，并重新输出当前唯一字段；"
                    "不要复述或修补上一次答案。"
                )
            try:
                extracted, metrics = await invoke_json(
                    client=client,
                    model=self._settings.models.mllm.model,
                    prompt=prompt,
                    images=images,
                    prompt_suffix=prompt_suffix + retry_suffix,
                    schema=schema,
                    schema_name=(
                        f"candidate_{definition.field_id}_{schema_identity}_a{attempt}"
                    ),
                    generation=self._settings.models.mllm.generation.model_dump(),
                    model_request_limiter=self._limiter,
                )
                attempts.append(metrics)
                if set(extracted.fields) != {definition.field_id}:
                    raise StructuredOutputError(
                        f"候选字段响应必须且只能包含 {definition.field_id}。",
                        finish_reason=None,
                        metrics=metrics,
                    )
                finalized = _normalize_candidate_business_result(
                    definition_record,
                    finalize_candidate_field(
                        extracted.fields[definition.field_id]
                    ),
                )
                errors = [
                    *validate_extracted_field(definition.field_id, finalized),
                    *_validate_frozen_output_constraints(
                        definition_record, finalized
                    ),
                    *_validate_candidate_business_rules(
                        definition_record, finalized
                    ),
                    *validate_attribute_business_rules(
                        definition.field_id, finalized
                    ),
                ]
                if errors:
                    raise StructuredOutputError(
                        "候选字段响应未通过业务校验：" + "；".join(errors),
                        finish_reason=None,
                        metrics=metrics,
                    )
                return {
                    "extraction": finalized.model_dump(mode="json"),
                    "attempt_count": len(attempts),
                    "metrics": aggregate_attempt_metrics(attempts),
                }
            except StructuredOutputError as error:
                if not attempts or attempts[-1] is not error.metrics:
                    attempts.append(error.metrics)
                last_error = error

        assert last_error is not None
        raise CandidateFieldExtractionError(
            str(last_error),
            attempt_count=len(attempts),
            metrics=aggregate_attempt_metrics(attempts),
        )

    async def close(self) -> None:
        """释放批次共享客户端和页面缓存。"""

        # 父图异常退出时可能仍有共享渲染任务；先等待其确定结束，避免清空引用后留下后台任务。
        if self._render_tasks:
            await asyncio.gather(*self._render_tasks.values(), return_exceptions=True)
        if self._client is not None:
            await self._client.close()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._client = None
        self._http_client = None
        self._render_tasks.clear()
        self._common_prefixes.clear()
