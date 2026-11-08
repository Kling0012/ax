"""S-19.5 orchestration through injected, bounded execution ports only."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from .evidence import EvidenceCache, compare, complete, make_evidence
from .policy import GateError, check_prepared, hosts, inspect_changes, matches
from .privacy import privacy_command


def load_recipes():
    # JSON is a YAML subset; keeps this boundary usable without a YAML dependency.
    return json.loads(Path(__file__).with_name("recipes.yaml").read_text(encoding="utf-8"))


def _request(request):
    if not isinstance(request, dict):
        raise GateError("request_invalid")
    for key in ("base_sha", "head_sha"):
        if not isinstance(request.get(key), str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", request[key]):
            raise GateError("full_sha_required")
    if not isinstance(request.get("repo"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request["repo"]):
        raise GateError("repo_invalid")
    if type(request.get("untrusted")) is not bool:
        raise GateError("trust_unconfirmed")
    if type(request.get("balance_changed", False)) is not bool:
        raise GateError("request_invalid")
    attempts = request.get("attempts", ["head-1", "head-2"] if request["untrusted"] else ["head-1"])
    if (not isinstance(attempts, list) or len(attempts) != (2 if request["untrusted"] else 1)
            or any(not isinstance(a, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", a) or a == "base" for a in attempts)
            or len(set(attempts)) != len(attempts)):
        raise GateError("independent_attempts_required")
    return attempts


def _recipe(request, recipes, paths):
    ref = request.get("test_recipe")
    if not isinstance(ref, str):
        raise GateError("recipe_unconfigured")
    if "#" in ref:
        source, ref = ref.rsplit("#", 1)
        if source not in ("verification/recipes.yaml", "plugins/subloop/change/verification/recipes.yaml"):
            raise GateError("recipe_unconfigured")
    recipe = deepcopy(recipes.get(ref))
    if not isinstance(recipe, dict):
        raise GateError("recipe_unconfigured")
    if not isinstance(recipe.get("version"), str) or not recipe["version"] or not isinstance(recipe.get("runner"), str) or not recipe["runner"]:
        raise GateError("recipe_unconfigured")
    if request.get("verify_runner", recipe["runner"]) != recipe["runner"]:
        raise GateError("verify_runner_mismatch")
    if type(recipe.get("timeout_s")) is not int or not 1 <= recipe["timeout_s"] <= 2400:
        raise GateError("recipe_unconfigured")
    if not isinstance(recipe.get("dependency", []), list) or any(not isinstance(c, str) or not c for c in recipe.get("dependency", [])):
        raise GateError("recipe_unconfigured")
    checks = recipe.get("checks")
    if not isinstance(checks, list) or not checks:
        raise GateError("recipe_unconfigured")
    if any(not isinstance(c, dict) or not isinstance(c.get("command"), str) or not c["command"] or not isinstance(c.get("kind"), str) or not c["kind"] for c in checks):
        raise GateError("recipe_unconfigured")
    for check in checks:
        if isinstance(check, dict) and check.get("command") == "{privacy_check}":
            check["command"] = privacy_command()
    if request["untrusted"]:
        full = recipe.get("all")
        if not isinstance(full, dict):
            # Repeating every configured check is the full available suite (hibi has no unit suite).
            full = {"kind": "all", "command": " && ".join(f"({c['command']})" for c in checks)}
        checks = [c for c in checks if c.get("kind") != "changed"]
        if not any(c.get("kind") == full["kind"] for c in checks):
            checks.append(full)
    elif any(matches(p, pattern) for p in paths for pattern in recipe.get("full_paths", [])):
        if not recipe.get("all"):
            raise GateError("recipe_unconfigured")
        checks.append(recipe["all"])
    if request.get("balance_changed", False) or any(matches(p, pattern) for p in paths for pattern in recipe.get("slow_paths", [])):
        if recipe.get("slow"):
            if not any(c.get("kind") == "all" for c in checks):
                checks.append(recipe["all"])
            checks.append(recipe["slow"])
    if any(not isinstance(c, dict) or not isinstance(c.get("command"), str) or not c["command"] or not isinstance(c.get("kind"), str) or not c["kind"] for c in checks):
        raise GateError("recipe_unconfigured")
    if len({c["kind"] for c in checks}) != len(checks):
        raise GateError("recipe_unconfigured")
    recipe["checks"] = checks
    return recipe


def _now():
    return datetime.now(timezone.utc).isoformat()


def run_verification(request: dict, runner, *, scrub, cache=None, recipes=None) -> dict:
    """Return status/reason/evidence/comparisons/unperformed; never authorize a merge.

    `scrub(obj) -> obj` is mandatory and must remove both credentials and PII.
    Runner.prepare attests the checked-out revision and isolation before execution.
    Runner.execute returns explicit completeness, timing, test names and diagnostics.
    """
    out = {"status": "parked", "reason": "verify_unavailable", "evidence": [], "comparisons": [], "unperformed": []}
    cache = cache if cache is not None else EvidenceCache()
    try:
        if not callable(scrub):
            raise GateError("redactor_unavailable")
        attempts = _request(request)
        paths = inspect_changes(request)
        recipe = _recipe(request, recipes if recipes is not None else load_recipes(), paths)
        allow = hosts(request.get("outbound_allowlist", []))
        if recipe.get("dependency") and not allow:
            raise GateError("dependency_allowlist_missing")
        out["unperformed"] = list(recipe.get("unperformed", []))
        # Current recipe content and outbound policy are pinned in addition to S-19.5's five fields.
        scope = json.dumps({"repo": request["repo"], "base": request["base_sha"], "recipe": recipe,
                            "allow": allow, "untrusted": request["untrusted"],
                            "forbidden": request.get("untrusted_forbidden_paths", [])}, sort_keys=True)
        revisions = [("base", request["base_sha"], "base")] if request["untrusted"] else []
        revisions += [("head", request["head_sha"], attempt) for attempt in attempts]
        by_attempt = {}
        used_execution_ids = set()
        for role, sha, attempt in revisions:
            group_id = "verify-" + uuid.uuid4().hex
            invocations = []
            for check in recipe["checks"]:
                invocations.append({"job_id": "verify-" + uuid.uuid4().hex, "workdir_id": group_id,
                                    "subject_sha": sha, "revision_role": role, "attempt": attempt,
                                    "recipe_version": recipe["version"], "environment": recipe["runner"],
                                    "command": check["command"], "validation_kind": check["kind"],
                                    "stage": "verify", "allow_hosts": [], "timeout_s": recipe["timeout_s"],
                                    "repo": request["repo"], "ro_mounts": recipe.get("ro_mounts", [])})
            expected = [dict(i, head_sha=sha, base_sha=request["base_sha"]) for i in invocations]
            reused = [cache.get(scope, e) for e in expected]
            if not all(reused):
                for index, command in enumerate(recipe.get("dependency", [])):
                    inv = dict(invocations[0], job_id="verify-" + uuid.uuid4().hex, command=command,
                               validation_kind=f"dependency-{index + 1}", stage="dependency", allow_hosts=allow)
                    bind = getattr(runner, "bind_invocation", None)
                    if callable(bind):
                        inv = bind(inv)
                    evidence = _execute(request, inv, runner, scrub)
                    out["evidence"].append(evidence)
                    if not complete(evidence):
                        raise GateError("evidence_incomplete")
                    if evidence["exit_code"] != 0:
                        out.update(status="failed", reason="dependency_failed")
                        return out
            checks = []
            for invocation, cached in zip(invocations, reused):
                bind = getattr(runner, "bind_invocation", None)
                if callable(bind):
                    invocation = bind(invocation)
                evidence = cached if cached is not None else _execute(request, invocation, runner, scrub)
                if evidence.get("execution_id") in used_execution_ids:
                    raise GateError("independent_attempts_required")
                used_execution_ids.add(evidence.get("execution_id"))
                out["evidence"].append(evidence)
                if not complete(evidence):
                    raise GateError("evidence_incomplete")
                cache.put(scope, evidence)
                checks.append(evidence)
            by_attempt[attempt] = checks
        if request["untrusted"]:
            for attempt in attempts:
                for base, head in zip(by_attempt["base"], by_attempt[attempt]):
                    out["comparisons"].append(compare(base, head))
            failed = next((c for c in out["comparisons"] if not c["accepted"]), None)
            if failed:
                out.update(status="failed", reason=failed["reason"])
                return out
        elif any(e["exit_code"] != 0 for e in by_attempt[attempts[0]]):
            out.update(status="failed", reason="verification_failed")
            return out
        out.update(status="passed", reason="verified")
        return out
    except GateError as exc:
        out["reason"] = exc.reason
    except (ConnectionError, OSError, NotImplementedError):
        out["reason"] = "verify_unavailable"
    except Exception:
        # No raw exception text: it can include command output, credentials or PII.
        out["reason"] = "verification_internal"
    return out


def _execute(request, invocation, runner, scrub):
    prepared = runner.prepare(deepcopy(invocation))
    check_prepared(invocation, prepared, untrusted=request["untrusted"])
    if (invocation["validation_kind"] == "privacy" or "/work/privacy-patterns" in invocation["command"]):
        if prepared.get("privacy_patterns_staged") is not True:
            raise GateError("privacy_patterns_unavailable")
    started = _now()
    try:
        result = runner.execute(deepcopy(invocation), deepcopy(prepared))
    except TimeoutError:
        result = {"job_id": invocation["job_id"], "sha": invocation["subject_sha"], "executed": False,
                  "started_at": started, "ended_at": _now(), "exit_code": None, "timed_out": True,
                  "complete": False, "output_truncated": False, "output_tail": "", "test_names": None}
    except (ConnectionError, OSError, NotImplementedError):
        raise GateError("verify_unavailable")
    return make_evidence(request, invocation, result, scrub)
