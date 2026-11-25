"""批量处理老师照片：抠图 → 人脸检测 → 头肩裁切 → 输出海报尺寸 PNG。

用法::

    python -m scripts.batch_process 老师照片 --out samples --width 1080 --height 1440

适合把一整个文件夹的老师照片一次性转成统一规格的海报素材。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接 `python scripts/batch_process.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import image_processor as ip  # noqa: E402
from app.services.face_detector import get_detector  # noqa: E402
from app.services.pipeline import run_pipeline  # noqa: E402

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def collect(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED))
        elif path.is_file() and path.suffix.lower() in SUPPORTED:
            files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成海报人像素材")
    parser.add_argument("inputs", nargs="+", help="图片文件或文件夹")
    parser.add_argument("--out", default="samples", help="输出目录（默认 samples）")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1440)
    parser.add_argument("--top-ratio", type=float, default=ip.DEFAULT_TOP_RATIO)
    parser.add_argument("--shoulder-ratio", type=float, default=ip.DEFAULT_SHOULDER_RATIO)
    parser.add_argument("--no-cutout", action="store_true", help="跳过 rembg 抠图")
    args = parser.parse_args()

    files = collect(args.inputs)
    if not files:
        print("没有找到可处理的图片")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    detector = get_detector()
    print(f"检测后端：{detector.name}，共 {len(files)} 张，目标 {args.width}×{args.height}")

    failed = 0
    for index, file in enumerate(files, start=1):
        prefix = f"[{index}/{len(files)}] {file.name}"
        try:
            image = ip.decode_image(file.read_bytes())
            result = run_pipeline(
                image,
                target_size=(args.width, args.height),
                top_ratio=args.top_ratio,
                shoulder_ratio=args.shoulder_ratio,
                remove_bg=not args.no_cutout,
                detector=detector,
            )
            target = out_dir / f"{file.stem}_poster.png"
            target.write_bytes(ip.encode_png(result.image))
            face_info = "未检测到人脸" if result.face is None else (
                f"人脸 {result.face.w}×{result.face.h} @({result.face.x},{result.face.y})"
            )
            print(f"{prefix} -> {target.name} · {face_info} · {result.crop.strategy}")
            for warning in result.warnings:
                print(f"    ! {warning}")
        except Exception as exc:  # 单张失败不影响批处理
            failed += 1
            print(f"{prefix} 失败：{exc}")

    print(f"完成：{len(files) - failed} 张成功，{failed} 张失败，输出目录 {out_dir.resolve()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
