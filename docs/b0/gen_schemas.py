import json, pathlib, sys
OUT = pathlib.Path(sys.argv[1])
BASE = "https://ax.invalid/schemas/"
D = "https://json-schema.org/draft/2020-12/schema"
def S(name, title, body, defs=None):
    d = {"$schema": D, "$id": BASE + name, "title": title}
    d.update(body)
    if defs: d["$defs"] = defs
    (OUT / name).write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
C = "common.schema.json"
def ref(n, frag=""): return {"$ref": f"{n}#{frag}" if frag else n}
def cref(k): return {"$ref": f"{C}#/$defs/{k}"}
def obj(props, req=(), extra=False, **kw):
    o = {"type": "object", "properties": props}
    if req: o["required"] = list(req)
    if not extra: o["additionalProperties"] = False
    o.update(kw); return o
def arr(items, **kw): return dict({"type": "array", "items": items}, **kw)
STR = {"type": "string"}; INT = {"type": "integer"}; NUM = {"type": "number"}; BOOL = {"type": "boolean"}
def enum(*v): return {"enum": list(v)}
def nullable(x): return {"anyOf": [x, {"type": "null"}]}

# ---------------- common ----------------
S(C, "共通の型", {}, {
  "ulid": {"type": "string", "pattern": "^[0-7][0-9A-HJKMNP-TV-Z]{25}$", "description": "S-2.1 ULID 26文字"},
  "ts": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", "description": "S-2.1 UTC の RFC 3339（ミリ秒まで）"},
  "ts_any": {"type": "string", "format": "date-time", "description": "RFC 3339（ミリ秒は任意。JST など他の時差も許す場面用）"},
  "plugin_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$", "description": "S-4.2 英小文字・数字・ハイフン"},
  "kebab_or_ulid": {"type": "string", "pattern": "^([a-z0-9][a-z0-9-]*|[0-7][0-9A-HJKMNP-TV-Z]{25})$", "description": "S-9.1 id"},
  "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$", "description": "S-8.6"},
  "sensitivity": enum("public", "private", "sensitive") | {"description": "S-15.1 情報の区分"},
  "mode": enum("run", "collect_only", "poll_paused", "draining") | {"description": "S-5.2 動作モード"},
  "plugin_kind": enum("subloop", "resource", "timer"),
  "error_class": enum("system.expected", "artifact.bug", "unknown"),
  "node_status": enum("ok", "bug", "error", "skip"),
  "issued_by": enum("user", "commander", "inspection", "manager", "backup", "emergency"),
  "via": enum("local", "ssh", "tailnet", "hq"),
  "tier": enum("read", "operate", "irreversible"),
  "role": enum("summary", "research", "second_opinion", "screen", "assist", "test_design", "impl", "impl_hard", "review", "review_final", "manager", "backup_manager", "impl_public"),
  "state_run": enum("planned", "launching", "submitted", "running", "completed", "failed", "timed_out", "cancelled", "lost", "skipped", "ejected", "missed"),
  "todo_state": enum("todo", "running", "review", "waiting_user", "parked", "done", "dropped"),
  "projection_name": enum("plugins", "runs", "jobs", "controls", "health"),
  "text_nosecret": {"type": "string", "description": "秘密を入れない文字列（検査は axcommon.redact。S-15.3）"},
})
# enum | dict の連結は Python 3.9+ の dict | で可（上の「enum(...) | {...}」）

S("healthz.schema.json", "GET /healthz の応答（S-2.1）",
  obj({"ok": {"const": True}, "component": STR, "version": STR,
       "projection_skipped": INT}, ["ok", "component", "version"], extra=True))

S("api-error.schema.json", "エラー応答（S-6.15）",
  obj({"error": obj({"code": STR, "message": STR, "hint": STR}, ["code", "message"], extra=True)}, ["error"]))

# ---------------- events ----------------
source = obj({"component": STR, "host": STR, "run_id": nullable(cref("ulid")), "node": nullable(STR),
              "principal": STR, "session": STR, "via": cref("via")}, ["component", "host"])
