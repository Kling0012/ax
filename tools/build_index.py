#!/usr/bin/env python3
"""INDEX.md の (c) ID の索引を、各文書の見出しと表から生成する（標準ライブラリだけ）。

使い方（final/ で実行）:
  python3 tools/build_index.py            生成した索引を標準出力へ出す
  python3 tools/build_index.py --write    INDEX.md の印の間を差し替える
  python3 tools/build_index.py --check    INDEX.md の印の間が生成物と一致するか調べる（終了コード 0/1）
第1引数にディレクトリを渡すこともできる（既定はこのスクリプトの親の親）。
"""
import re, sys, pathlib

BEGIN = "<!-- INDEX-C:BEGIN（tools/build_index.py が生成。手で直さない。直し方: python3 tools/build_index.py --write） -->"
END = "<!-- INDEX-C:END -->"
MARK_RE = re.compile(r"<!-- INDEX-C:BEGIN.*?-->\n.*?<!-- INDEX-C:END -->", re.S)


def cut(s, n):
    s = re.sub(r"\s+", " ", s.strip()).replace("|", "／")
    return s if len(s) <= n else s[: n - 1] + "…"


def read(d, name):
    return (d / name).read_text(encoding="utf-8")


def expand_tests(text):
    """'T-40〜T-47'（範囲）と単独の 'T-n' を T 番号の集合にする。"""
    ids = set()
    for a, b in re.findall(r"T-(\d+)〜T-(\d+)", text):
        ids |= {f"T-{i}" for i in range(int(a), int(b) + 1)}
    ids |= set(re.findall(r"\bT-\d+\b", text))
    return ids


def stage_table(D):
    """D-15 の表 {段: (作る物, 試験の集合, 試験の列の原文)} を順序つきで返す。"""
    m = re.search(r"^## D-15 .*?(?=^## D-16 )", D, re.M | re.S)
    rows = {}
    for line in m.group(0).splitlines():
        c = re.match(r"^\| (B\d+b?) \| (.*) \| (.*) \|$", line)
        if c:
            rows[c.group(1)] = (c.group(2), expand_tests(c.group(3)), c.group(3))
    return rows


