#!/usr/bin/env python3
"""REQUIREMENTS / SPEC / DESIGN の3方向の対応と、INDEX.md（(c) の一致、(b)(d) の参照の実在）を機械的に調べる。"""
import re, sys, pathlib
d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
R = (d/"REQUIREMENTS.md").read_text(); S = (d/"SPEC.md").read_text(); D = (d/"DESIGN.md").read_text()
bad = []
req = set(re.findall(r"^\| (R-[A-Z]\d+) \|", R, re.M))
spec_heads = set(re.findall(r"^#{2,3} (S-\d+(?:\.\d+)?)\b", S, re.M))
design_heads = set(re.findall(r"^## (D-\d+)\b", D, re.M))
def sections(text, pat):
    parts = re.split(pat, text, flags=re.M)
    return {parts[i]: parts[i+1] for i in range(1, len(parts)-1, 2)}
ssec = sections(S, r"^## (S-\d+) ")
dsec = sections(D, r"^## (D-\d+|付録 A) ")
# 1 SPEC -> REQ
s2r = {}
for k, body in ssec.items():
    m = re.search(r"^要求: (.+)$", body, re.M)
    if not m: bad.append(f"{k}: 要求の行がない"); continue
    ids = set(re.findall(r"R-[A-Z]\d+", m.group(1))); s2r[k] = ids
    for i in ids - req: bad.append(f"{k}: 未定義の要求 {i}")
covered_r = set().union(*s2r.values())
for i in sorted(req - covered_r): bad.append(f"要求 {i} を満たす仕様の節がない")
# 2 DESIGN -> SPEC
d2s = {}
for k, body in dsec.items():
    m = re.search(r"^仕様: (.+)$", body, re.M)
    if not m: bad.append(f"{k}: 仕様の行がない"); continue
    ids = set(re.findall(r"S-\d+(?:\.\d+)?", m.group(1))); d2s[k] = ids
    for i in ids - spec_heads: bad.append(f"{k}: 未定義の仕様 {i}")
covered_s = {i.split(".")[0] for v in d2s.values() for i in v}
for k in sorted(ssec, key=lambda x: int(x[2:])):
    if k not in covered_s and k not in ("S-1",): bad.append(f"仕様 {k} を実現する設計の節がない")
# 3 REQ -> DESIGN（仕様を通して）
r_via_d = set()
for v in d2s.values():
    for i in v: r_via_d |= s2r.get(i.split(".")[0], set())
for i in sorted(req - r_via_d): bad.append(f"要求 {i} が設計まで届いていない")
# 本文中の参照
for name, text in (("SPEC", S), ("DESIGN", D)):
    for ref in set(re.findall(r"\bS-\d+(?:\.\d+)?\b", text)):
        if ref not in spec_heads: bad.append(f"{name}: 存在しない節 {ref} を参照")
    for ref in set(re.findall(r"\bD-\d+\b", text)):
        if ref not in design_heads: bad.append(f"{name}: 存在しない節 {ref} を参照")
    for ref in set(re.findall(r"\bR-[A-Z]\d+\b", text)):
        if ref not in req: bad.append(f"{name}: 存在しない要求 {ref} を参照")
tests = set(re.findall(r"^\| (T-\d+) \|", S, re.M))
for ref in set(re.findall(r"\bT-\d+\b", D)):
    if ref not in tests: bad.append(f"DESIGN: 存在しない試験 {ref}")
for t in sorted(tests - set(re.findall(r"\bT-\d+\b", D)), key=lambda x:int(x[2:])): bad.append(f"試験 {t} を作る段が設計にない")
# 既定値の名前
knobs = set(re.findall(r"^\| `([a-z_]+)` \|", ssec["S-17"], re.M))
for name, text in (("SPEC", S), ("DESIGN", D)):
    for k in set(re.findall(r"S-17 `([a-z_]+)`", text)):
        if k not in knobs: bad.append(f"{name}: S-17 にない既定値 {k}")
for k in set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", D)):
    if k.endswith(("_s","_min","_h","_days","_max","_count","_after","_share","_beats","_streak","_loops","_coverage","_success","_runs","_threshold","_stretch")) and k not in knobs and k not in ("node_runs","max_loops"):
        bad.append(f"DESIGN: 既定値らしい名前 {k} が S-17 にない")
for k in sorted(knobs):
    if k not in S.replace(ssec["S-17"], ""): bad.append(f"S-17 の {k} が本文で使われていない")