KINDS_DEF = "event-data.schema.json"
S("event.schema.json", "時系列記録の事象（S-3.2）", {
  **obj({
    "id": cref("ulid"), "seq": {"type": "integer", "minimum": 1}, "ts": cref("ts"), "source": source,
    "project": nullable(STR), "subloop": nullable(STR), "kind": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"},
    "subject": STR, "data": {"type": "object"}, "corr": nullable(cref("ulid")), "cause": nullable(cref("ulid")),
  }, ["id", "ts", "source", "kind", "subject", "data"]),
  "allOf": [{"if": {"properties": {"kind": {"const": k}}, "required": ["kind"]},
             "then": {"properties": {"data": {"$ref": f"{KINDS_DEF}#/$defs/{k}"}}}}
            for k in ["run.state", "job.state", "item.state", "plugin.registered", "plugin.mode", "health.beat", "error.classified",
                      "control.issued", "control.applied", "control.expired_pending", "control.released", "control.renewed",
                      "state.recorded", "task.state", "resource.state", "resource.repeat_failure", "health.alert",
                      "archive.moved", "secret.stored", "decision.recorded"]]})

# data defs per kind (S-3.3 の「data の主な項目」。必須は投影が読む項目 S-3.6 と、文書が必須と書く項目だけ)
cmd_ref = ref("control-command.schema.json")
cmd_data = obj({"command": cmd_ref}, ["command"], extra=True)
free = {"type": "object", "additionalProperties": True}
KD = {
 "run.state": obj({"state": cref("state_run"), "reason": nullable(STR)}, ["state"], extra=True),
 "job.state": obj({"job_id": STR, "state": STR, "model": STR, "role": STR}, ["job_id", "state"], extra=True),
 "item.state": obj({"item_id": STR, "state": STR, "reason": nullable(STR), "todo_id": STR, "artifacts": free}, ["item_id", "state"], extra=True),
 "node.state": obj({"node": STR, "state": STR, "attempt": INT}, ["node", "state"], extra=True),
 "task.state": obj({"todo_id": STR, "state": cref("todo_state"), "reason": nullable(STR)}, ["todo_id", "state"], extra=True),
 "state.recorded": obj({"kind": enum("focus", "daily"), "date": STR, "as_of": STR, "state": STR, "issuer": STR, "changed": {"const": True}}, ["kind", "as_of", "issuer", "changed"]),
 "resource.state": obj({"resource_id": STR, "status": enum("active", "degraded", "excluded", "retired"), "until": nullable(cref("ts_any"))}, ["resource_id", "status"], extra=True),
 "resource.repeat_failure": obj({"resource_id": STR, "code": STR, "count": INT}, ["resource_id", "code", "count"], extra=True),
 "error.classified": obj({"class": cref("error_class"), "code": STR, "message": STR}, ["class", "code"], extra=True),
 "health.beat": obj({"uptime_s": NUM, "lag_s": NUM}, ["uptime_s"], extra=True),
 "health.alert": obj({"check": STR, "target": STR, "severity": STR}, ["check"], extra=True),
 "plugin.registered": obj({"manifest_version": STR, "mode": cref("mode")}, ["mode"], extra=True),
 "plugin.mode": obj({"mode": cref("mode")}, ["mode"], extra=True),
 "control.issued": cmd_data, "control.applied": cmd_data, "control.expired_pending": cmd_data,
 "control.released": cmd_data, "control.renewed": cmd_data,
 "control.rejected": obj({"command": free, "reason": STR}, [], extra=True),
 "archive.moved": obj({"from": STR, "to": STR, "bytes": INT}, ["from", "to"], extra=True),
 "secret.stored": obj({"name": STR}, ["name"]),
 "decision.recorded": obj({"decision_id": STR, "kind": STR, "project": nullable(STR), "supersedes": nullable(STR), "principal": STR, "attested": BOOL}, ["decision_id"], extra=True),
 "instruction.received": obj({"destination": STR, "reason": STR}, [], extra=True),
 "instruction.routed": obj({"destination": STR}, [], extra=True),
 "instruction.rejected": obj({"destination": STR, "reason": STR}, [], extra=True),
 "env.usage": free, "env.job_usage": free, "sandbox.call": free, "sandbox.token": free,
 "inspection.phase": obj({"phase": STR, "plan": free}, [], extra=True),
 "manager.cycle": free, "manager.decision": free, "backup.activated": obj({"reason": STR}, [], extra=True),
 "memory.distilled": obj({"memory_id": STR, "topic": STR}, [], extra=True),
 "change.cycle": obj({"phase": enum("started", "completed", "failed")}, [], extra=True),
 "automation.observed": free, "automation.delivery": free, "automation.collect_checked": free,
 "commander.denied": obj({"principal": STR, "destination": STR, "reason": STR}, [], extra=True),
 "auth.failed": obj({"via": STR, "reason": STR}, [], extra=True),
 "removal.state": free, "removal.check": free, "cutover.state": free,
}
# S-3.3 のその他の kind は data を自由な object とする（書き手の部品の担当が必要なら自分の schemas/ext/ で絞る）
S(KINDS_DEF, "事象の種類ごとの data（S-3.3）", {"description": "kind ごとの data の定義。$defs/<kind> を引く。表にない kind は自由な object。"}, KD)

