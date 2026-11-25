# -*- coding: utf-8 -*-
"""Prove the repair touched exactly the broken attributes and nothing else."""
import re
import xml.etree.ElementTree as ET

src = r"D:\DSH\自动修改海报\电视台红色海报多人_复制-01.svg"
dst = r"D:\DSH\自动修改海报\电视台红色海报多人_复制-01.fixed.svg"

a = open(src, "rb").read()
b = open(dst, "rb").read()

print("orig bytes:", len(a), " fixed bytes:", len(b), " delta:", len(b) - len(a))
print("BOM orig:", a[:3], " BOM fixed:", b[:3])
print("CRLF count orig:", a.count(b"\r\n"), " fixed:", b.count(b"\r\n"))

ta, tb = a.decode("utf-8"), b.decode("utf-8")
print("utf-8 decode both: OK")
print("line count:", ta.count("\n"), tb.count("\n"))

pat = re.compile(r'font-family="([^",]*?), "([^"]*?)"')
expected = pat.sub(lambda m: 'font-family="%s, &quot;%s&quot;"' % (m.group(1), m.group(2)), ta)
print("fixed == expected-repair(orig):", expected == tb)

# which lines differ (line-wise compare keeps this cheap)
la, lb = ta.split("\n"), tb.split("\n")
lines_changed = [i + 1 for i, (x, y) in enumerate(zip(la, lb)) if x != y]
print("changed lines:", lines_changed)
print("unchanged line count:", len(la) - len(lines_changed))

# every repaired attribute uses the same alias?
print("alias values:", sorted(set(m.group(2) for m in pat.finditer(ta))))

# sanity: line 49 Chinese text intact
l49 = tb.split("\n")[48]
print("L49 text tail:", l49[l49.index(">") + 1:])
print("L49 balanced quotes:", l49.count('"') % 2 == 0)

root = ET.fromstring(tb)
texts = list(root.iter("{http://www.w3.org/2000/svg}text"))
print("text elements:", len(texts))
print("sample parsed attr:", texts[0].get("font-family"))
