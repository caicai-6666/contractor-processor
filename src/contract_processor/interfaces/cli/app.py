"""本地开发阶段的命令行入口。"""

import argparse
from pathlib import Path

from contract_processor.bootstrap.container import build_inspect_field_catalog


def _project_root() -> Path:
    """从安装后的包位置向上定位项目根目录。"""

    return Path(__file__).resolve().parents[4]


def _inspect_fields(_: argparse.Namespace) -> int:
    summary = build_inspect_field_catalog(_project_root()).execute()
    print(f"Core 字段：{summary.core_count}；Attribute 字段：{summary.attribute_count}")
    return 0


def _process_batch(args: argparse.Namespace) -> int:
    input_directory = Path(args.input_directory)
    if not input_directory.is_dir():
        raise SystemExit(f"合同目录不存在：{input_directory}")
    raise SystemExit("LangGraph 批处理工作流尚未实现；请先完成 PDF 渲染与模型调用模块。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contract-processor", description="合同元数据发现工作流")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect-fields", help="检查 Core 与 Attribute 字段库")
    inspect_parser.set_defaults(handler=_inspect_fields)

    process_parser = subparsers.add_parser("process-batch", help="处理一个合同目录（尚未实现）")
    process_parser.add_argument("input_directory", help="待处理 PDF 合同所在目录")
    process_parser.set_defaults(handler=_process_batch)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.handler(args))
