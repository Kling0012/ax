"""worker.run: worker-pool にジョブを出し、結果を待つ（S-7.4、S-8.6、D-6.3）。LLM を使う。

- `with`: role、prompt（`prompts/…` のパスか本文）、workdir_id（書き換える役割と review では必須）、resume（auto|off。既定は test_design・impl・impl_hard が auto）、
  resume_from（明示）、resume_ref（項目の再開の参照。最初の入場で resume_from と resume_required に入れる。S-8.12 の 8）、verify（受け入れ検証のコマンド）。
- 読み取り専用の役割（summary、research、second_opinion、screen、assist ほか）に workdir_id を渡すのは拒否する（書き換えは work-tdd の tdd-change だけ。T-11）。
- ok の条件は worker-pool の判定（verify.rc == 0 かつ report.status == ready）。ワーカーの「できた」だけでは ok にしない（S-7.3）。
- blocked は bug ではない。項目を外へ出し、ToDo にする（理由が waiting_reason に当たれば waiting_user、当たらなければ parked）。resume_ref を残す。
- 出力 data: job_id、workdir_id、model。
"""
from __future__ import annotations

from runtime.subloop.nodeapi import bug, eject, error, ok, skip
from runtime.subloop.nodelib import WAITING_REASONS, WORKDIR_ROLES, WRITE_ROLES, adopt_or_submit, job_view, wait_job

ROLES = ["eigen-decide", "summary", "research", "second_opinion", "screen", "assist", "test_design", "impl", "impl_hard", "review", "review_final", "manager", "backup_manager", "impl_public"]
READONLY = ("eigen-decide", "summary", "research", "second_opinion", "screen", "assist", "manager", "backup_manager", "review_final", "impl_public")
AUTO_RESUME = ("test_design", "impl", "impl_hard")

