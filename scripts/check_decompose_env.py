#!/usr/bin/env python
"""校验 .venv-decompose 的关键依赖（供安装脚本调用，避免 PowerShell 拼 `-c` 的引号问题）。

用法：
    python scripts/check_decompose_env.py --stage core     # torch CPU / numpy / cv2
    python scripts/check_decompose_env.py --stage all      # 再加 layerd / paddle / paddleocr
    python scripts/check_decompose_env.py --stage all --skip-paddle

退出码：0 通过；1 有硬性不满足（torch 为 CUDA 版、numpy 越界、layerd 缺失等）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

OK = "OK"
BAD = "FAIL"


def prepare_paddle_cache() -> None:
    """Paddle 的 C++ 推理层要求纯 ASCII 路径；独立运行检查脚本时也要兜底。"""
    home = Path(os.path.expanduser("~"))
    try:
        (home / ".cache" / "paddle" / "dataset").mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = Path(Path.cwd().anchor) / "poster-decompose-cache" / "home"
        os.environ["USERPROFILE"] = str(fallback)
        os.environ["HOME"] = str(fallback)
        (fallback / ".cache" / "paddle" / "dataset").mkdir(parents=True, exist_ok=True)

    if not os.environ.get("PADDLE_PDX_CACHE_HOME"):
        candidates = []
        override = os.environ.get("DECOMPOSE_ASCII_CACHE")
        if override:
            candidates.append(Path(override) / "paddle")
        # 项目父目录通常可写且是纯 ASCII；盘符根目录常常不可写
        parent = Path.cwd().parent
        while parent != parent.parent and not str(parent).isascii():
            parent = parent.parent
        if str(parent).isascii():
            candidates.append(parent / "poster-decompose-cache" / "paddle")
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
        if base:
            candidates.append(Path(base) / "poster-decompose" / "paddle")
        cache = candidates[0]
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                cache = candidate
                break
            except OSError:
                continue
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache)
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "huggingface")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{OK if ok else BAD}] {name:10s} {detail}".rstrip())
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["core", "all"], default="all")
    parser.add_argument("--skip-paddle", action="store_true")
    args = parser.parse_args()
    prepare_paddle_cache()

    passed = True

    try:
        import torch

        cuda = torch.version.cuda
        passed &= report(
            "torch",
            cuda is None,
            f"{torch.__version__} cuda={cuda} threads={torch.get_num_threads()}",
        )
        if cuda is not None:
            print("  必须安装纯 CPU 版 torch（--index-url https://download.pytorch.org/whl/cpu）")
    except Exception as exc:  # noqa: BLE001
        passed = False
        report("torch", False, str(exc))

    try:
        import numpy

        version = tuple(int(part) for part in numpy.__version__.split(".")[:2])
        in_range = (2, 0) <= version < (2, 4)
        passed &= report(
            "numpy",
            in_range,
            f"{numpy.__version__}（layerd 需要 >=2.0，paddlex 需要 <2.4）",
        )
    except Exception as exc:  # noqa: BLE001
        passed = False
        report("numpy", False, str(exc))

    try:
        import cv2

        report("opencv", True, cv2.__version__)
    except Exception as exc:  # noqa: BLE001
        passed = False
        report("opencv", False, str(exc))

    if args.stage == "all":
        try:
            from layerd import LayerDPipeline  # noqa: F401

            report("layerd", True, "LayerDPipeline 可用")
        except Exception as exc:  # noqa: BLE001
            passed = False
            report("layerd", False, str(exc))

        if args.skip_paddle:
            print("  [--] paddle    按 -SkipPaddle 跳过")
        else:
            try:
                import paddle

                report("paddle", True, getattr(paddle, "__version__", ""))
            except Exception as exc:  # noqa: BLE001
                passed = False
                report("paddle", False, str(exc))
            try:
                import paddleocr

                report("paddleocr", True, getattr(paddleocr, "__version__", ""))
            except Exception as exc:  # noqa: BLE001
                passed = False
                report("paddleocr", False, str(exc))

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