S("event-post.schema.json", "POST /v1/events の本文（S-3.5）", {"oneOf": [ref("event.schema.json"), arr(ref("event.schema.json"), minItems=1)]})
S("projection-response.schema.json", "GET /v1/projections/{name} の応答（S-3.6）", obj({
  "name": cref("projection_name"), "upto_seq": {"type": "integer", "minimum": 0},
  "rows": arr(obj({"key": STR, "value": {"type": "object"}, "upto_seq": {"type": "integer", "minimum": 0}}, ["key", "value", "upto_seq"]))},
  ["name", "upto_seq", "rows"]))

# ---------------- manifest ----------------
knob = obj({
  "type": enum("integer", "number", "boolean", "string", "enum", "duration", "list"),
  "default": {}, "min": NUM, "max": NUM, "enum": arr({}), "effective": enum("immediate", "next_run", "restart"),
  "title": STR, "who": arr(cref("issued_by")),
}, ["type", "default", "effective", "title"], allOf=[{"if": {"properties": {"type": {"const": "enum"}}, "required": ["type"]}, "then": {"required": ["enum"]}}])
S("manifest.schema.json", "プラグインの manifest（S-4.2、S-4.3）", obj({
  "schema_version": {"const": 1}, "id": cref("plugin_id"), "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+"},
  "kind": cref("plugin_kind"), "project": STR, "title": STR, "image": STR,
  "execution": enum("mechanical", "llm"),
  "schedule": obj({"kind": enum("interval", "cron", "event"), "every_s": {"type": "integer", "minimum": 1}, "cron": STR, "missed": {"const": "record_only"}}, ["kind"]),
  "graph": STR,
  "capabilities": arr(cref("mode") | {}, uniqueItems=True) | {"allOf": [{"contains": {"const": "run"}}, {"contains": {"const": "draining"}}]},
  "knobs": {"type": "object", "additionalProperties": {"$ref": "#/$defs/knob"}},
  "health": obj({"heartbeat_s": {"type": "integer", "minimum": 15, "maximum": 300}}, ["heartbeat_s"]),
  "sensitivity": cref("sensitivity"), "roles": arr(cref("role")), "secrets": arr(STR),
  "outputs": arr(STR), "depends_on_plugins": arr(STR), "accepts_items": BOOL,
}, ["schema_version", "id", "version", "kind", "title", "image", "execution", "schedule", "capabilities", "health", "sensitivity"],
  allOf=[{"if": {"properties": {"kind": {"const": "subloop"}}, "required": ["kind"]}, "then": {"required": ["graph"]}},
         {"if": {"required": ["accepts_items"], "properties": {"accepts_items": {"const": True}}}, "then": {"properties": {"kind": {"const": "subloop"}}}}]),
  {"knob": knob})

S("registry.schema.json", "plugins/registry.yaml（S-4.1。tools/build_registry.py が作る）", obj({
  "schema_version": {"const": 1},
  "plugins": arr(obj({"id": cref("plugin_id"), "kind": cref("plugin_kind"), "path": STR, "source": STR}, ["id", "kind", "path"]))},
  ["schema_version", "plugins"]))
