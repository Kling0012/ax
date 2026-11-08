"""偽の agent-memory MCP（Streamable HTTP）。axcommon.client の Transport と同じ形の呼び出し可能な物。ソケットを使わない。

  m = FakeMcp()
  m.sse = True                 応答を SSE（data: 行）で返す
  m.refuse = True              接続できない（NotSent）
  m.drop_after_save = True     保存はするが、応答が届く前に切れる（Indeterminate。保存されたかは不明）
  m.timeout_on_search = True   検索の途中で切れる
  m.expire_session_once()      次のツール呼び出しで 404（セッション切れ）を 1 回返す
  m.memories                   保存された記憶（dict: id -> memory）
  m.calls                      ツール呼び出しの記録（name, arguments）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from tests.fakes.b1.core_memconn_imports import Indeterminate, NotSent


@dataclass
class FakeMcp:
    sse: bool = False
    refuse: bool = False
    drop_after_save: bool = False
    timeout_on_search: bool = False
    error_on_save: bool = False
    memories: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    initialized: int = 0
    _expire: int = 0
    _n: int = 0
    session: str = "sess-1"

    def expire_session_once(self):
        self._expire += 1

    def _reply(self, rid, result=None, error=None, status=200):
        body = {"jsonrpc": "2.0", "id": rid}
        body.update({"error": error} if error else {"result": result})
        raw = json.dumps(body, ensure_ascii=False)
        if self.sse:
            return status, {"Content-Type": "text/event-stream"}, f"event: message\ndata: {raw}\n\n".encode()
        return status, {"Content-Type": "application/json"}, raw.encode()

    def _tool(self, rid, payload):
        return self._reply(rid, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False})

    def __call__(self, method, url, headers, body, timeout):
        if self.refuse:
            raise NotSent("ConnectionRefusedError")
        req = json.loads(body.decode("utf-8"))
        rid, m = req.get("id"), req.get("method")
        if m == "initialize":
            self.initialized += 1
            st, h, b = self._reply(rid, {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake"}})
            h["Mcp-Session-Id"] = f"{self.session}-{self.initialized}"
            return st, h, b
        if m == "notifications/initialized":
            return 202, {}, b""
        if m != "tools/call":
            return self._reply(rid, error={"code": -32601, "message": "method not found"})
        if self._expire:
            self._expire -= 1
            return 404, {}, b""
        name, args = req["params"]["name"], req["params"]["arguments"]
        self.calls.append((name, args))
        if name == "memory_search":
            if self.timeout_on_search:
                raise Indeterminate("TimeoutError")
            q = args["query"].casefold()
            hits = [{"memory": dict(m_, memory_id=i), "enrichment_status": "pending"} for i, m_ in self.memories.items()
                    if q[:30] in m_["content"].casefold() or m_["content"].casefold()[:30] in q]
            return self._tool(rid, {"results": hits[: args.get("limit", 10)]})
        if name in ("memory_remember", "memory_supersede"):
            if self.error_on_save:
                return self._reply(rid, {"content": [{"type": "text", "text": "boom"}], "isError": True})
            mem = args["memory"] if name == "memory_remember" else args["replacement"]
            self._n += 1
            mid = f"mem-{self._n:04d}"
            self.memories[mid] = mem
            if name == "memory_supersede":
                self.memories[args["memory_id"]] = dict(self.memories.get(args["memory_id"], {}), superseded_by=mid)
            if self.drop_after_save:
                raise Indeterminate("ConnectionResetError")
            return self._tool(rid, {"memory_id": mid, "enrichment_status": "pending"})
        if name == "memory_get":
            mem = self.memories.get(args["memory_id"])
            return self._tool(rid, {"memory": mem}) if mem else self._reply(rid, {"content": [{"type": "text", "text": "not found"}], "isError": True})
        if name == "memory_list":
            return self._tool(rid, {"memories": [dict(v, memory_id=k) for k, v in self.memories.items()][: args.get("limit", 100)]})
        return self._reply(rid, error={"code": -32602, "message": "unknown tool"})
