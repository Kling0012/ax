"""decide.llm: worker-pool の判定役（役割 screen）に、JSON Schema 付きで判断を頼む（S-7.4、D-6.3）。LLM を使う。
返答（報告の本文の中の最後の JSON）が Schema に合わなければ bug。合えば data.decision に入れる。"""
from __future__ import annotations

import json

from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import adopt_or_submit, job_view, wait_job

schema = {
    "type": "object",
    "properties": {"prompt": {"type": "string", "minLength": 1}, "schema": {"type": "object"}, "role": {"enum": ["screen", "summary", "research", "second_opinion"]}},
    "required": ["prompt", "schema"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"], "llm": True, "role_field": "role"}


def last_json(text: str):
    """本文の中の最後の JSON オブジェクト（波括弧の対応で切り出す）。"""
    best = None
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    best = json.loads(text[start:i + 1])
                except ValueError:
                    pass
    return best


async def run(ctx, inp):
    import jsonschema

    w = inp["with"]
    role = w.get("role", "screen")
    prompt = w["prompt"] + "\n\n## 出力の形\n次の JSON Schema に合う JSON オブジェクトを 1 つだけ、最後に書く。\n" + json.dumps(w["schema"], ensure_ascii=False)
    body = {"role": role, "prompt": prompt, "sensitivity": ctx.manifest.get("sensitivity", "private"), "project": (ctx.item or {}).get("project"),
            "subloop": ctx.svc.plugin_id, "item_id": ctx.item_id}
    body = {k: v for k, v in body.items() if v is not None}
    jid, rej = await adopt_or_submit(ctx, body)
    if jid is None:
        return error("job_rejected", str(rej.get("code")), expected=rej.get("status") in (408, 429, 503))
    job = await wait_job(ctx, jid)
    v = job_view(job)
    if v["state"] != "ok":
        return error(f"job_{v['state']}", f"判定のジョブが {v['state']}", expected=v["state"] in ("timed_out", "queued", "launching", "submitted", "running", "stalled"))
    obj = last_json(v["report_text"])
    try:
        if obj is None:
            raise ValueError("JSON が無い")
        jsonschema.validate(obj, w["schema"])
    except (ValueError, jsonschema.ValidationError):
        return bug("decision_invalid", "返答が Schema に合わない", data={"job_id": jid})
    return ok({"decision": obj, "job_id": jid, "model": v.get("model")})
