"""排他（S-8.12 の 11、T-114）。

同じ session_id を、同時に 2 つのジョブが再開しない。worker-pool は、resume_from を持つ
ジョブの作成と同じ処理の中で、同じ連鎖に終わっていないジョブが無いことを確かめる。
launcher は、連鎖の root ごとの鍵（flock）の下で、同じ連鎖の実行中のコンテナが無いことを
確かめてから起動する。

- 二重の再開が起きるときは、R-N14 の指定（resume_required）があるジョブは queued
  （理由 session_busy）で先のジョブの終了を待つ。指定が無いジョブは新しい会話で起動する。
- 終了、失敗、取消、時間切れ、主体消失のどの経路でも、排他は一度だけ解放する。
  libera の経路は worker-pool の内部で、1 回だけの解放を数える。
- 同じ workdir_id を使うジョブにも同じ排他をかける（queued、理由 workdir_busy）。
"""
from __future__ import annotations

TERMINAL_STATES = ("ok", "failed", "timed_out", "cancelled", "lost", "rejected")


class Exclusivity:
    """root（連鎖）と workdir ごとの排他。解放は 1 回だけ。"""

    def __init__(self):
        self._held: set[tuple[str, str]] = set()  # ("root", root) / ("wd", wd)
        self.releases = 0

    def _key(self, kind: str, name: str) -> tuple[str, str]:
        return (kind, name)

    def try_acquire(self, *, root: str, wd: str) -> tuple[bool, str | None]:
        """取れなければ理由を返す。両方取れるときだけ両方を取る。"""
        if self._key("root", root) in self._held:
            return False, "session_busy"
        if self._key("wd", wd) in self._held:
            return False, "workdir_busy"
        self._held.add(self._key("root", root))
        self._held.add(self._key("wd", wd))
        return True, None

    def release(self, *, root: str, wd: str) -> bool:
        """一度だけ解放する。再び呼んでも回数は増えない（T-114）。"""
        freed = False
        for k in (self._key("root", root), self._key("wd", wd)):
            if k in self._held:
                self._held.discard(k)
                freed = True
        if freed:
            self.releases += 1
        return freed

    def held(self, kind: str, name: str) -> bool:
        return self._key(kind, name) in self._held


def decide_conflict(*, resume_required: bool, busy: str | None) -> dict:
    """二重の再開のときの扱い（S-8.12 の 11）。"""
    if busy is None:
        return {"mode": "launch", "reason": "ok"}
    if resume_required:
        return {"mode": "queued", "reason": busy}  # 先のジョブの終了を待つ
    return {"mode": "new_session", "reason": busy}
