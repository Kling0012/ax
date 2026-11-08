"""schemas/ の読み込みと検証（D-2.2 axcommon.schemas、S-16.1）。

schemas/*.json は JSON Schema Draft 2020-12。`$id` は https://ax.invalid/schemas/<ファイル名>。ファイル間の `$ref` は相対（common.schema.json#/$defs/ulid など）。
置き場の探し方: 環境変数 AX_SCHEMAS_DIR > コンテナの /ax/schemas > リポジトリの schemas/（このファイルの隣）。

CLI:
  python3 -m axcommon.schemas --check                  全スキーマをメタスキーマで検査し、$ref が解決できることも確かめる（tools/ci.sh の(1)）
  python3 -m axcommon.schemas --validate NAME FILE     FILE（JSON か YAML）を NAME で検査する
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

BASE_URI = "https://ax.invalid/schemas/"


class SchemaError(Exception):
    """スキーマが見つからない、または壊れている。"""


class ValidationFailed(ValueError):
    """インスタンスがスキーマに合わない。errors は人が読める文字列の一覧。"""

    def __init__(self, name: str, errors: list[str]):
        super().__init__(f"{name}: " + "; ".join(errors[:5]))
        self.name = name
        self.errors = errors


def schemas_dir() -> Path:
    env = os.environ.get("AX_SCHEMAS_DIR")
    if env:
        return Path(env)
    for cand in (Path("/ax/schemas"), Path(__file__).resolve().parent.parent / "schemas"):
        if cand.is_dir():
            return cand
    raise SchemaError("schemas/ が見つからない（AX_SCHEMAS_DIR を設定する）")


def _norm(name: str) -> str:
    return name if name.endswith(".json") else name + ".schema.json"


@lru_cache(maxsize=None)
def _load_cached(directory: str, fname: str) -> dict:
    p = Path(directory) / fname
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise SchemaError(f"スキーマが読めない: {fname}: {e.strerror}") from e
    except ValueError as e:
        raise SchemaError(f"スキーマが JSON でない: {fname}") from e


def load(name: str) -> dict:
    """スキーマを返す。name は 'event' でも 'event.schema.json' でもよい。"""
    return _load_cached(str(schemas_dir()), _norm(name))


def all_names() -> list[str]:
    return sorted(p.name for p in schemas_dir().glob("*.json"))


def clear_cache() -> None:
    _load_cached.cache_clear()


def _jsonschema():
    try:
        import jsonschema
    except ImportError as e:  # pragma: no cover
        raise SchemaError("jsonschema が無い") from e
    return jsonschema


def _all_docs() -> dict[str, dict]:
    return {BASE_URI + n: load(n) for n in all_names()}


def validator(name: str, fragment: str | None = None):
    """name のスキーマの検査器（Draft202012Validator）。fragment は '/$defs/request' のような JSON ポインタ。"""
    js = _jsonschema()
    schema = load(name)
    docs = _all_docs()
    target: Any = schema
    if fragment:
        for part in fragment.strip("/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        # 断片の中の相対 $ref と内部 $ref のために、断片を元の文書の中の $ref として包む
        target = {"$ref": f"{schema['$id']}#{fragment}"}
    try:  # jsonschema 4.18 以降（referencing）
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        reg = Registry().with_resources((uri, Resource.from_contents(doc, default_specification=DRAFT202012)) for uri, doc in docs.items())
        return js.Draft202012Validator(target, registry=reg, format_checker=js.Draft202012Validator.FORMAT_CHECKER)
    except ImportError:  # 古い jsonschema（RefResolver）
        resolver = js.RefResolver(base_uri=schema["$id"], referrer=schema, store=docs)
        return js.Draft202012Validator(target, resolver=resolver)


def _fmt(err) -> str:
    path = "/".join(str(p) for p in err.absolute_path)
    return f"{path or '(root)'}: {err.message}"


def errors(name: str, instance: Any, fragment: str | None = None) -> list[str]:
    v = validator(name, fragment)
    return sorted((_fmt(e) for e in v.iter_errors(instance)))


def validate(name: str, instance: Any, fragment: str | None = None) -> None:
    errs = errors(name, instance, fragment)
    if errs:
        raise ValidationFailed(name, errs)


def is_valid(name: str, instance: Any, fragment: str | None = None) -> bool:
    return not errors(name, instance, fragment)


def check_all() -> list[str]:
    """全スキーマのメタスキーマ検査と $ref の解決。問題の文字列の一覧を返す（空なら合格）。"""
    js = _jsonschema()
    bad: list[str] = []
    names = all_names()
    if not names:
        return ["schemas/ にスキーマが無い"]
    docs = {}
    for n in names:
        try:
            d = load(n)
            js.Draft202012Validator.check_schema(d)
            docs[n] = d
        except SchemaError as e:
            bad.append(str(e))
        except js.SchemaError as e:
            bad.append(f"{n}: メタスキーマ違反: {e.message}")
            continue
        if n in docs:
            if d.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                bad.append(f"{n}: $schema が Draft 2020-12 でない")
            if d.get("$id") != BASE_URI + n:
                bad.append(f"{n}: $id が {BASE_URI}{n} でない")
    # $ref の解決: 全 $ref を集め、対象の文書と断片が実在するか調べる
    for n, d in docs.items():
        for ref in _refs(d):
            doc_name, _, frag = ref.partition("#")
            tgt = docs.get(doc_name) if doc_name else d
            if tgt is None:
                bad.append(f"{n}: $ref の先の文書が無い: {ref}")
                continue
            node: Any = tgt
            try:
                for part in [p for p in frag.strip("/").split("/") if p]:
                    node = node[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                bad.append(f"{n}: $ref の先の断片が無い: {ref}")
    return bad


def _refs(node: Any):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def load_environments(items: list[dict]) -> tuple[list[dict], list[tuple[str, list[str]]]]:
    """環境台帳の項目（S-25.2）を 1 件ずつ検査する。(読み込めた物, [(id, 理由)]) を返す。
    cloud-sandbox の free_guard 違反（cost.kind が free でない、max_concurrent が 1 でない、free_guard が違う）は
    その環境だけを拒否し、他の環境は読み込む（S-25.2）。"""
    ok, rejected = [], []
    for it in items:
        errs = errors("environment", it)
        if errs:
            rejected.append((str(it.get("id", "?")), errs))
        else:
            ok.append(it)
    return ok, rejected


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="axcommon.schemas")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--validate", nargs=2, metavar=("NAME", "FILE"))
    a = ap.parse_args(argv)
    if a.check:
        try:
            bad = check_all()
        except SchemaError as e:
            print(f"NG: {e}", file=sys.stderr)
            return 1
        for b in bad:
            print("NG:", b, file=sys.stderr)
        if bad:
            return 1
        print(f"OK: schemas/ の {len(all_names())} 件がメタスキーマ（Draft 2020-12）に合い、$ref が解決できる")
        return 0
    if a.validate:
        name, f = a.validate
        text = Path(f).read_text(encoding="utf-8")
        try:
            inst = json.loads(text)
        except ValueError:
            import yaml

            inst = yaml.safe_load(text)
        errs = errors(name, inst)
        for e in errs:
            print("NG:", e)
        if not errs:
            print("OK")
        return 1 if errs else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