S("registry-fragment.schema.json", "plugins/registry.d/<pkg>.yaml（各束の登録の断片）", obj({
  "plugins": arr(obj({"id": cref("plugin_id"), "kind": cref("plugin_kind"), "path": STR}, ["id", "kind", "path"]))}, ["plugins"]))

S("plugin-status.schema.json", "GET /status（S-4.4）", obj({
  "mode": cref("mode"), "knobs": {"type": "object"}, "running": arr({}), "queued": {"type": "integer", "minimum": 0}, "last_run": nullable({"type": "object"})},
  ["mode", "knobs", "running", "queued", "last_run"], extra=True))
S("plugin-apply.schema.json", "POST /apply の本文（S-4.4）", obj({"mode": cref("mode"), "knobs": {"type": "object"}, "revision": {"type": "integer", "minimum": 0}}, ["mode", "knobs", "revision"]))

# ---------------- control ----------------
cmd_props = {
  "id": cref("ulid"), "target": STR,
  "op": enum("set_mode", "set_knob", "exclude_resource", "include_resource", "cancel", "run_now"),
  "args": {"type": "object"}, "reason": STR, "issued_by": cref("issued_by"), "issued_at": cref("ts"), "expires_at": nullable(cref("ts")),
  "on_expiry": enum("ask_issuer", "release"), "corr": nullable(cref("ulid"))}
op_args = [
 ("set_mode", obj({"mode": cref("mode"), "permission_ids": arr(STR)}, ["mode"])),
 ("set_knob", obj({"name": STR, "value": {}}, ["name", "value"])),
 ("exclude_resource", obj({"resource_id": STR, "until": cref("ts_any")}, ["resource_id", "until"])),
 ("include_resource", obj({"resource_id": STR}, ["resource_id"])),
 ("cancel", {"type": "object", "minProperties": 1, "properties": {"run_id": STR, "item_id": STR, "job_id": STR}, "additionalProperties": False}),
 ("run_now", obj({})),
]
exp_rule = {"if": {"required": ["issued_by"], "properties": {"issued_by": {"not": {"const": "user"}}}}, "then": {"required": ["expires_at"], "properties": {"expires_at": cref("ts")}}}
S("control-command.schema.json", "制御命令（S-5.3）", {**obj(cmd_props, ["id", "target", "op", "args", "reason", "issued_by", "issued_at"]),
  "allOf": [exp_rule] + [{"if": {"properties": {"op": {"const": o}}, "required": ["op"]}, "then": {"properties": {"args": a}}} for o, a in op_args]})
S("control-command-request.schema.json", "POST /v1/commands の本文（S-5.7。issued_by は資格から決まる。本文の値は照合だけに使う）",
  {**obj({k: v for k, v in cmd_props.items() if k not in ("id", "issued_at")}, ["target", "op", "args", "reason"]),
   "allOf": [{"if": {"properties": {"op": {"const": o}}, "required": ["op"]}, "then": {"properties": {"args": a}}} for o, a in op_args]})
S("permission-check.schema.json", "POST /v1/permissions/check（S-5.7）", {}, {
  "request": obj({"project": STR, "repo": STR, "destination": STR, "operation": STR}, ["project", "repo", "destination", "operation"]),
  "response": obj({"allowed": BOOL, "permission_id": nullable(STR), "reason": STR}, ["allowed", "reason"])})
S("validate.schema.json", "POST /v1/validate（S-5.7）", {}, {
  "request": obj({"kind": enum("manifest", "graph"), "body": {}}, ["kind", "body"]),
  "response": obj({"ok": BOOL, "errors": arr(STR)}, ["ok", "errors"])})

# ---------------- intake ----------------
S("instruction.schema.json", "入口の指示（S-6.2）", obj({
  "id": cref("ulid"), "from": enum("commander", "hq"), "destination": STR, "payload": {"type": "object"}, "project": nullable(STR),
  "idempotency_key": {"type": "string", "maxLength": 200}, "session": {"type": "string", "maxLength": 64}, "expires_at": cref("ts"),
  "confirm_irreversible": BOOL}, ["from", "destination", "payload"]))
