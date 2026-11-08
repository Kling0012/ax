"""ntfy への配送（S-22.4、F-9.1）。

POST `<ntfy_endpoint>/` に JSON `{"topic","title","message","priority","tags","click"}`。受け付けられた印は HTTP 200 と応答の `id`
（テスト通知の合格条件。端末への到達は確かめない。F-9.1）。ntfy 通知上の実行・回答ボタン（`actions`）は初日は付けない（S-22.4）。
外部中継（ntfy.sh の upstream）に渡るのは topic 名とメッセージ ID だけ（サーバー側の設定。F-9.1）。ここは中継へ何も送らない。

結果:
  Receipt(ok=True, id=…)                                 受け付けられた
  Receipt(ok=False, transient=True, reason=…)            届かない・5xx。保持して再配送する
  Receipt(ok=False, transient=False, reason=…)           4xx、ID が無い。再試行しても通らない（回数を限って記録する）
"""
from __future__ import annotations

from dataclasses import dataclass

from axcommon.client import ApiClient, ApiError, Transport, Unavailable, urllib_transport

TEST_TITLE = "AX テスト通知"
TEST_MESSAGE = "届いたら、このカードに『届いた』と答えてください"


@dataclass
class Receipt:
    ok: bool
    id: str | None = None
    transient: bool = False
    reason: str | None = None


class NtfyClient:
    def __init__(self, endpoint: str, topic: str = "hub", *, transport: Transport | None = None, timeout: float = 10.0):
        self.endpoint, self.topic = endpoint, topic
        underlying = transport or urllib_transport

        def checked_transport(*args):
            status, headers, raw = underlying(*args)
            if 200 <= status < 300 and status != 200:
                raise ApiError(status, f"http_{status}")
            return status, headers, raw

        self._api = ApiClient(endpoint, transport=checked_transport, timeout=timeout)

    def send(self, *, title: str, message: str, priority: int = 3, tags: list[str] | None = None, click: str | None = None) -> Receipt:
        body: dict = {"topic": self.topic, "title": title, "message": message, "priority": int(priority)}
        if tags:
            body["tags"] = list(tags)
        if click:
            body["click"] = click
        try:
            res = self._api.post("/", body)
        except Unavailable as e:
            return Receipt(False, transient=True, reason=f"unavailable:{type(e).__name__}")
        except ApiError as e:
            return Receipt(False, transient=False, reason=f"http_{e.status}")
        nid = res.get("id") if isinstance(res, dict) else None
        if not isinstance(nid, str) or not nid:
            return Receipt(False, transient=False, reason="no_id")
        return Receipt(True, id=nid)

    def send_test(self) -> Receipt:
        """テスト通知（件名「AX テスト通知」）。1 件だけ。繰り返さない呼び出しは呼ぶ側（dispatcher.send_test）の責任。"""
        return self.send(title=TEST_TITLE, message=TEST_MESSAGE, priority=3, tags=["white_check_mark"])
