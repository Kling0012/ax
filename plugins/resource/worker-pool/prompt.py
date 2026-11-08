"""依頼の組み立て（S-8.12、D-7.5）。

POST /v1/jobs から runner へ渡す依頼は、次の順に組み立てる（並び A）。
(1) 常設安全規則と役割の規則。(2) 対象プロジェクトの規則。(3) 昇格済みの教訓（ID 順）。
(4) 作業ごとに変わる部分（prompt、attachments、verify、budget_min、Web 隔離の値、時刻、ジョブ ID、試行番号）。

- (1)〜(3) の連結を prefix_id の元にする（並び A と並び B は同じ連結を前に置くか後ろに置くかの差だけ）。
- prefix_cache が on のモデルだけ並び A。off と unknown は並び B（依頼の本体を先頭、(1)〜(3) を後ろ）。
- 同じ役割・プロジェクト・教訓の集合なら (1)〜(3) は同じバイト列にする。作業ごとの値を混ぜない。
- prefix_id は (1)〜(3) の SHA-256 の先頭 16 桁。同じ prefix_id の別の試行が
  prefix_ttl_min 分以内にあったかも記録する。秘密の値は (1)〜(3) に入れない（S-15.2）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptParts:
    standing: str = ""      # (1) 常設安全規則と役割の規則
    project: str = ""       # (2) 対象プロジェクトの規則
    lessons: tuple = ()     # (3) 昇格済みの教訓（ID 順）
    job: str = ""           # (4) 作業ごとに変わる部分


def _normalize(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def prefix_blocks(parts: PromptParts) -> bytes:
    """(1)〜(3) の連結。"""
    chunks: list[bytes] = []
    if parts.standing:
        chunks.append(_normalize(parts.standing))
    if parts.project:
        chunks.append(_normalize(parts.project))
    for lid, body in parts.lessons:
        chunks.append(_normalize(str(lid) + "\n" + str(body)))
    return b"\n".join(chunks)


def prefix_id(parts: PromptParts) -> str:
    """(1)〜(3) の SHA-256 の先頭 16 桁。時刻やジョブ ID は含まない（T-109）。"""
    return hashlib.sha256(prefix_blocks(parts)).hexdigest()[:16]


def build(parts: PromptParts, *, arrangement: str = "A") -> str:
    """同じ並びの関数 1 つ。並び A は (1)〜(3) を先頭に、並び B は (1)〜(3) を後ろに置く。"""
    head = prefix_blocks(parts).decode("utf-8")
    tail = parts.job
    if arrangement == "A":
        return (head + "\n\n" + tail).strip() if head else tail
    if arrangement == "B":
        return (tail + "\n\n" + head).strip() if head else tail
    raise ValueError(f"並びが不正: {arrangement!r}")


def arrangement_for(prefix_cache: str | None) -> str:
    """モデルの prefix_cache で並びを選ぶ（S-8.12 の 5）。on だけ並び A。"""
    return "A" if prefix_cache == "on" else "B"


def session_arg(*, session_id: str | None, resume: bool = False) -> str:
    """agent-cli の {session_arg}（F-7.2）。新しい会話は --session-id、再開は --resume（--fork-session は付けない）。"""
    if not session_id:
        return ""
    return f"--resume {session_id}" if resume else f"--session-id {session_id}"


def job_block(*, prompt: str, job_id: str, attempt_no: int = 1, budget_min: int | None = None,
              verify: str | None = None, attachments: list[str] | None = None, now: str | None = None) -> str:
    lines = [prompt]
    meta = {"job_id": job_id, "attempt": attempt_no}
    if budget_min is not None:
        meta["budget_min"] = budget_min
    if verify:
        meta["verify"] = verify
    if attachments:
        meta["attachments"] = attachments
    if now:
        meta["at"] = now
    lines.append("\n[ax job " + json.dumps(meta, ensure_ascii=False, sort_keys=True) + "]")
    return "\n".join(lines)
