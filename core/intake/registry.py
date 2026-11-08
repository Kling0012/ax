"""行き先の登録簿（S-6.3、D-5）。

置き場: `config/intake-destinations.yaml`（基点）と `config/intake-destinations.d/*.yaml`（後続の束の
行き先。PLAN.json の conventions）。行き先の追加と削除は、このファイルの変更だけで済ませる。

読み直し: 起動時と、ファイルの更新時刻が変わったとき。読み直しで Schema が壊れていたら、前の登録簿を
使い続け、`health.alert` を書く（入口全体を止めない。D-5）。

指摘されるもの（1 行 1 件。名前だけを含み、値は含まない）:
- tier か via が無い行は登録しない（S-6.9）。
- irreversible と tier が食い違う行は登録しない（S-6.3）。
- Schema が読めない、または payload 用の Schema として壊れている行は登録しない（入口全体を止めない）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from axcommon import schemas, timeutil

log = logging.getLogger("axintake.registry")

TIERS = ("read", "operate", "irreversible")
VIAS = ("local", "ssh", "tailnet", "hq")
FROM_VALUES = ("commander", "hq")
ACK_TYPES = ("sync", "async")

FAULT_CODES = ("tier_via_missing", "irreversible_tier_mismatch", "schema_load_failed")


def repo_config_dir() -> Path:
    env = os.environ.get("AX_CONFIG_DIR")
    if env:
        return Path(env)
    for cand in (Path("/ax/config"), Path(__file__).resolve().parent.parent.parent / "config"):
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent.parent.parent / "config"


def parse_files(paths: list[Path]) -> tuple[list[dict], list[str]]:
    """登録簿の候補の YAML を読んで（壊れた行は `faults` に名前だけ）、行き先の素の一覧を返す。"""
    docs: list[dict] = []
    faults: list[str] = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            faults.append("registry_file_unreadable")
            log.error("登録簿 %s が読めない（%s）", p.name, type(e).__name__)
            continue
        try:
            import yaml

            doc = yaml.safe_load(text)
        except Exception:  # noqa: BLE001  壊れた YAML は fault として扱う（fail-soft）
            faults.append("registry_file_broken")
            log.error("登録簿 %s が YAML でない", p.name)
            continue
        errs = schemas.errors("intake-destinations", doc if doc is not None else [])
        if errs:
            faults.append("registry_schema_failed")
            log.error("登録簿 %s が Schema に合わない（%s）", p.name, "; ".join(errs[:3]))
            continue
        docs.extend(doc if isinstance(doc, list) else [])
    return docs, faults


def schema_name(spec: Any) -> str | None:
    """登録簿の schema 欄を schemas/ の中の名前に直す。`schemas/dest/x.json` は `dest/x.json`。"""
    if not isinstance(spec, str) or not spec:
        return None
    name = spec[len("schemas/"):] if spec.startswith("schemas/") else spec
    return name


def schema_available(name: str | None) -> tuple[bool, str]:
    """payload の Schema が読めるか。読めなければ (False, 原因)。

    `schema_missing` は schemas/dest/ がまだ無いだけ（B12b2 が作る。S-6.22）。
    `schema_error` は壊れている（登録しない）。
    """
    if not name:
        return False, "schema_missing"
    fname = name if name.endswith(".json") else name + ".schema.json"
    p = Path(schemas.schemas_dir()) / fname
    if not p.is_file():
        return False, "schema_missing"
    try:
        schemas.validator(name)  # 読めてメタスキーマに合うか（$ref の解決も確かめる）
        return True, ""
    except Exception:  # noqa: BLE001  JSON でない・メタスキーマ違反・$ref が解けない
        return False, "schema_error"


def unresolved_contract(destination: dict | None, schema: dict | None = None) -> str | None:
    """Return the declared reason when a destination has no executable handler."""
    marker = schema.get("x-ax-unresolved") if isinstance(schema, dict) else None
    if marker:
        return str(marker)
    handler = destination.get("handler") if isinstance(destination, dict) else None
    return handler if isinstance(handler, str) and handler.startswith("not-implemented:") else None


def payload_contract(destination: dict | None, schema: dict) -> dict:
    """Expose a read-only unresolved-contract marker for the effective registry entry."""
    resolved = dict(schema)
    if reason := unresolved_contract(destination, schema):
        resolved.setdefault("x-ax-unresolved", reason)
    return resolved


def build_registry(docs: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """行き先の一覧から、使える登録簿と指摘の一覧を作る（重複は後勝ち）。"""
    raw: dict[str, dict] = {}
    for d in docs:
        if isinstance(d, dict) and isinstance(d.get("name"), str):
            raw[d["name"]] = d  # 後勝ち（.d/*.yaml が基点を上書きできる）
    entries: dict[str, dict] = {}
    faults: list[str] = []
    for name, d in raw.items():
        entry = dict(d)
        entry.setdefault("irreversible", entry.get("tier") == "irreversible")
        entry.setdefault("ack", "sync")
        if entry.get("tier") not in TIERS or not entry.get("via"):
            faults.append("tier_via_missing")  # tier と via は必須（S-6.9）
            log.error("行き先 %s: tier か via が無い。登録しない", name)
            continue
        if entry.get("irreversible") != (entry.get("tier") == "irreversible"):
            faults.append("irreversible_tier_mismatch")  # 食い違いは登録簿の検査で落とす（S-6.3）
            log.error("行き先 %s: irreversible と tier が食い違う。登録しない", name)
            continue
        if not conflict_key_valid(entry.get("conflict_key")):
            faults.append("conflict_key_invalid")  # 式は書けない（S-6.3）
            log.error("行き先 %s: conflict_key の書式が不正。登録しない", name)
            continue
        name_schema = schema_name(entry.get("schema"))
        ok, why = schema_available(name_schema)
        if name_schema and why.startswith("schema_error"):
            faults.append("schema_load_failed")  # 壊れた Schema の行き先は登録しない
            log.error("行き先 %s: payload の Schema が壊れている。登録しない", name)
            continue
        entry["_schema_name"] = name_schema if ok else None
        entry["_schema_missing"] = not ok
        if not ok:
            # schemas/dest/ の本体は B12b2 が作る（S-6.22）。それまでは登録だけして検査は緩く続ける
            faults.append("payload_schema_missing")
        entries[name] = entry
    return entries, faults


def discover_files(config_dir: Path, *, main_file: str = "intake-destinations.yaml",
                   domain: str = "intake-destinations.d") -> list[Path]:
    """登録簿のファイル一覧（基点 + .d/*.yaml の名前順）。"""
    config_dir = Path(config_dir)
    main = config_dir / main_file
    out = [main] if main.is_file() else []
    d = config_dir / domain
    if d.is_dir():
        out += sorted(p for p in d.glob("*.yaml") if p.name != ".gitkeep")
    return out


class Registry:
    def __init__(self, entries: dict[str, dict], config_dir: Path, *,
                 main_file: str = "intake-destinations.yaml", domain: str = "intake-destinations.d"):
        self._entries = dict(entries)
        self._config_dir = Path(config_dir)
        self._main_file = main_file
        self._domain = domain
        self.reload_reason = "startup"
        self.reload_count = 1
        self.faults: list[str] = []
        self._mtimes = self._read_mtimes()

    @classmethod
    def load(cls, config_dir: Path, **kw) -> "Registry":
        """登録簿を読んで作る（起動時）。"""
        docs, faults = parse_files(discover_files(Path(config_dir), **{k: v for k, v in kw.items() if k in ("main_file", "domain")}))
        entries, f2 = build_registry(docs)
        r = cls(entries, config_dir, **{k: v for k, v in kw.items() if k in ("main_file", "domain")})
        r.faults = faults + f2
        return r

    def entries(self) -> dict[str, dict]:
        return dict(self._entries)

    def get(self, name: str) -> dict | None:
        return self._entries.get(name)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def digest(self) -> str:
        """登録簿の複合の記録（正規形の sha256。壊れた行は含めない）。"""
        body = json.dumps([self._entries[k] for k in sorted(self._entries)], sort_keys=True,
                          ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _files(self) -> list[Path]:
        main = self._config_dir / self._main_file
        out = [main] if main.is_file() else []
        d = self._config_dir / self._domain
        if d.is_dir():
            out += sorted(p for p in d.glob("*.yaml") if p.name != ".gitkeep")
        return out

    def _read_mtimes(self) -> dict[str, float]:
        out = {}
        for p in self._files():
            try:
                out[str(p)] = p.stat().st_mtime
            except OSError:
                continue
        return out

    def maybe_reload(self, *, emit_alert=None) -> bool:
        """更新時刻の変わったファイルがあるときだけ読み直す。壊れていたら前の登録簿を保つ（D-5）。

        `registry_*` の指摘（YAML の破損・Schema 違反）のときは前の登録簿を使い続け、`health.alert` を書く。
        行き先の削除（指摘なしで減ること）は利用者の変更なので従う。
        """
        if self._read_mtimes() == self._mtimes:
            return False
        docs: list[dict] = []
        faults: list[str] = []
        for p in self._files():
            d, f = parse_files([p])
            docs += d
            faults += f
        new_entries, f2 = build_registry(docs)
        faults += f2
        broken = [f for f in faults if f.startswith("registry_")]
        self._mtimes = self._read_mtimes()
        if broken:
            # 壊れた読み直しは前の登録簿を保つ（D-5。入口全体を止めない）
            self.faults = faults
            if emit_alert is not None:
                emit_alert("registry_broken", {"faults": sorted(set(faults))})
            return False
        self._entries = new_entries
        self.faults = faults
        self.reload_count += 1
        self.reload_reason = "reload"
        if faults and emit_alert is not None:
            emit_alert("registry_reload_faults", {"faults": sorted(set(faults))})
        return True


class ReloadWatch:
    """更新時刻で読み直す係（D-5）。待つ間は止めない（fail-soft）。"""

    def __init__(self, registry: Registry, interval_s: float = 5.0, *, emit_alert=None):
        self.registry = registry
        self.interval_s = float(interval_s)
        self.emit_alert = emit_alert
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="intake-registry-reload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.registry.maybe_reload(emit_alert=self.emit_alert)
            except Exception:  # noqa: BLE001  1 回の失敗で係を止めない
                log.exception("登録簿の読み直しに失敗（前の登録簿を使い続ける）")


def payload_hash(payload: Any) -> str:
    """payload の正規形の hash（S-6.16 依頼 ID の照合と S-6.17 の意図）。"""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_CONFLICT_REF = re.compile(r"^\{[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\}$")
_CONFLICT_LITERAL = re.compile(r"^[A-Za-z0-9_:\-]*$")


def conflict_key_valid(template: Any) -> bool:
    """conflict_key は payload の項目の参照だけを書け、式は書けない（S-6.3、S-6.17）。

    `{target}`、`{args.name}` のような参照と、`:` などの区切りの文字だけを許す。
    """
    if not isinstance(template, str) or not template:
        return True
    rest = template
    for m in re.finditer(r"\{[^{}]*\}", template):
        if not _CONFLICT_REF.match(m.group(0)):
            return False
        rest = rest.replace(m.group(0), "", 1)
    return "{" not in rest and "}" not in rest and bool(_CONFLICT_LITERAL.match(rest))


def render_conflict_key(template: str, payload: Any) -> str:
    """conflict_key の書式を payload で埋める。無い項目は空にする（S-6.17）。"""
    if not isinstance(template, str):
        return ""
    out: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch == "{":
            j = template.find("}", i)
            if j < 0:
                out.append(template[i:])
                break
            out.append(_deref(template[i + 1:j], payload))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _deref(path: str, payload: Any) -> str:
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return ""
        cur = cur[part]
    return "" if cur is None else str(cur)


def ts_expires_at(entry: dict, now: Any = None) -> str | None:
    """default_expiry_min から期限の時刻（UTC、ミリ秒まで）を作る。登録簿に無ければ None。"""
    from datetime import timedelta

    minutes = entry.get("default_expiry_min")
    if not minutes:
        return None
    now = now or timeutil.now()
    return timeutil.format_ts(now + timedelta(minutes=int(minutes)))
