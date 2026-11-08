"""GitHub REST adapter for collection and the change merge boundary.

The API origin and evidence callback are deployment configuration, never item input.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from urllib.parse import quote

from axcommon.client import ApiClient, Unavailable
from ..collection.models import ItemKey, Observation, Snapshot
from .gate import full_sha


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise Unavailable(type(error).__name__) from error


class GitHub:
    def __init__(self, registry, token, *, base_url="https://api.github.com", evidence=None, timeout=10):
        self.registry = registry
        self.evidence = evidence
        self.api = ApiClient(base_url, token, timeout=timeout, transport=_transport,
                             extra_headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    def _repo(self, repo, *, issue=False):
        repo = ItemKey("github", repo, "issue", 1).issue_repo
        allowed = set(self.registry.targets) if issue else {t.implementation_repo for t in self.registry.targets.values()}
        if repo not in allowed:
            raise ValueError("unregistered repository")
        return repo

    def _key(self, key):
        self._repo(key.issue_repo, issue=True)
        return f"/repos/{key.issue_repo}/issues/{key.number}"

    @staticmethod
    def _number(value):
        if type(value) is not int or value <= 0:
            raise ValueError("positive number required")
        return value

    def _pages(self, path, *, field=None, params=None):
        result = []
        for page in range(1, 10001):
            data = self.api.get(path, params={**(params or {}), "per_page": 100, "page": page})
            rows = data.get(field) if field and isinstance(data, dict) else data
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid paginated response")
            result.extend(rows)
            if not rows:
                return result
        raise ValueError("pagination limit exceeded")

    def list_items(self, issue_repo, *, state):
        repo = self._repo(issue_repo, issue=True)
        issues = self._pages(f"/repos/{repo}/issues", params={"state": state})
        pulls = self._pages(f"/repos/{repo}/pulls", params={"state": state})
        result = []
        for kind, rows in (("issue", [x for x in issues if "pull_request" not in x]), ("pr", pulls)):
            for row in rows:
                key = ItemKey("github", repo, kind, row["number"])
                version = row.get("updated_at")
                if not isinstance(version, str) or not version:
                    raise ValueError("missing item revision")
                result.append(Snapshot(key, version, tuple(x["name"] for x in row.get("labels", [])),
                                       row["state"], row.get("body") or "",
                                       {"repo": repo, "number": key.number} if kind == "pr" else None))
        return result

    def list_comments(self, key):
        return [{**row, "is_bot": (row.get("user") or {}).get("type") == "Bot"}
                for row in self._pages(self._key(key) + "/comments")]

    def get_comment(self, key, external_id):
        self._key(key)
        cid = self._number(int(external_id))
        # The list endpoint binds a comment to the requested issue, unlike /issues/comments/{id}.
        return next((c for c in self.list_comments(key) if c.get("id") == cid), None)

    def post_comment(self, key, body, marker):
        if marker not in body:
            raise ValueError("receipt marker missing")
        return str(self.api.post(self._key(key) + "/comments", {"body": body})["id"])

    def edit_comment(self, key, external_id, body):
        if self.get_comment(key, external_id) is None:
            raise ValueError("comment does not belong to item")
        self.api.request("PATCH", f"/repos/{key.issue_repo}/issues/comments/{self._number(int(external_id))}", json_body={"body": body})

    def get_labels(self, key):
        return {row["name"] for row in self._pages(self._key(key) + "/labels")}

    def change_labels(self, key, *, add, remove):
        path = self._key(key) + "/labels"
        if add:
            self.api.post(path, {"labels": sorted(add)})
        for label in sorted(remove):
            self.api.request("DELETE", path + "/" + quote(label, safe=""))

    def observe_work(self, key, external_id):
        self._key(key)
        return Observation("lost", external_id)

    def _pull(self, repo, pr):
        repo = self._repo(repo)
        pr = self._number(pr)
        row = self.api.get(f"/repos/{repo}/pulls/{pr}")
        if not isinstance(row, dict) or row.get("number") != pr or (row.get("base", {}).get("repo") or {}).get("full_name", "").lower() != repo:
            raise ValueError("pull request identity mismatch")
        return repo, row

    def view(self, item, target):
        repo = self._repo(item["implementation_repo"])
        issue_repo = self._repo(item["issue_repo"], issue=True)
        registered = self.registry.targets[issue_repo]
        if registered.implementation_repo != repo or target.get("implementation_repo", "").lower() != repo or target.get("issue_repo", "").lower() != issue_repo:
            raise ValueError("target mismatch")
        if item.get("kind") == "issue" and item.get("pr") is None:
            return self._issue_view(item, registered, repo, issue_repo)
        pr = self._number(item["pr"])
        repo, pull = self._pull(repo, pr)
        head, base = pull["head"]["sha"], pull["base"]["sha"]
        if not full_sha(head) or not full_sha(base):
            raise ValueError("invalid pull request revision")
        prefix = f"/repos/{repo}"
        comments = self._pages(f"{prefix}/issues/{pr}/comments")
        review_comments = self._pages(f"{prefix}/pulls/{pr}/comments")
        source_comments = []
        source_issue = None
        if item.get("kind") == "issue":
            issue_number = self._number(item.get("number"))
            source_issue = self.api.get(f"/repos/{issue_repo}/issues/{issue_number}")
            if not isinstance(source_issue, dict) or source_issue.get("number") != issue_number:
                raise ValueError("source issue identity mismatch")
            source_comments = self._pages(f"/repos/{issue_repo}/issues/{issue_number}/comments")
        files = self._pages(f"{prefix}/pulls/{pr}/files")
        if pull.get("changed_files") is None or len(files) != pull["changed_files"]:
            raise ValueError("incomplete changed files")
        changed_files = []
        file_status = {"added": "added", "modified": "modified", "removed": "deleted", "renamed": "renamed"}
        files_complete = True
        for entry in files:
            status = file_status.get(entry.get("status"))
            path = entry.get("filename")
            previous = entry.get("previous_filename")
            if (not status or not isinstance(path, str) or not path or
                    (status == "renamed" and (not isinstance(previous, str) or not previous))):
                files_complete = False
                break
            # GitHub omits patches for binaries and may omit them for other
            # reasons. Treat either case as binary/unknown, never as text.
            row = {"path": path, "status": status, "binary": not isinstance(entry.get("patch"), str)}
            if status == "renamed":
                row["old_path"] = previous
            changed_files.append(row)
        statuses = self._pages(f"{prefix}/commits/{head}/statuses")
        checks = self._pages(f"{prefix}/commits/{head}/check-runs", field="check_runs")
        permission = self.api.get(prefix)
        if not isinstance(permission, dict) or permission.get("full_name", "").lower() != repo:
            raise ValueError("repository identity mismatch")
        candidates = []
        for status in statuses:
            candidates.append((status.get("context"), status.get("updated_at") or status.get("created_at") or "", status.get("state"), status.get("sha", head)))
        for check in checks:
            candidates.append((check.get("name"), check.get("completed_at") or check.get("started_at") or "", check.get("conclusion") if check.get("status") == "completed" else check.get("status"), check.get("head_sha")))
        ci = []
        for source in registered.ci_sources:
            matches = [x for x in candidates if x[0] == source]
            if matches:
                latest = max(x[1] for x in matches)
                ci.extend({"source": source, "status": x[2], "head_sha": x[3], "origin": "configured"} for x in matches if x[1] == latest)
        headers = {"Accept": "application/vnd.github.v3.diff", "X-GitHub-Api-Version": "2022-11-28"}
        if self.api._token:
            headers["Authorization"] = "Bearer " + self.api._token
        status, _, diff_bytes = self.api._transport("GET", self.api.base_url + f"{prefix}/pulls/{pr}", headers, None, self.api.timeout)
        if status != 200:
            raise Unavailable("pull_diff_unavailable")
        diff = diff_bytes.decode("utf-8")
        body = json.dumps({"title": pull.get("title"), "body": pull.get("body"), "comments": comments,
                           "review_comments": review_comments, "source_issue": source_issue, "source_comments": source_comments}, ensure_ascii=False, sort_keys=True)
        raw = {"pull": pull, "comments": comments, "review_comments": review_comments, "source_comments": source_comments, "source_issue": source_issue, "files": files, "diff": diff}
        revision = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        facts = {"repo": repo, "pr": pr, "head_sha": head, "base_sha": base,
                 "state": pull.get("state"), "draft": pull.get("draft"), "mergeable": pull.get("mergeable"),
                 "merged": pull.get("merged"), "merged_at": pull.get("merged_at"), "merge_commit_sha": pull.get("merge_commit_sha"),
                 "changed_paths": [f["filename"] for f in files], "files_complete": files_complete,
                 "changed_files": changed_files if files_complete else [], "comments_complete": True,
                 "comments": comments, "review_comments": review_comments, "source_comments": source_comments, "body": body, "diff": diff, "input_revision": revision,
                 "ci_complete": True, "ci": ci,
                 "can_merge": permission.get("permissions", {}).get("push") is True}
        evidence = self.evidence(item, target, facts.copy()) if self.evidence else {}
        if not isinstance(evidence, dict):
            raise ValueError("invalid evidence")
        # Only explicitly trusted evidence fields may augment provider facts.
        allowed = {"unprocessed_comments", "requirements_revision", "required_ids", "requirements", "publication_head", "outbound_checked", "collect_only_reviewed_head"}
        return {**{k: evidence[k] for k in allowed if k in evidence}, **facts}

    def _issue_view(self, item, registered, repo, issue_repo):
        number = self._number(item.get("number"))
        project, base_ref = registered.project, registered.base_ref
        if not project or not base_ref or item.get("project") != project:
            raise ValueError("issue base configuration unavailable")
        issue = self.api.get(f"/repos/{issue_repo}/issues/{number}")
        if (not isinstance(issue, dict) or issue.get("number") != number or
                "pull_request" in issue or not isinstance(issue.get("updated_at"), str) or
                not issue["updated_at"]):
            raise ValueError("source issue identity or revision mismatch")
        comments = self._pages(f"/repos/{issue_repo}/issues/{number}/comments")
        ref = self.api.get(f"/repos/{repo}/git/ref/heads/{quote(base_ref, safe='')}")
        sha = (ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not full_sha(sha):
            raise ValueError("issue base revision unavailable")
        source = {"issue": issue, "comments": comments, "base_ref": base_ref, "base_sha": sha,
                  "issue_repo": issue_repo, "implementation_repo": repo, "project": project}
        revision = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        body = json.dumps({"title": issue.get("title"), "body": issue.get("body"), "comments": comments},
                          ensure_ascii=False, sort_keys=True)
        return {"snapshot_type": "issue", "repo": repo, "source_issue_repo": issue_repo,
                "source_issue_number": number, "project": project, "base_ref": base_ref,
                "pr": None, "head_sha": sha, "base_sha": sha, "input_revision": revision,
                "comments_complete": True, "comments": comments, "body": body, "diff": ""}

    def merge(self, repo, pr, *, sha, method):
        repo, pull = self._pull(repo, pr)
        if not full_sha(sha) or pull.get("head", {}).get("sha") != sha or method not in {"merge", "squash", "rebase"}:
            raise ValueError("invalid expected head or merge method")
        result = self.api.put(f"/repos/{repo}/pulls/{pr}/merge", {"sha": sha, "merge_method": method})
        if not isinstance(result, dict) or result.get("merged") is not True or not full_sha(result.get("sha")):
            raise ValueError("merge unconfirmed")
        return {"merged": True, "head_sha": sha, "merge_commit_sha": result["sha"]}

    def reconcile_merge(self, repo, pr, *, sha):
        if not full_sha(sha):
            raise ValueError("invalid expected head")
        repo, pull = self._pull(repo, pr)
        merge_sha = pull.get("merge_commit_sha")
        if pull.get("merged") is True and pull.get("merged_at") and pull.get("head", {}).get("sha") == sha and full_sha(merge_sha):
            return {"merged": True, "head_sha": sha, "merge_commit_sha": merge_sha, "merged_at": pull["merged_at"]}
        return {"merged": False, "state": "lost"}
