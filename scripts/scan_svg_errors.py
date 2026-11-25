# -*- coding: utf-8 -*-
"""Diagnose XML/parsing errors in the given SVG file."""
import re
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\DSH\自动修改海报\电视台红色海报多人_复制-01.svg"

with open(path, "rb") as f:
    raw = f.read()

print("bytes:", len(raw))
print("BOM:", raw[:4])
text = raw.decode("utf-8", errors="replace")
lines = text.split("\n")
print("lines:", len(lines))

# 1) Report every XML parse error by repeatedly looking at expat's position.
print("\n=== iterative XML parse errors (max 20) ===")
for attempt in range(20):
    try:
        ET.fromstring(text)
        print("OK: document parses cleanly")
        break
    except ET.ParseError as e:
        print(f"[{attempt + 1}] {e}")
        m = re.match(r"not well-formed \(invalid token\): line (\d+), column (\d+)", str(e))
        m2 = re.search(r"line (\d+), column (\d+)", str(e))
        if not m2:
            break
        ln, col = int(m2.group(1)), int(m2.group(2))
        cur = lines[ln - 1]
        print(f"    line {ln} len={len(cur)}")
        print(f"    ...{cur[max(0, col - 60):col + 40]!r}")
        break
    except Exception as e:  # noqa: BLE001
        print("other error:", type(e).__name__, e)
        break

# 2) Find attributes whose value contains a stray double quote.
print("\n=== suspects: font-family / any attr with an embedded double quote ===")
pat = re.compile(r'([\w:-]+)="([^"]*)"')
for i, line in enumerate(lines, 1):
    if '"' not in line:
        continue
    # crude: an odd number of double quotes on a line that contains attributes
    if line.count('"') % 2 == 1:
        print(f"L{i}: ODD quote count ({line.count(chr(34))}) len={len(line)}")
        print("   ", line[:400])

# 3) Show all font-family occurrences with their raw text
print("\n=== font-family occurrences ===")
for i, line in enumerate(lines, 1):
    for m in re.finditer(r'font-family=[^ >\n]*', line):
        print(f"L{i}: {m.group(0)[:160]}")
