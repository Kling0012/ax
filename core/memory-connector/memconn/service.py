"""記憶コネクタの流れ（D-13、S-14.2、F-8）。

remember(req):
  1. 必須項目と形を検査（checks.normalize）→ 拒否 422
  2. 秘密・個人情報の検査（S-15.3）→ 当たれば拒否 422（置き換えて送らない）。safety.rejected を書く
  3. memory_search で同じ内容を探す（内容がほぼ同じ、または同じ refs）。あれば保存せず、見つけた ID を memory.distilled に書く（status=duplicate）
  4. supersedes があれば memory_supersede（旧 ID を渡す。履歴が残る）、無ければ memory_remember（status=saved / superseded）
  5. 返ってきた ID を memory.distilled に書く。確認用に enrichment_status を返す（補完を待たない）
接続できなかったとき（NotSent）は outbox/ に溜めて 202 queued。要求を送った後の切断・時間切れ（Indeterminate）は再送せず、unknown として
memory.distilled（state=unknown）に残して 202。保存の範囲は共通記憶一つ（visibility=project、scope user:operator・project:all・agent:null）。
"""
from __future__ import annotations

import difflib
import logging
import os
from typing import Any, Callable

from axcommon import timeutil
from axcommon.client import ApiError, Unavailable
from axcommon.events import EventSink, Outbox
from axcommon.redact import Redactor

from . import COMPONENT, checks
from .mcp import Indeterminate, McpClient, NotSent, ToolError, find_memory_id

log = logging.getLogger("memory-connector")

SCOPE = {"user": os.environ.get("AX_MEMCONN_USER", "operator"), "project": "all", "agent": None}
SEARCH_AGENT = "ax"
SEARCH_LIMIT = 10
SIMILAR = 0.95


class Result:
    def __init__(self, http: int, body: dict):
        self.http, self.body = http, body


def error(http: int, code: str, message: str, **extra) -> Result:
    return Result(http, {"error": {"code": code, "message": message, **extra}})


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def build_tags(rec: dict) -> list[str]:
    return [f"project:{rec['project']}", f"subloop:{rec['subloop']}", f"host:{rec['host']}", f"kind:{rec['kind']}", "ax"]


def build_refs(rec: dict) -> list[str]:
    refs = [f"event:{rec['source']}"]
    for r in rec.get("refs", []):
        if r not in refs:
            refs.append(r)
    return refs


def build_memory(rec: dict) -> dict:
    return {
        "type": rec["kind"],
        "content": rec["content"],
        "scope": dict(SCOPE),
        "source": {"agent": "ax", "session": rec["source"], "uri": f"event:{rec['source']}"},
        "visibility": "project",
        "refs": build_refs(rec),
        "tags": build_tags(rec),
        "valid_from": rec.get("valid_from") or timeutil.now_ts(),
    }


def extract_hits(res: Any) -> list[dict]:
    """memory_search の結果から、記憶の一覧 [{id, content, refs}] を取り出す（応答の形に寄りかかりすぎない）。"""
    items: list = []
    if isinstance(res, list):
        items = res
    elif isinstance(res, dict):
        for k in ("results", "memories", "items", "hits"):
            if isinstance(res.get(k), list):
                items = res[k]
                break
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        m = it.get("memory") if isinstance(it.get("memory"), dict) else it
        mid = m.get("memory_id") or m.get("id") or it.get("memory_id")
        if mid:
            out.append({"id": str(mid), "content": str(m.get("content", "")), "refs": [str(r) for r in (m.get("refs") or [])]})
    return out


def find_duplicate(hits: list[dict], rec: dict) -> str | None:
    want_norm = _norm(rec["content"])
    want_refs = set(build_refs(rec))
    for h in hits:
        if rec.get("supersedes") and h["id"] == rec["supersedes"]:
            continue  # 置き換える相手は重複とみなさない
        hn = _norm(h["content"])
        if hn == want_norm or (hn and difflib.SequenceMatcher(None, hn[:2000], want_norm[:2000]).ratio() >= SIMILAR):
            return h["id"]
        if h["refs"] and set(h["refs"]) == want_refs:
            return h["id"]
    return None


