"""解析设计师导出的 SVG 模板并缓存 schema。

用法::

    python -m scripts.analyze_template "电视台红色海报多人_复制-01.fixed.svg" --no-llm

不带 ``--no-llm`` 时会读取 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 做视觉推断，
失败则自动退回规则推断。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import template_analyzer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 SVG 海报模板")
    parser.add_argument("svg", help="SVG 文件路径")
    parser.add_argument("--name", default="", help="模板名称")
    parser.add_argument("--force", action="store_true", help="忽略缓存重新解析")
    parser.add_argument("--no-llm", action="store_true", help="只用规则推断")
    parser.add_argument("--no-browser", action="store_true", help="不启动无头浏览器（坐标会用估算值）")
    args = parser.parse_args()

    path = Path(args.svg)
    if not path.is_file():
        print(f"文件不存在：{path}")
        return 1

    schema = template_analyzer.analyze_template(
        path.read_bytes(),
        name=args.name or path.stem,
        force=args.force,
        use_llm=not args.no_llm,
        use_browser=not args.no_browser,
    )
    print(f"模板 ID：{schema['template_id']}")
    print(f"画布：{schema['canvas']['width']} × {schema['canvas']['height']}")
    print(f"文字节点：{len(schema['texts'])}，图片节点：{len(schema['images'])}")
    print(f"解析引擎：{schema['source']}，摘要：{schema.get('summary')}")
    for portrait in schema["portraits"]:
        print(f"人像槽位 {portrait['slot'] + 1}：{portrait['mode']}，{portrait['width']}×{portrait['height']}")
    for warning in schema.get("warnings", []):
        print(f"! {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