S("intake-destinations.schema.json", "config/intake-destinations.yaml（S-6.3）", arr(obj({
  "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"}, "title": STR, "schema": STR, "handler": STR,
  "allowed_from": arr(enum("commander", "hq"), minItems=1), "via": arr(cref("via"), minItems=1), "tier": cref("tier"),
  "irreversible": BOOL, "ack": enum("sync", "async"), "default_expiry_min": {"type": "integer", "minimum": 1},
  "conflict_key": STR, "mcp_tool": STR}, ["name", "title", "schema", "handler", "allowed_from", "via", "tier"]),
  ))

# ---------------- graph ----------------
node = obj({
  "id": STR, "type": STR, "with": {"type": "object"},
  "limits": obj({"timeout_s": {"type": "integer", "minimum": 1}, "retries": {"type": "integer", "minimum": 0}, "continuations": {"type": "integer", "minimum": 0}}, ["timeout_s"]),
  "on_fail": {"type": "string", "pattern": "^(eject|skip|goto:.+)$"},
  "needs_mode": arr(cref("mode"))}, ["id", "type", "limits"])
include = obj({"include": STR, "as": STR, "with": {"type": "object"}}, ["include"])
edge = obj({"from": STR, "to": STR, "when": {"type": "string", "pattern": "^(ok|bug|error|skip|outcome:.+)$"}, "max_loops": {"type": "integer", "minimum": 1}}, ["from", "to"])
S("graph.schema.json", "graph.yaml（S-7.1、S-7.2）", obj({
  "schema_version": {"const": 1}, "entry": STR, "item_source": STR,
  "max_parallel_items": {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": r"^\{\{knobs\.[a-z_]+\}\}$"}]},
  "nodes": arr({"oneOf": [{"$ref": "#/$defs/node"}, {"$ref": "#/$defs/include"}]}, minItems=1),
  "edges": arr(edge)}, ["schema_version", "entry", "nodes", "edges"]), {"node": node, "include": include})
S("subgraph.schema.json", "部分グラフ plugins/subloop/_lib/<名前>.yaml（S-7.1）", obj({
  "schema_version": {"const": 1}, "params": {"type": "object"}, "entry": STR,
  "exits": {"type": "object", "propertyNames": {"pattern": "^(ok|bug|error|skip|outcome:.+)$"}, "additionalProperties": STR},
  "nodes": arr({"oneOf": [ref("graph.schema.json", "/$defs/node"), ref("graph.schema.json", "/$defs/include")]}, minItems=1),
  "edges": arr(edge)}, ["schema_version", "entry", "exits", "nodes", "edges"]))
S("node-io.schema.json", "ノードの入出力（S-7.3）", {}, {
  "input": obj({"run_id": cref("ulid"), "item": {"type": "object"}, "with": {"type": "object"},
                "artifacts": obj({"workdir": STR, "files": arr(STR)}, [], extra=True), "deadline": cref("ts_any")}, ["run_id", "item", "with"]),
  "output": obj({"status": cref("node_status"), "data": {"type": "object"}, "artifacts": {"type": "object"},
                 "error": obj({"class": cref("error_class"), "code": STR, "message": STR}, ["class", "code"])}, ["status"])})
S("work-item.schema.json", "work-tdd の項目（S-7.10）", obj({
  "item_key": STR, "todo_id": STR, "kind": enum("work", "subloop_create", "core_change"), "project": STR,
  "role": enum("impl", "impl_hard"), "title": STR, "body": STR,
  "acceptance": obj({"examples": arr(STR, minItems=1), "boundaries": arr(STR, minItems=1)}, ["examples", "boundaries"]),
  "base_ref": STR, "branch": STR, "verify": STR, "template": STR, "answer": STR,
  "resume_ref": nullable(obj({"job_id": STR, "workdir_id": cref("workdir_id"), "model": STR, "ended_at": cref("ts_any")}, ["job_id"]))},
  ["item_key", "todo_id", "kind", "project", "role", "title", "body", "acceptance"],
  allOf=[{"if": {"properties": {"kind": {"const": "subloop_create"}}, "required": ["kind"]}, "then": {"required": ["template"]}}]))

# ---------------- ledger ----------------
S("todo.schema.json", "ToDo（S-9.1、S-9.2）", obj({
  "id": cref("kebab_or_ulid"), "project": nullable(STR), "subloop": nullable(STR), "title": STR, "state": cref("todo_state"),
  "owner": enum("system", "user"), "reason": nullable(STR), "depends_on": arr(STR), "check_at": nullable(cref("ts_any")),
  "source": obj({"run_id": STR, "item_id": STR, "event_id": STR}, [], extra=True), "artifacts": arr(STR),
  "goal_ids": arr(STR), "promise_ids": arr(STR), "due_at": nullable(cref("ts_any")), "cancel_reason": nullable(STR),
  "resume_ref": nullable(obj({"job_id": STR, "workdir_id": cref("workdir_id"), "model": STR, "ended_at": cref("ts_any")}, ["job_id"])),
  "user_only": BOOL,
  "card": nullable(obj({"question": STR, "options": arr(STR), "answer": nullable(STR), "version": {"type": "integer", "minimum": 1}, "notification_state": STR,
                        "waiting_reason": enum("physical", "secret_input", "money", "publish", "irreversible", "preference", "range_review")}, ["question"], extra=True)),
 }, ["title", "state", "owner"], extra=True,
 allOf=[{"if": {"properties": {"state": {"const": "parked"}}, "required": ["state"]}, "then": {"required": ["reason", "check_at"]}},
        {"if": {"properties": {"state": {"const": "dropped"}}, "required": ["state"]}, "then": {"required": ["reason"]}},
        {"if": {"properties": {"state": {"const": "waiting_user"}}, "required": ["state"]}, "then": {"required": ["card"], "properties": {"card": {"type": "object", "required": ["question", "waiting_reason"]}}}}]))
S("todo-transition.schema.json", "POST /v1/todos/{id}/transition（S-9.3）", obj({"to": cref("todo_state"), "reason": STR, "check_at": cref("ts_any")}, ["to"]))
S("todo-answer.schema.json", "POST /v1/todos/{id}/answer（S-9.3）", obj({
  "answer": STR, "answer_source": enum("hq", "relay"), "quote": STR, "version": {"type": "integer", "minimum": 1}}, ["answer"],
  allOf=[{"if": {"properties": {"answer_source": {"const": "relay"}}, "required": ["answer_source"]}, "then": {"required": ["quote"]}}]))
HSTATE = enum("away", "play", "focus", "break", "shallow")
focus = obj({"state": HSTATE, "since": cref("ts_any"), "as_of": cref("ts_any")}, ["state", "since", "as_of"])
seg = obj({"start": cref("ts_any"), "end": cref("ts_any"), "state": HSTATE,
           "category": enum("work", "research", "communication", "media", "play", "other"),
           "session_title": nullable({"type": "string", "maxLength": 40})}, ["start", "end", "state", "category"])
daily = obj({"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}, "tz": {"const": "Asia/Tokyo"}, "as_of": cref("ts_any"), "segments": arr(seg)},
            ["date", "tz", "as_of", "segments"])
S("focus-state.schema.json", "PUT /v1/focus-state の本文（S-9.5）", focus)
S("daily-state.schema.json", "PUT /v1/daily-state/{date} の本文（S-9.5）。segments の件数は S-17 hibi_segments_max、大きさは hibi_body_max_kib で別に検査する", daily)
S("state-put-response.schema.json", "focus/daily の PUT の応答（S-9.5）", obj({"stored": BOOL, "kind": enum("focus", "daily"), "as_of": cref("ts_any"), "version": {"type": "integer", "minimum": 1}}, ["stored", "kind", "as_of", "version"]))
S("hibi-state.schema.json", "hibi-state の成果物 state.json（S-23.3）", obj({
  "now": {"type": "object", "properties": {"state": enum("away", "play", "focus", "break", "shallow", "unknown"), "since": cref("ts_any"), "as_of": cref("ts_any")}, "required": ["state"], "additionalProperties": True},
  "day": {"type": "object", "properties": {"date": STR, "segments": arr(seg)}, "additionalProperties": True},
  "fetched_at": cref("ts_any")}, ["now", "day", "fetched_at"]))

# ---------------- resources / worker-pool ----------------
S("resource.schema.json", "資源台帳の登録（モデル。S-8.2、F-7.3）", obj({
  "id": STR, "kind": enum("model", "environment"), "family": STR, "tier": enum("free", "flash", "standard", "last_resort"),
  "level": enum("L1", "L2", "L3", "L4"), "billing": enum("metered", "flat"), "vision": BOOL, "runner": STR, "cap": {"type": "integer", "minimum": 0},
  "prefix_cache": enum("on", "off", "unknown"), "status": enum("active", "degraded", "excluded", "retired"),
  "trains_on_input": enum("no_train", "train", "unknown"), "stats": {"type": "object"}, "description": STR,
  "pricing": obj({"unit": STR, "input": NUM, "output": NUM, "fetched_at": cref("ts_any"), "source": STR}, [], extra=True)},
  ["id", "kind", "family", "tier", "status"], extra=True))
S("job-request.schema.json", "POST /v1/jobs の本文（S-8.6、S-8.8）", obj({
  "role": cref("role"), "prompt": STR, "attachments": arr(STR), "workdir_id": cref("workdir_id"), "verify": {"oneOf": [STR, obj({"cmd": STR}, ["cmd"], extra=True)]},
  "budget_min": {"type": "integer", "minimum": 1}, "sensitivity": cref("sensitivity"), "needs_vision": BOOL, "project": nullable(STR),
  "subloop": nullable(STR), "item_id": nullable(STR), "todo_id": nullable(STR), "resume_from": STR, "resume_required": BOOL,
  "tools": arr(enum("sandbox-desktop", "web-gui", "web-screenshot", "linux-sandbox")), "placement": enum("local", "env", "any"),
  "depends_on": arr(obj({"job_id": STR, "result": STR}, ["job_id"], extra=True)), "deadline": cref("ts_any")},
  ["role", "prompt", "sensitivity"],
  allOf=[{"if": {"required": ["tools"]}, "then": {"properties": {"role": enum("research", "screen", "assist"), "sensitivity": {"const": "public"}}}}]))
S("job.schema.json", "GET /v1/jobs/{id} の応答（S-8.6）", obj({
  "id": STR, "state": enum("queued", "launching", "submitted", "running", "stalled", "ok", "failed", "timed_out", "cancelled", "lost", "rejected"),
  "model": nullable(STR), "role": cref("role"), "workdir_id": cref("workdir_id"),
  "verify": obj({"rc": nullable(INT), "log_tail": STR}, [], extra=True),
  "report": obj({"status": enum("ready", "blocked", "failed", "dropped"), "human_action": nullable(STR), "text": STR}, [], extra=True),
  "changed_files": arr(STR), "started_at": nullable(cref("ts_any")), "ended_at": nullable(cref("ts_any"))}, ["id", "state", "role"], extra=True))
S("runner.schema.json", "config/runners/<名前>.yaml（F-7.2）", obj({
  "name": STR, "kind": enum("agent-cli", "chat-json"), "image": STR, "command": STR, "env": {"type": "object", "additionalProperties": STR},
  "timeout_s": {"type": "integer", "minimum": 1}, "idle_timeout_s": {"type": "integer", "minimum": 1}, "result": {"type": ["object", "string"]}},
  ["name", "kind", "image", "command"], extra=True))

# ---------------- environments ----------------
S("environment.schema.json", "実行環境の台帳の項目（S-25.2）", obj({
  "id": cref("plugin_id"), "adapter": enum("local", "ssh-host", "container", "cloud-sandbox"), "connect": {"type": "object"},
  "conn_credential": {"type": "object"}, "gate_credential": {"type": "object"}, "os": enum("linux", "windows", "macos"),
  "capabilities": arr({"type": "string", "pattern": "^[a-z0-9][a-z0-9:-]*$"}), "entrypoints": {"type": "object"},
  "data_classes": arr(cref("sensitivity")), "secrets_allowed": BOOL, "work_credentials": arr({"type": "object"}), "secret_targets": arr({"type": "object"}),
  "max_concurrent": {"type": "integer", "minimum": 0}, "cost": obj({"kind": enum("free", "fixed", "metered")}, ["kind"], extra=True),
  "lifetime": enum("permanent", "rental", "ephemeral"), "owner_use": BOOL, "model_family": STR, "rank": {"type": "integer"},
  "registered_at": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
  "free_guard": obj({"machines": {"const": 1}, "cost_max_usd": {"const": 0}}, ["machines", "cost_max_usd"]),
  "state": enum("trial", "available", "degraded", "excluded", "draining", "retired"),
  "gate_state": enum("trial", "available", "degraded", "excluded", "draining", "retired"),
  "module": {"type": "object"}, "probe": {"type": "object"}, "limits": {"type": "object"}, "runner": {"type": "object"}},
  ["id", "adapter", "capabilities", "data_classes", "registered_at"], extra=True,
  allOf=[{"if": {"properties": {"adapter": {"const": "cloud-sandbox"}}, "required": ["adapter"]},
          "then": {"required": ["free_guard", "cost", "max_concurrent"], "properties": {"max_concurrent": {"const": 1}, "cost": {"properties": {"kind": {"const": "free"}}}}}}]))

# ---------------- config files ----------------
S("error-rules.schema.json", "config/error-rules.yaml（S-2.3）", obj({
  "schema_version": {"const": 1},
  "rules": arr(obj({
    "class": cref("error_class"), "code": STR,
    "match": obj({"http_status": arr(INT), "http_status_range": arr(INT, minItems=2, maxItems=2), "body_regex": STR, "exception": arr(STR), "exit_code": arr(INT), "hint": arr(STR)}, []),
    "action": STR, "counts_as_failure": enum("yes", "no", "hold")}, ["class", "code", "match", "counts_as_failure"]), minItems=1)},
  ["schema_version", "rules"]))
S("defaults.schema.json", "config/defaults.yaml（S-17）", obj({
  "schema_version": {"const": 1},
  "defaults": {"type": "object", "additionalProperties": obj({
    "default": {}, "min": NUM, "max": NUM, "unit": STR, "used_in": STR, "note": STR, "range_text": STR}, ["default"])}},
  ["schema_version", "defaults"]))
S("profile.schema.json", "profile/<名前>.yaml（S-16.3、F-18.6）", obj({
  "host": STR, "bind_addr": STR, "secrets_dir": STR, "tmp_dir": STR, "archive_target": STR, "environments_file": STR,
  "memory_endpoint": STR, "model_gateway": STR, "models_file": STR, "ntfy_endpoint": STR, "ntfy_topic": STR, "commander_unit": {"oneOf": [STR, arr(STR)]},
  "ax_state_dir": STR, "commander_socket": STR, "seat_user": STR, "seat_socket": STR, "seats_file": STR, "seat_gate_path": STR,
  "gate_port": {"type": "integer", "minimum": 1, "maximum": 65535}, "gate_peers_file": STR, "archive_sources": STR,
  "env_ssh_targets": arr(obj({"id": STR, "host": STR, "port": {"type": "integer"}}, ["id", "host", "port"])),
  "instabox_api_base": STR, "instabox_machine_id": STR, "box_machine_name": STR, "box_machine_id": STR,
  "sandbox_gate_port": {"type": "integer"}, "archive_dest": obj({"host": STR, "port": {"type": "integer"}, "user": STR}, ["host", "port", "user"]),
  "public_proxy_ip": STR, "hq_access": {"type": "object"}},
  ["host", "bind_addr", "secrets_dir", "tmp_dir", "ax_state_dir", "commander_socket"], extra=True))
S("outbox-entry.schema.json", "outbox の1行（S-2.2、D-2.2。axcommon.events が書く）", obj({
  "target": enum("timeline", "ledger", "memory-connector"), "path": STR, "method": enum("POST", "PUT"),
  "body": {}, "queued_at": cref("ts"), "attempts": {"type": "integer", "minimum": 0}, "id": cref("ulid")},
  ["target", "path", "method", "body", "queued_at", "id"]))
print(sorted(p.name for p in OUT.glob("*.json")))
