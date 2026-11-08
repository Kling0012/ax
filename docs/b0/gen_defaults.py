#!/usr/bin/env python3
"""SPEC.md の S-17 の表から config/defaults.yaml を作る（B0 の生成物。手で直すときは SPEC.md の S-17 に合わせる）。

使い方: python3 gen_defaults.py <SPEC.md> <出力 yaml>
"""
import re, sys, yaml, datetime

spec = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^## S-17 .*?(?=^## S-18 )", spec, re.S | re.M)
body = m.group(0)
out = {}
for line in body.splitlines():
    mm = re.match(r"^\| `([a-z_0-9]+)`(?:（profile）)? \| (.*?) \| (.*?) \| (.*?) \|$", line)
    if not mm:
        continue
    name, dflt, rng, used = mm.groups()
    ent = {}
    d = dflt.strip()
    if re.match(r"^\d+:\d+ ", d) or re.match(r"^\d\d:\d\d", d):
        ent["default"] = d.split("（")[0].strip()
    elif re.match(r"^-?\d+\.\d+", d):
        ent["default"] = float(re.match(r"^-?\d+\.\d+", d).group(0))
    elif re.match(r"^\d+", d):
        ent["default"] = int(re.match(r"^\d+", d).group(0))
    elif d in ("空",):
        ent["default"] = ""
    elif d.startswith("`") or re.match(r"^[A-Za-z]", d):
        ent["default"] = d.strip("`")
    else:
        ent["default"] = d
    if d != str(ent["default"]) and not re.fullmatch(r"\d+", d):
        ent["note"] = d
    r = rng.strip()
    mr = re.match(r"^(-?[\d.]+)〜(-?[\d.]+)$", r)
    if mr:
        conv = (lambda x: float(x) if "." in x else int(x))
        ent["min"], ent["max"] = conv(mr.group(1)), conv(mr.group(2))
    elif r and r not in ("—",):
        ent["range_text"] = r
    ent["used_in"] = used.strip()
    out[name] = ent
out["retry_backoff"] = {"default": {"base_s": 30, "factor": 2, "max_s": 1800}, "used_in": "S-2.3",
                        "note": "S-17 の「再試行の待ち」（30秒 × 2^回数、最大 30分）。名前は実装者が決めた（docs/CHANGELOG.md）"}
doc = {"schema_version": 1, "defaults": out}
hdr = ("# S-17 既定値。公開してよい値だけ。範囲の変更は利用者（S-17）。\n"
       "# 生成: docs/b0/gen_defaults.py が SPEC.md の S-17 の表から作る。範囲 min/max が無い物は「登録簿で検証」などの文言を range_text に残す。\n")
open(sys.argv[2], "w", encoding="utf-8").write(hdr + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200))
print(len(out), "entries")
