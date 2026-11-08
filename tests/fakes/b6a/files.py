"""偽の檔案（B6a の試験用）。

worker-pool の試験が要る檔案を $TMPDIR の下に作る。固定部分（(1)〜(3)）の
バイト列が決まる出処として使う。実の config/rules/<役割>.md は B6 の実行時が
持つが、試験では偽で通す（T-109 の 2 つの作業で prefix_id が同じになる）。
"""
from __future__ import annotations

from pathlib import Path


def write_rules(root: str | Path, *, role: str = "impl", standing: str = "standing-rules\n",
                project: str = "project-rules\n", lessons: dict[str, str] | None = None) -> Path:
    p = Path(root)
    rules = p / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / f"{role}.md").write_text(standing, encoding="utf-8")
    (rules / f"{role}-project.md").write_text(project, encoding="utf-8")
    for lid, body in (lessons or {}).items():
        (rules / f"lesson-{lid}.txt").write_text(body, encoding="utf-8")
    return p


def write_container_log(root: str | Path, job_id: str, *, session_id: str = "sid-1",
                        input_tokens: int = 100, cache_read: int = 80,
                        cache_creation: int = 0, output: int = 20) -> Path:
    p = Path(root) / f".container-{job_id}.log"
    doc = {"result": "ok", "session_id": session_id, "is_error": False,
           "usage": {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
                     "cache_creation_input_tokens": cache_creation, "output_tokens": output}}
    p.write_text("boot\n" + __import__("json").dumps(doc) + "\n", encoding="utf-8")
    return p


def write_usage(root: str | Path, job_id: str, usage: dict) -> Path:
    p = Path(root) / f".usage-{job_id}.json"
    p.write_text(__import__("json").dumps(usage), encoding="utf-8")
    return p