class MemoryService:
    def __init__(self, mcp: McpClient | None, redactor: Redactor | None, sink: EventSink | None, outbox: Outbox | None):
        self.mcp, self.redactor, self.sink, self.outbox = mcp, redactor, sink, outbox
        self.counts = {"saved": 0, "superseded": 0, "duplicate": 0, "queued": 0, "unknown": 0, "rejected": 0}

    # --- 事象 -----------------------------------------------------------------------------------
    def _emit(self, kind: str, subject: str, data: dict, rec: dict | None = None) -> None:
        if self.sink is None:
            return
        kw = {}
        if rec:
            kw = {"project": rec.get("project"), "subloop": rec.get("subloop"), "cause": rec.get("source")}
        try:
            self.sink.emit(kind, subject, data, **kw)
        except Exception:  # noqa: BLE001  事象が書けなくても保存の結果は返す
            log.exception("事象を書けない: %s", kind)

    def _topic(self, rec: dict) -> str:
        return f"{rec['project']}/{rec['subloop']}:{rec['kind']}"

    # --- 保存 -----------------------------------------------------------------------------------
    def remember(self, req: Any) -> Result:
        rec, rej = checks.normalize(req)
        if rej is None:
            rej = checks.check_secrets(rec, self.redactor)
        if rej is not None:
            self.counts["rejected"] += 1
            if rej.code == "secret_detected":
                self._emit("safety.rejected", (rec or {}).get("source") or "unknown",
                           {"target": (rec or {}).get("source"), "result": "rejected", "reason": "secret_detected", "categories": rej.categories}, rec)
            return error(422, rej.code, rej.message, **({"fields": rej.fields} if rej.fields else {}),
                         **({"categories": rej.categories} if rej.categories else {}))
        if self.mcp is None:
            return error(503, "unavailable", "agent-memory の接続先が設定されていない")
        try:
            return self._process(rec)
        except NotSent:
            return self._queue(rec)

    def _process(self, rec: dict) -> Result:
        """検索 → 保存。NotSent（接続できない）は呼び出し元が溜める。"""
        assert self.mcp is not None
        query = " ".join(rec["content"].split())[:200]
        res = self.mcp.call_tool("memory_search", {"query": query, "user": SCOPE["user"], "project": SCOPE["project"], "agent": SEARCH_AGENT,
                                                    "limit": SEARCH_LIMIT})
        dup = find_duplicate(extract_hits(res), rec)
        if dup:
            self.counts["duplicate"] += 1
            self._emit("memory.distilled", dup, {"memory_id": dup, "topic": self._topic(rec), "duplicate": True}, rec)
            return Result(200, {"status": "duplicate", "memory_id": dup})
        memory = build_memory(rec)
        try:
            if rec.get("supersedes"):
                out = self.mcp.call_tool("memory_supersede", {"memory_id": rec["supersedes"], "replacement": memory}, write=True)
                status = "superseded"
            else:
                out = self.mcp.call_tool("memory_remember", {"memory": memory}, write=True)
                status = "saved"
        except Indeterminate as e:
            # 保存されたかどうか不明。再送すると重複を作る。unknown として残す（F-8）
            self.counts["unknown"] += 1
            self._emit("memory.distilled", rec["source"], {"topic": self._topic(rec), "state": "unknown", "reason": type(e).__name__}, rec)
            return Result(202, {"status": "unknown", "memory_id": None, "detail": "応答を確認できなかった。再送しない"})
        except ToolError as e:
            return error(502, "memory_error", "agent-memory が失敗を返した: " + str(e)[:120])
        mid = find_memory_id(out)
        if not mid:
            self.counts["unknown"] += 1
            self._emit("memory.distilled", rec["source"], {"topic": self._topic(rec), "state": "unknown", "reason": "no_memory_id"}, rec)
            return Result(202, {"status": "unknown", "memory_id": None, "detail": "応答に ID が無い。再送しない"})
        self.counts[status] += 1
        enrich = out.get("enrichment_status") if isinstance(out, dict) else None
        self._emit("memory.distilled", mid, {"memory_id": mid, "topic": self._topic(rec)}, rec)
        return Result(200, {"status": status, "memory_id": mid, "enrichment_status": enrich})

    def _queue(self, rec: dict) -> Result:
        if self.outbox is None:
            return error(503, "unavailable", "agent-memory に届かない")
        self.outbox.append("memory", "/v1/remember", "POST", rec, entry_id=rec["source"] + "-" + timeutil.now_ts())
        self.counts["queued"] += 1
        return Result(202, {"status": "queued", "memory_id": None, "detail": "接続できないため outbox に溜めた"})

    def drain_outbox(self) -> int:
        """溜めた依頼を順に処理する。接続できなければそこで止める。Indeterminate は unknown にして先へ進む（再送しない）。"""
        if self.outbox is None or self.mcp is None:
            return 0

        def send(entry: dict) -> None:
            try:
                self._process(entry["body"])
            except NotSent as e:
                raise Unavailable(str(e)) from e

        return self.outbox.drain(send)

    # --- 検索・読み取り ------------------------------------------------------------------------
    def search(self, req: Any) -> Result:
        if not isinstance(req, dict) or not isinstance(req.get("query"), str) or not req["query"].strip():
            return error(422, "bad_request", "query が要る")
        if self.mcp is None:
            return error(503, "unavailable", "agent-memory の接続先が設定されていない")
        limit = req.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            return error(422, "bad_request", "limit は 1〜50")
        # 絞り込みは query に語を足す（agent-memory 側の改修前。S-14.2、F-8）
        terms = []
        for f in ("project", "subloop", "host", "kind"):
            v = req.get(f)
            if isinstance(v, str) and v:
                terms.append(f"{f}:{v}")
        query = " ".join([req["query"].strip()] + terms)
        args = {"query": query, "user": SCOPE["user"], "project": SCOPE["project"], "agent": SEARCH_AGENT, "limit": limit}
        if req.get("include_history") is True:
            args["include_history"] = True
        try:
            return Result(200, {"results": self.mcp.call_tool("memory_search", args)})
        except Unavailable:
            return error(503, "unavailable", "agent-memory に届かない")
        except ToolError as e:
            return error(502, "memory_error", str(e)[:120])

    def get(self, memory_id: str) -> Result:
        if self.mcp is None:
            return error(503, "unavailable", "agent-memory の接続先が設定されていない")
        try:
            return Result(200, {"memory": self.mcp.call_tool("memory_get", {"memory_id": memory_id})})
        except Unavailable:
            return error(503, "unavailable", "agent-memory に届かない")
        except ToolError as e:
            return error(502, "memory_error", str(e)[:120])

    def list(self, params: dict) -> Result:
        if self.mcp is None:
            return error(503, "unavailable", "agent-memory の接続先が設定されていない")
        args = {k: params[k] for k in ("limit", "field", "since", "until", "cursor") if params.get(k)}
        if "limit" in args:
            try:
                args["limit"] = int(args["limit"])
            except ValueError:
                return error(422, "bad_request", "limit は整数")
        try:
            return Result(200, {"memories": self.mcp.call_tool("memory_list", args)})
        except Unavailable:
            return error(503, "unavailable", "agent-memory に届かない")
        except ToolError as e:
            return error(502, "memory_error", str(e)[:120])