# 外付けの環境モジュール（ENV-MODULE.md）と版の一致
em_path = d/"ENV-MODULE.md"
if em_path.exists():
    E = em_path.read_text()
    for ref in set(re.findall(r"\bS-\d+(?:\.\d+)?\b", E)):
        if ref not in spec_heads: bad.append(f"ENV-MODULE: 存在しない節 {ref} を参照")
    for ref in set(re.findall(r"\bD-\d+\b", E)):
        if ref not in design_heads: bad.append(f"ENV-MODULE: 存在しない節 {ref} を参照")
    for ref in set(re.findall(r"\bR-[A-Z]\d+\b", E)):
        if ref not in req: bad.append(f"ENV-MODULE: 存在しない要求 {ref} を参照")
    for ref in set(re.findall(r"\bT-\d+\b", E)):
        if ref not in tests: bad.append(f"ENV-MODULE: 存在しない試験 {ref}")
    for k in set(re.findall(r"S-17 `([a-z_]+)`", E)):
        if k not in knobs: bad.append(f"ENV-MODULE: S-17 にない既定値 {k}")
    for line in re.findall(r"^要求: (.+)$", E, re.M):
        for i in re.findall(r"R-[A-Z]\d+", line):
            if i not in req: bad.append(f"ENV-MODULE: 未定義の要求 {i}")
    if "ENV-MODULE.md" not in ssec["S-25"]: bad.append("S-25 から ENV-MODULE.md への参照がない")
    if "ENV-MODULE.md" not in dsec["D-23"]: bad.append("D-23 から ENV-MODULE.md への参照がない")
    for t_id in sorted(set(re.findall(r"\bT-\d+\b", E)) - set(re.findall(r"\bT-\d+\b", D))):
        bad.append(f"ENV-MODULE の試験 {t_id} を作る段が設計にない")
    docs = {"REQUIREMENTS": R, "SPEC": S, "DESIGN": D, "ENV-MODULE": E, "CONSISTENCY": (d/"CONSISTENCY.md").read_text()}
    vers = {n: (re.search(r"^版: (\d+\.\d+\.\d+)", x, re.M) or [None, "?"])[1] for n, x in docs.items()}
    if len(set(vers.values())) != 1: bad.append(f"版が文書で一致しない: {vers}")
# 付録 FACTS.md（現物の事実と構築時の既定）の参照と版
fa_path = d/"FACTS.md"
if fa_path.exists():
    F = fa_path.read_text()
    for ref in set(re.findall(r"\bS-\d+(?:\.\d+)?\b", F)):
        if ref not in spec_heads: bad.append(f"FACTS: 存在しない節 {ref} を参照")
    for ref in set(re.findall(r"\bD-\d+\b", F)):
        if ref not in design_heads: bad.append(f"FACTS: 存在しない節 {ref} を参照")
    for ref in set(re.findall(r"\bR-[A-Z]\d+\b", F)):
        if ref not in req: bad.append(f"FACTS: 存在しない要求 {ref} を参照")
    for ref in set(re.findall(r"\bT-\d+\b", F)):
        if ref not in tests: bad.append(f"FACTS: 存在しない試験 {ref}")
    for k in set(re.findall(r"S-17 `([a-z_]+)`", F)):
        if k not in knobs: bad.append(f"FACTS: S-17 にない既定値 {k}")
    if "FACTS.md" not in ssec["S-16"]: bad.append("S-16 から FACTS.md への参照がない")
    if "FACTS.md" not in dsec["D-22"]: bad.append("D-22 から FACTS.md への参照がない")
    fv = (re.search(r"^版: (\d+\.\d+\.\d+)", F, re.M) or [None, "?"])[1]
    rv = (re.search(r"^版: (\d+\.\d+\.\d+)", R, re.M) or [None, "?"])[1]
    if fv != rv: bad.append(f"FACTS の版 {fv} が REQUIREMENTS の版 {rv} と違う")
    # 秘密らしい値（tailnet の IP、長い16進、メールアドレス）を書いていない
    for pat, what in ((r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "tailnet の IP"), (r"\b[0-9a-f]{32,}\b", "長い16進"),
                      (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "メールアドレス")):
        if re.search(pat, F): bad.append(f"FACTS: {what}らしい値がある")
# INDEX.md（実装者向けの索引）: (c) の生成物との一致、(b)(d) の参照の実在、各文書の冒頭の案内
import importlib.util
ix_path = d/"INDEX.md"
if not ix_path.exists():
    bad.append("INDEX.md がない")