def headings(text, pat):
    """見出しの (ID, 見出し全文) を出現順に返す。"""
    out = []
    for line in text.splitlines():
        m = re.match(pat, line)
        if m:
            out.append((next(g for g in m.groups() if g), re.sub(r"^#+ ", "", line).strip()))
    return out


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def generate(d):
    R, S, D = read(d, "REQUIREMENTS.md"), read(d, "SPEC.md"), read(d, "DESIGN.md")
    F, E, C = read(d, "FACTS.md"), read(d, "ENV-MODULE.md"), read(d, "CONSISTENCY.md")
    out = []
    # 要求 R-*（所在の章つき）
    rows, sec = [], ""
    for line in R.splitlines():
        h = re.match(r"^## (\d+)\. (.+)$", line)
        if h:
            sec = f"{h.group(1)} {h.group(2)}"
        m = re.match(r"^\| (R-[A-Z]\d+) \| (.*?) \|", line)
        if m:
            rows.append((m.group(1), cut(sec, 20), cut(m.group(2), 34)))
    out += [f"#### c1 要求 R-*（REQUIREMENTS.md。{len(rows)} 件。列: ID、章、要旨）", ""]
    out += table(["ID", "章", "要旨"], [list(r) for r in rows]) + [""]
    # 仕様 S-*
    sh = headings(S, r"^(?:#{2,3} (S-\d+(?:\.\d+)?)\b|## (付録 A)\b)")
    out += [f"#### c2 仕様 S-*（SPEC.md。{len(sh)} 見出し。列: ID、見出し）", ""]
    out += table(["ID", "見出し"], [[a, cut(b, 70)] for a, b in sh]) + [""]
    # 設計 D-*
    dh = headings(D, r"^(?:#{2,3} (D-\d+(?:\.\d+)?)\b|## (付録 [AB])\b)")
    out += [f"#### c3 設計 D-*（DESIGN.md。{len(dh)} 見出し。列: ID、見出し）", ""]
    out += table(["ID", "見出し"], [[a, cut(b, 70)] for a, b in dh]) + [""]
    # 試験 T-*（所在の節と、作る段）
    stages = stage_table(D)
    t2stage = {}
    for st, (_, ts, _) in stages.items():
        for t in ts:
            t2stage.setdefault(t, []).append(st)
    extra = {}  # D-15 の列に番号がない試験 -> 追加の表の設計の節
    m15 = re.search(r"^\| 対象 \| 試験 \|\n.*?(?=^## D-16 )", D, re.M | re.S)
    for line in (m15.group(0).splitlines() if m15 else []):
        c = re.match(r"^\| .*? (D-\d+(?:\.\d+)?)(?:、.*?)? \| (.*) \|$", line)
        if c:
            for t_id in expand_tests(c.group(2)):
                extra.setdefault(t_id, c.group(1))
    rows, cur = [], ""
    for line in S.splitlines():
        h = re.match(r"^#{2,3} (S-\d+(?:\.\d+)?)\b", line)
        if h:
            cur = h.group(1)
        m = re.match(r"^\| (T-\d+) \| (.*?) \|", line)
        if m:
            rows.append((m.group(1), cur, cut(m.group(2), 30), ",".join(sorted(t2stage.get(m.group(1), [])) or ["-（追加表 " + extra.get(m.group(1), "?") + "）"])))
    out += [f"#### c4 試験 T-*（SPEC.md の表。{len(rows)} 件。列: ID、表のある節、対象、通す段（D-15。「-」は D-15 の列に番号がなく、DESIGN.md の追加の表の節を括弧に示す）。18 と 19 は欠番）", ""]
    out += table(["ID", "節", "対象", "段"], [list(r) for r in rows]) + [""]
    # FACTS F-*
    fh = headings(F, r"^#{2,3} (F-\d+(?:\.\d+)?)\b")
    out += [f"#### c5 FACTS F-*（FACTS.md。{len(fh)} 見出し。列: ID、見出し）", ""]
    out += table(["ID", "見出し"], [[a, cut(re.sub(r"^F-\d+(?:\.\d+)? ", "", b), 70)] for a, b in fh]) + [""]
    # ENV-MODULE §
    eh = [(m.group(1), m.group(2)) for m in re.finditer(r"^#{2,3} (\d+(?:\.\d+)?)\.? (.+)$", E, re.M)
          if "変更履歴" not in m.group(2)]
    out += [f"#### c6 ENV-MODULE §（ENV-MODULE.md。{len(eh)} 見出し。参照は「ENV §14.1」の形。列: §、見出し）", ""]
    out += table(["§", "見出し"], [[a, cut(b, 70)] for a, b in eh]) + [""]
    # CONSISTENCY
    ch = [(m.group(1)) for m in re.finditer(r"^## (.+)$", C, re.M)]
    out += [f"#### c7 CONSISTENCY.md の章（記録。決定の根拠にはしない。F-23。{len(ch)} 章）", ""]
    out += table(["章"], [[cut(h, 70)] for h in ch]) + [""]
    # 既定値
    m = re.search(r"^## S-17 .*?(?=^## S-18 )", S, re.M | re.S)
    rows = []
    for line in m.group(0).splitlines():
        k = re.match(r"^\| `([a-z_]+)` \| (.*?) \| (.*?) \| (.*?) \|", line)
        if k:
            rows.append((f"`{k.group(1)}`", cut(k.group(4), 50)))
    out += [f"#### c8 既定値の名前（SPEC.md の S-17。{len(rows)} 件。列: 名前、使う所）", ""]
    out += table(["名前", "使う所"], [list(r) for r in rows])
    return BEGIN + "\n" + "\n".join(out).rstrip() + "\n" + END


def current_block(index_text):
    m = MARK_RE.search(index_text)
    return m.group(0) if m else None


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    d = pathlib.Path(args[0]) if args else pathlib.Path(__file__).resolve().parent.parent
    gen = generate(d)
    ip = d / "INDEX.md"
    if "--write" in argv:
        t = ip.read_text(encoding="utf-8")
        if not MARK_RE.search(t):
            print("INDEX.md に印（INDEX-C:BEGIN／END）がない", file=sys.stderr); return 1
        ip.write_text(MARK_RE.sub(lambda _m: gen, t, count=1), encoding="utf-8")
        print("INDEX.md の (c) を更新した"); return 0
    if "--check" in argv:
        cur = current_block(ip.read_text(encoding="utf-8")) if ip.exists() else None
        if cur == gen:
            print("OK: INDEX.md の (c) は生成物と一致する"); return 0
        print("INDEX.md の (c) が生成物と違う。直し方: python3 tools/build_index.py --write"); return 1
    print(gen); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