schema = {
    "type": "object",
    "properties": {
        "role": {"enum": ROLES}, "prompt": {"type": "string", "minLength": 1},
        "workdir_id": {"anyOf": [{"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}, {"type": "null"}]},
        "resume": {"enum": ["auto", "off"]}, "resume_from": {"type": "string"},
        "resume_ref": {"type": ["object", "null", "string"]}, "verify": {"type": ["string", "null"]},
        "answer": {"type": ["string", "null"]}, "needs_vision": {"type": "boolean"}, "budget_min": {"type": "integer", "minimum": 1},
        "test_cases_max": {"type": ["integer", "null"]}, "tests_dir": {"type": ["string", "null"]},
    },
    "required": ["role", "prompt"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "skip", "error"], "llm": True, "role_field": "role", "launching": False}


def _prompt_text(ctx, w) -> str:
    p = w["prompt"]
    if "\n" not in p and p.endswith(".md"):
        try:
            return ctx.read_prompt(p)
        except FileNotFoundError:
            raise
    return p


def _compose(ctx, w) -> str:
    item = ctx.item or {}
    parts = [_prompt_text(ctx, w).rstrip()]
    if item.get("title") or item.get("body"):
        parts.append("## 依頼\n" + (item.get("title") or "") + ("\n\n" + item["body"] if item.get("body") else ""))
    acc = item.get("acceptance") or {}
    if acc.get("examples"):
        parts.append("## 受け入れの例（すべきこと）\n" + "\n".join(f"- {x}" for x in acc["examples"]))
    if acc.get("boundaries"):
        parts.append("## 境界の例（してはいけないこと）\n" + "\n".join(f"- {x}" for x in acc["boundaries"]))
    if w.get("answer"):
        parts.append("## 利用者の回答（前回の確認への答え）\n" + str(w["answer"]))
    if w["role"] == "test_design":
        cap = w.get("test_cases_max") or ctx.knobs.get("test_cases_max") or ctx.defaults.get("test_cases_max")
        parts.append(f"## テストの件数の上限\n{cap}（1 テスト 1 振る舞い。外部に触れない）")
        if w.get("tests_dir"):
            parts.append(f"## テストの置き場\nテストは `{w['tests_dir']}/` の下に書く。ここ以外のテストは変えない。")
    if w["role"] in ("impl", "impl_hard"):
        parts.append("## 常設の規則\nテストを書かない・変えない。テストは tests/ax_generated/ の下にあり、変更すると機械的に不合格になる。")
    return "\n\n".join(parts)


def _todo_for_blocked(ctx, view, resume_ref):
    wr = view.get("waiting_reason")
    action = view.get("human_action") or view.get("report_text", "")[:300] or "確認が必要"
    base = {"title": f"ワーカーが止まった: {(ctx.item or {}).get('title') or ctx.item_id}"[:200], "resume_ref": resume_ref}
    if wr in WAITING_REASONS:
        base.update(state="waiting_user", owner="user", card={"question": action, "waiting_reason": wr})
    else:
        base.update(state="parked", owner="system", reason="blocked")
    return base


async def run(ctx, inp):
    w = inp["with"]
    role = w["role"]
    wd = w.get("workdir_id") or None
    if role in READONLY and wd:
        return error("role_cannot_write", f"役割 {role} はリポジトリを書き換えられない。書き換えは work-tdd の tdd-change だけ（T-11）")
    if role in WORKDIR_ROLES and not wd:
        return error("workdir_required", f"役割 {role} は workdir_id が要る（git.worktree の出力を渡す）")
    try:
        prompt = _compose(ctx, w)
    except FileNotFoundError:
        return error("prompt_missing", f"prompt が見つからない: {w['prompt']}")
    body = {"role": role, "prompt": prompt, "sensitivity": ctx.manifest.get("sensitivity", "private"), "project": (ctx.item or {}).get("project"),
            "subloop": ctx.svc.plugin_id, "item_id": ctx.item_id, "todo_id": (ctx.item or {}).get("todo_id")}
    if wd:
        body["workdir_id"] = wd
        if role == "test_design":
            body["attachments"] = ["docs/test-procedure.md"]
    if w.get("verify"):
        body["verify"] = w["verify"]
    if w.get("needs_vision"):
        body["needs_vision"] = True
    if w.get("budget_min"):
        body["budget_min"] = int(w["budget_min"])
    # 続きからの再開（S-8.12 の 8）
    mode = w.get("resume") or ("auto" if role in AUTO_RESUME else "off")
    prev = [r for r in ctx.previous_runs() if r["state"] == "done" and (r.get("out") or {}).get("data", {}).get("job_id")]
    ref = w.get("resume_ref")
    if w.get("resume_from"):
        body["resume_from"] = w["resume_from"]
    elif mode == "auto" and prev:
        body["resume_from"] = prev[-1]["out"]["data"]["job_id"]  # 同じ項目の同じノードに入り直すとき
    elif mode == "auto" and isinstance(ref, dict) and ref.get("job_id") and not ctx.previous_runs():
        body["resume_from"] = ref["job_id"]  # 項目の resume_ref は最初の入場で
        body["resume_required"] = True
    body = {k: v for k, v in body.items() if v is not None}
    jid, rej = await adopt_or_submit(ctx, body)
    if jid is None:
        return error("job_rejected", str(rej.get("code")), expected=rej.get("status") in (408, 429, 503))
    job = await wait_job(ctx, jid)
    v = job_view(job)
    data = {"job_id": jid, "workdir_id": v.get("workdir_id") or wd, "model": v.get("model")}
    st, rs = v["state"], v["report_status"]
    if rs == "blocked" and st in ("ok", "failed", "parked"):
        rr = {"job_id": jid}
        for k_src, k_dst in (("workdir_id", "workdir_id"), ("model", "model"), ("ended_at", "ended_at")):
            val = v.get(k_src) or (wd if k_dst == "workdir_id" else None)
            if val:
                rr[k_dst] = val
        view = dict(v)
        view["waiting_reason"] = (job.get("report") or {}).get("waiting_reason")
        todo = None if (ctx.item or {}).get("todo_id") else _todo_for_blocked(ctx, view, rr)
        arts = {"resume_ref": rr, "human_action": v.get("human_action")}
        if view["waiting_reason"] in WAITING_REASONS:
            arts["waiting_reason"] = view["waiting_reason"]
        return eject("blocked", todo=todo, artifacts=arts, data=data)
    if rs == "dropped" and st in ("ok", "failed"):
        return skip(data)
    if st == "ok" and rs in ("ready", None) and (v["verify_rc"] in (0, None)):
        return ok(data)
    if st == "ok":
        return bug("not_ready", f"report={rs} verify.rc={v['verify_rc']}", data=data)
    if st == "failed":
        if v["verify_rc"] not in (0, None):
            return bug("verify_failed", f"verify.rc={v['verify_rc']}", data={**data, "log_tail": v["log_tail"][-2000:]})
        return error("job_failed", "ジョブが失敗した", data=data)
    return error(f"job_{st}", f"ジョブが {st}", expected=st in ("timed_out", "queued", "launching", "submitted", "running", "stalled"), data=data)