else:
    X = ix_path.read_text()
    spec_ = importlib.util.spec_from_file_location("build_index", pathlib.Path(__file__).resolve().parent/"build_index.py")
    bi = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(bi)
    cur = bi.current_block(X)
    if cur is None: bad.append("INDEX.md に (c) の印（INDEX-C:BEGIN／END）がない")
    elif cur != bi.generate(d): bad.append("INDEX.md の (c) が build_index.py の出力と違う。直し方: python3 tools/build_index.py --write")
    rest = bi.MARK_RE.sub("", X)  # (c) 以外の本文（(a)(b)(d)）
    fact_heads = set(re.findall(r"^#{2,3} (F-\d+(?:\.\d+)?)\b", (d/"FACTS.md").read_text(), re.M))
    env_heads = set(re.findall(r"^#{2,3} (\d+(?:\.\d+)?)\.? ", (d/"ENV-MODULE.md").read_text(), re.M))
    for ref in sorted(set(re.findall(r"\bS-\d+(?:\.\d+)?\b", rest))):
        if ref not in spec_heads: bad.append(f"INDEX: 存在しない節 {ref}")
    for ref in sorted(set(re.findall(r"\bD-\d+(?:\.\d+)?\b", rest))):
        if ref not in design_heads and ref not in set(re.findall(r"^#{2,3} (D-\d+\.\d+)\b", D, re.M)): bad.append(f"INDEX: 存在しない節 {ref}")
    for ref in sorted(set(re.findall(r"\bR-[A-Z]\d+\b", rest))):
        if ref not in req: bad.append(f"INDEX: 存在しない要求 {ref}")
    for ref in sorted(set(re.findall(r"\bT-\d+\b", rest))):
        if ref not in tests: bad.append(f"INDEX: 存在しない試験 {ref}")
    for ref in sorted(set(re.findall(r"\bF-\d+(?:\.\d+)?\b", rest))):
        if ref not in fact_heads: bad.append(f"INDEX: 存在しない F 節 {ref}")
    for ref in sorted(set(re.findall(r"ENV §(\d+(?:\.\d+)?)", rest))):
        if ref not in env_heads: bad.append(f"INDEX: 存在しない ENV-MODULE の節 §{ref}")
    for k in sorted(set(re.findall(r"S-17 `([a-z_]+)`", rest))):
        if k not in knobs: bad.append(f"INDEX: S-17 にない既定値 {k}")
    # (b) の段: D-15 の表と、段の並び・合否の試験が一致する
    stages = bi.stage_table(D)
    bsec = re.search(r"^## \(b\).*?(?=^## \(c\))", rest, re.M | re.S)
    got = {}
    if bsec:
        for m in re.finditer(r"^### (B\d+b?) .*?(?=^### |\Z|^- 起動順)", bsec.group(0), re.M | re.S):
            tl = re.search(r"^- 合否の試験: (.+)$", m.group(0), re.M)
            if not tl: bad.append(f"INDEX: 段 {m.group(1)} に「合否の試験」の行がない")
            if not re.search(r"^- 読む節: .*(?:S-|D-|F-|ENV §)", m.group(0), re.M): bad.append(f"INDEX: 段 {m.group(1)} に「読む節」の行がない")
            if not re.search(r"^- 作る物: ", m.group(0), re.M): bad.append(f"INDEX: 段 {m.group(1)} に「作る物」の行がない")
            if not re.search(r"^- 目的: ", m.group(0), re.M): bad.append(f"INDEX: 段 {m.group(1)} に「目的」の行がない")
            got[m.group(1)] = bi.expand_tests(tl.group(1)) if tl else set()
    else:
        bad.append("INDEX: (b) の節がない")
    if list(got) != list(stages): bad.append(f"INDEX: (b) の段の並び {list(got)} が D-15 の {list(stages)} と違う")
    for st in stages:
        if st in got and got[st] != stages[st][1]:
            bad.append(f"INDEX: 段 {st} の試験が D-15 と違う（INDEX だけ {sorted(got[st]-stages[st][1])}、D-15 だけ {sorted(stages[st][1]-got[st])}）")
    # 各文書の冒頭に INDEX.md を先に読む旨がある
    for nm in ("REQUIREMENTS", "SPEC", "DESIGN", "FACTS", "ENV-MODULE", "CONSISTENCY"):
        head = "\n".join((d/f"{nm}.md").read_text().splitlines()[:8])
        if "INDEX.md" not in head: bad.append(f"{nm}: 冒頭に INDEX.md を先に読む案内がない")
    # 版
    xv = (re.search(r"^版: (\d+\.\d+\.\d+)", X, re.M) or [None, "?"])[1]
    rv_ = (re.search(r"^版: (\d+\.\d+\.\d+)", R, re.M) or [None, "?"])[1]
    if xv != rv_: bad.append(f"INDEX の版 {xv} が REQUIREMENTS の版 {rv_} と違う")
print(f"要求 {len(req)} / 仕様の章 {len(ssec)} / 設計の章 {len(dsec)} / 試験 {len(tests)} / 既定値 {len(knobs)}")
print("\n".join(bad) if bad else "OK: 3方向の対応に抜けと宙に浮いた参照はない（INDEX.md の一致を含む）")
sys.exit(1 if bad else 0)
