"""gate.secret_scan: 秘密と個人情報の検査（S-15.3、S-7.4）。外へ公開する型の前に必ず通す（S-7.7 の 8）。LLM は使わない。

検査する物: with.texts（文字列の配列）と、with.paths（workdir_id の worktree の中のファイル。相対パス）。見つかれば bug（secret_found）。
見つけた値は出力にも事象にも入れない（種類と場所だけ）。S-15.3 の正本は axcommon.redact（Redactor.scan）で、それを使う。
それが無い環境では、この型の組み込みの形式（秘密鍵、トークン、鍵 ID、メールアドレス、電話番号、代入）で検査する。
"""
from __future__ import annotations

import re
from pathlib import Path

from runtime.subloop.nodeapi import bug, error, ok

schema = {
    "type": "object",
    "properties": {"texts": {"type": "array", "items": {"type": "string"}}, "paths": {"type": "array", "items": {"type": "string"}},
                   "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}},
    "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"]}

PATTERNS: dict[str, re.Pattern] = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "api_key_sk": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    "aws_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "bearer_token": re.compile(r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._\-]{16,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    "assigned_secret": re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{12,}"),
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+\.[A-Za-z0-9.\-]{2,}\b"),
    "phone": re.compile(r"(?<!\d)(?:\+81|0)\d{1,4}[- ]?\d{1,4}[- ]?\d{3,4}(?!\d)"),
}
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "ax.invalid", "users.noreply.github.com")
MAX_FILE_BYTES = 2_000_000


_REDACTOR = None


def _redactor():
    """axcommon.redact（S-15.3 の正本。b1）。無い環境では None（組み込みの形式で検査する）。"""
    global _REDACTOR
    if _REDACTOR is None:
        try:
            from axcommon import redact

            _REDACTOR = redact.from_environment()
        except Exception:  # noqa: BLE001
            _REDACTOR = False
    return _REDACTOR or None


def scan_text(text: str) -> list[str]:
    """見つかった種別の一覧（値は返さない）。"""
    kinds: list[str] = []
    r = _redactor()
    if r is not None:
        kinds += [f"{h.category}:{h.rule}" for h in r.scan(text)]
        return sorted(set(kinds))
    for kind, pat in PATTERNS.items():
        for m in pat.finditer(text):
            if kind == "email" and m.group(0).lower().endswith(tuple("@" + d for d in ALLOWED_EMAIL_DOMAINS)):
                continue
            kinds.append(kind)
            break
    return kinds


async def run(ctx, inp):
    w = inp["with"]
    found: list[dict] = []
    for i, t in enumerate(w.get("texts") or []):
        for k in scan_text(t or ""):
            found.append({"kind": k, "where": f"texts[{i}]"})
    paths = w.get("paths") or []
    if paths:
        if not w.get("workdir_id"):
            return error("workdir_required", "paths を検査するには workdir_id が要る")
        base = ctx.workdir(w["workdir_id"]).resolve()
        for rel in paths:
            p = (base / rel).resolve()
            if base not in p.parents and p != base:
                return error("path_outside", f"worktree の外のパス: {rel}")
            if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                continue
            for k in scan_text(p.read_text(encoding="utf-8", errors="replace")):
                found.append({"kind": k, "where": rel})
    if found:
        return bug("secret_found", f"{len(found)} 件", data={"findings": found})
    return ok({"checked_texts": len(w.get("texts") or []), "checked_files": len(paths)})
