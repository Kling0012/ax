"""Public CI correlation; the caller supplies durable state and external operations."""
from __future__ import annotations

NODE_HELPER = True

import re
from datetime import datetime, timezone

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_JOB = re.compile(r"shard ([1-9][0-9]*)\Z")


def _timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class PublicCI:
    """An adapter and state store are injected; failures never produce a result status.

    State store implements get(key), put(key, value). Adapter methods are dispatch,
    runs, run, jobs, artifacts, collect_artifact, statuses and set_status. The
    collect_artifact implementation must validate, decrypt and persist the archive.
    """

    def __init__(self, adapter, store, clock):
        self.adapter, self.store, self.clock = adapter, store, clock

    def trigger(self, private_repo, public_repo, sha, mode, *, ref="main", workflow="ci.yml"):
        if not (_REPO.fullmatch(private_repo) and _REPO.fullmatch(public_repo)
                and _SHA.fullmatch(sha) and mode in ("quick", "full")):
            raise ValueError("invalid CI request")
        key = f"dispatch:{public_repo}:{private_repo}:{sha}:{mode}"
        previous = self.store.get(key)
        now = self.clock()
        if previous:
            return previous if previous.get("run_id") else self.identify(previous, key)
        # Persist before dispatch, including on an ambiguous network failure.
        request = dict(private_repo=private_repo, public_repo=public_repo, private_sha=sha,
                       mode=mode, ref=ref, workflow=workflow, started=now, run_id=None)
        self.store.put(key, request.copy())
        self.adapter.dispatch(public_repo, workflow, ref, {"sha": sha, "mode": mode})
        return self.identify(request, key)

    def identify(self, request, key=None):
        title = f"ci {request['private_sha']} {request['mode']}"
        matching = [r for r in self.adapter.runs(request["public_repo"], request["workflow"])
                    if r.get("display_title") == title and r.get("head_branch") == request["ref"]
                    and _timestamp(r["created_at"]) >= request["started"]]
        if matching:
            request = {**request, "run_id": min(r["id"] for r in matching)}
            if key:
                self.store.put(key, request.copy())
        return request

    def collect(self, request, *, force=False):
        rid = request.get("run_id")
        if not isinstance(rid, int) or rid <= 0:
            return {"collected": False, "reason": "run_not_identified"}
        try:
            run = self.adapter.run(request["public_repo"], rid)
            attempt = run["run_attempt"]
            if (run.get("id") != rid or not isinstance(attempt, int) or attempt < 1
                    or run.get("display_title") != f"ci {request['private_sha']} {request['mode']}"
                    or run.get("head_branch") != request["ref"]
                    or _timestamp(run["created_at"]) < request["started"]):
                return {"collected": False, "reason": "run_mismatch"}
            if run.get("status") != "completed":
                return {"collected": False, "reason": "running"}
            if run.get("conclusion") not in ("success", "failure"):
                return {"collected": False, "reason": "unsupported_conclusion"}
            key = f"collect:{request['public_repo']}:{rid}:{attempt}"
            state = self.store.get(key) or {}
            now = self.clock()
            if state.get("collected"):
                return state
            if not force and now < state.get("next_check", 0):
                return {**state, "reason": "retry_wait"}
            jobs = self.adapter.jobs(request["public_repo"], rid, attempt)
            names = set()
            for job in jobs:
                if job.get("conclusion") == "skipped":
                    continue
                name = job.get("name", "")
                if name == "plan":
                    names.add(f"plan-{attempt}")
                elif match := _JOB.fullmatch(name):
                    if int(match.group(1)) > 256:
                        raise ValueError("invalid shard")
                    names.add(f"shard-{match.group(1)}-{attempt}")
                else:
                    raise ValueError("unknown job")
            if f"plan-{attempt}" not in names:
                raise ValueError("missing plan")
            artifacts = self.adapter.artifacts(request["public_repo"], rid)
            selected = {}
            for art in artifacts:
                if art.get("name") in names and not art.get("expired"):
                    if art["name"] in selected:
                        raise ValueError("duplicate artifact")
                    selected[art["name"]] = art
            if set(selected) != names:
                raise ValueError("missing artifact")
            for name in sorted(names):
                # The adapter verifies the archive contents and saves to the matching tuple.
                self.adapter.collect_artifact(request, rid, attempt, name, selected[name])
            statuses = self.adapter.statuses(request["private_repo"], request["private_sha"])
            current = next((s for s in statuses if s.get("context") == "ci/public"), None)
            result = run["conclusion"]
            if request["mode"] == "quick" and current and current.get("description", "").startswith("full:"):
                raise ValueError("full status takes precedence")
            description = f"{request['mode']}: {result}"
            if not current or current.get("state") != result or current.get("description") != description or current.get("target_url") != run.get("html_url"):
                self.adapter.set_status(request["private_repo"], request["private_sha"], result,
                                        "ci/public", description, run["html_url"])
            state = dict(collected=True, run_id=rid, attempt=attempt, private_repo=request["private_repo"],
                         private_sha=request["private_sha"], mode=request["mode"], result=result,
                         artifacts=sorted(names))
            self.store.put(key, state)
            return state
        except Exception as exc:
            # A failed fetch/decryption/status update remains uncollected. Older runs back off.
            attempt = locals().get("attempt")
            if not isinstance(attempt, int) or attempt < 1:
                return {"collected": False, "reason": type(exc).__name__}
            key = f"collect:{request['public_repo']}:{rid}:{attempt}"
            previous = self.store.get(key) or {}
            count = previous.get("failures", 0) + 1
            age = max(0, self.clock() - request["started"])
            delay = min(3600, 60 * (2 ** min(count - 1, 6))) if age >= 3600 else 60
            state = dict(collected=False, failures=count, next_check=self.clock() + delay,
                         reason=type(exc).__name__)
            self.store.put(key, state)
            return state
