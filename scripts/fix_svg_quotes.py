# -*- coding: utf-8 -*-
"""修复 SVG 中 font-family 属性里未转义的双引号（浏览器报
"Unexpected token inside opening tag" 的常见原因）。

用法：
    python scripts/fix_svg_quotes.py 海报.svg                 # 只检查，输出 *.fixed.svg
    python scripts/fix_svg_quotes.py 海报.svg -o 输出.svg
    python scripts/fix_svg_quotes.py 海报.svg --inplace        # 原地修复，原文件备份为 *.bak.svg

问题形态：
    font-family="AlibabaPuHuiTi_3_65_Medium-GBpc-EUC-H, "Alibaba PuHuiTi 3.0" letter-spacing=...">
                                                         ^ 这里的双引号提前结束了属性值
修复后：
    font-family="AlibabaPuHuiTi_3_65_Medium-GBpc-EUC-H, &quot;Alibaba PuHuiTi 3.0&quot;" letter-spacing=...">
"""
import argparse
import re
import shutil
import sys
import xml.etree.ElementTree as ET

PATTERN = re.compile(r'font-family="([^",]*?), "([^"]*?)"')


def repair(text: str):
    return PATTERN.subn(lambda m: 'font-family="%s, &quot;%s&quot;"' % (m.group(1), m.group(2)), text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("svg")
    ap.add_argument("-o", "--out")
    ap.add_argument("--inplace", action="store_true")
    args = ap.parse_args()

    raw = open(args.svg, "rb").read()
    text = raw.decode("utf-8")
    fixed, n = repair(text)
    print(f"修复 font-family 属性 {n} 处")

    try:
        ET.fromstring(fixed)
        print("校验通过：修复后 XML 可完整解析")
    except ET.ParseError as e:
        print("校验失败，仍有问题：", e)
        return 1

    if args.inplace:
        shutil.copy2(args.svg, args.svg[:-4] + ".bak.svg")
        out = args.svg
        print("原文件已备份为", args.svg[:-4] + ".bak.svg")
    elif args.out:
        out = args.out
    else:
        if n == 0:
            print("没有需要修复的内容，未写出文件")
            return 0
        out = args.svg[:-4] + ".fixed.svg"

    if args.inplace or args.out or n:
        with open(out, "w", encoding="utf-8", newline="") as f:
            f.write(fixed)
        print("已写出：", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
