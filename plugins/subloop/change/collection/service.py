"""Injected collection API for the change graph (b14-4). No scheduler or daemon."""
from __future__ import annotations

import json

from axcommon.timeutil import parse_ts
from .models import GitHubClient, ItemKey, Observation, TargetRegistry
from .store import CollectionStore


class CollectionService:
    def __init__(self, store: CollectionStore, registry: TargetRegistry, client: GitHubClient,
                 *, permission_check=None, outbound_check=None):
        self.store, self.registry, self.client = store, registry, client
        self.permission_check = permission_check or (lambda repo, operation: False)
        self.outbound_check = outbound_check or (lambda text: False)
        self._recovered = False

    def collect(self, cycle_id: str) -> dict:
        cycle = self.store.cycle(cycle_id)
        result = {"targets": len(self.registry.targets), "discovered": 0, "errors": dict(self.registry.errors),
                  "dry_run": bool(cycle["dry_run"])}
        if cycle["mode"] not in {"run", "collect_only"}:
            return result
        for repo in self.registry.targets:
            if self.store.clock() >= cycle["started_at"] + cycle["seconds"]:
                result["budget_exhausted"] = True
                break
            try:
                for snapshot in self.client.list_items(repo, state="all"):
                    if self.store.clock() >= cycle["started_at"] + cycle["seconds"]:
                        result["budget_exhausted"] = True
                        break
                    # Never accept a cross-repo result supplied by the provider adapter.
                    if snapshot.key.issue_repo != repo:
                        continue
                    result["discovered"] += int(self.store.collect(snapshot, self.registry))
            except Exception as error:
                result["errors"][repo] = type(error).__name__
        return result

    def claim_next(self, cycle_id: str, owner: str, **kwargs):
        if not self._recovered:
            self.reconcile_external()
        else:
            for op in self.store.pending_operations():
                if op["name"] == "work":
                    item = self.store.get(ItemKey.decode(op["key"]))
                    if item["lease_until"] is not None and item["lease_until"] <= self.store.clock():
                        self._reconcile_operation(op)
        return self.store.claim_next(cycle_id, self.registry, owner, **kwargs)

    def _can_write(self, cycle_id, key, operation):
        cycle = self.store.cycle(cycle_id)
        return (cycle["mode"] == "run" and not cycle["dry_run"] and self.registry.resolve(key) is not None and
                self.store.clock() < cycle["started_at"] + cycle["seconds"] and
                self.permission_check(key.issue_repo, operation))

    def receipt(self, cycle_id: str, key: ItemKey, body: str) -> dict:
        item = self.store.get(key)
        if item is None or self.registry.resolve(key) is None:
            return {"state": "skipped", "reason": "unregistered_item"}
        marker = "<!-- ax:receipt:" + key.encode() + ":" + str(item["generation"]) + " -->"
        rendered = body + "\n" + marker
        if not self._can_write(cycle_id, key, "comment") or not self.outbound_check(rendered):
            return {"state": "skipped", "reason": "mode_or_permission_or_outbound_check", "planned": True}
        old = self.store.operation(key, "receipt")
        if old:
            self._reconcile_operation(old)
            return self.store.operation(key, "receipt")
        op = self.store.prepare_operation(key, "receipt", {"body": rendered, "marker": marker})
        if op is None:
            return self.store.operation(key, "receipt")
        try:
            external_id = self.client.post_comment(key, rendered, marker)
            if not external_id:
                raise ValueError("missing external comment id")
            self.store.update_operation(op, "submitted", str(external_id))
            op.update(state="submitted", external_id=str(external_id))
            self._reconcile_operation(op)
        except Exception:
            self.store.update_operation(op, "lost")
        return self.store.operation(key, "receipt")

    def sync_labels(self, cycle_id: str, key: ItemKey, label_state: str | None = None) -> dict:
        target = self.registry.resolve(key)
        item = self.store.get(key)
        if target is None or item is None:
            return {"state": "skipped", "reason": "unregistered_item"}
        current = item["hold"] or item["state"]
        if label_state is not None and label_state != current:
            raise ValueError("label state does not match current item state")
        label_state = current
        if label_state not in target.state_labels:
            raise ValueError("label state is not configured")
        actual = self.client.get_labels(key)
        managed = set(target.state_labels.values())
        desired = {target.state_labels[label_state]}
        add, remove = desired - actual, (actual & managed) - desired
        if not add and not remove:
            return {"state": "completed", "changed": False}
        if not self._can_write(cycle_id, key, "label"):
            return {"state": "skipped", "planned": True, "add": sorted(add), "remove": sorted(remove)}
        op = self.store.prepare_operation(key, "labels", {"desired": sorted(desired), "managed": sorted(managed)}, replace_completed=True)
        if op is None:
            return self.store.operation(key, "labels")
        try:
            self.client.change_labels(key, add=add, remove=remove)
            self._reconcile_operation(op)
        except Exception:
            self.store.update_operation(op, "lost")
        return self.store.operation(key, "labels")

    def update_rank(self, cycle_id: str, key: ItemKey, position: int) -> dict:
        if type(position) is not int or position < 1:
            raise ValueError("positive queue position required")
        receipt = self.store.operation(key, "receipt")
        if not receipt or receipt["state"] != "completed" or not receipt["external_id"]:
            return {"state": "skipped", "reason": "receipt_unconfirmed"}
        request = json.loads(receipt["request"])
        body = request["body"] + "\n\nQueue position: " + str(position)
        comment = self.client.get_comment(key, receipt["external_id"])
        if comment and comment.get("body") == body:
            return {"state": "completed", "changed": False}
        if not self._can_write(cycle_id, key, "comment") or not self.outbound_check(body):
            return {"state": "skipped", "planned": True}
        op = self.store.prepare_operation(key, "rank", {"body": body, "external_id": receipt["external_id"]}, replace_completed=True)
        if op is None:
            return self.store.operation(key, "rank")
        try:
            self.client.edit_comment(key, receipt["external_id"], body)
            self.store.update_operation(op, "submitted", receipt["external_id"])
            op["external_id"] = receipt["external_id"]
            self._reconcile_operation(op)
        except Exception:
            self.store.update_operation(op, "lost")
        return self.store.operation(key, "rank")

    def comments(self, key: ItemKey, *, cutover_time: str | None = None) -> dict:
        """Return raw input for the safety stage; this method never reopens or accepts requirements."""
        if self.registry.resolve(key) is None:
            return {"context": [], "new": []}
        comments = list(self.client.list_comments(key))
        own_ids = set()
        with self.store.transaction() as db:
            for row in db.execute("SELECT external_id FROM operations WHERE key=? AND name IN ('receipt','rank') AND external_id IS NOT NULL", (key.encode(),)):
                own_ids.add(str(row[0]))
        context, candidates = [], []
        cutoff = parse_ts(cutover_time) if cutover_time else None
        for comment in comments:
            cid = str(comment.get("comment_id", comment.get("id")))
            normalized = {**comment, "id": cid, "comment_id": cid, "repo": key.issue_repo,
                          "item_number": key.number, "revision": comment.get("revision", comment.get("updated_at")),
                          "origin": "ax" if cid in own_ids else comment.get("origin", "automation" if comment.get("is_bot") else "human")}
            created = comment.get("created_at")
            if cutoff is None or not created or parse_ts(created) <= cutoff:
                context.append(normalized)
            elif normalized["origin"] == "human":
                candidates.append(normalized)
        return {"context": context, "new": self.store.new_comments(key, candidates)}

    def _reconcile_operation(self, op):
        if op["state"] in {"completed", "failed", "cancelled", "ejected", "timed_out", "skipped", "missed"} or (op["state"] == "lost" and op["name"] != "work"):
            return
        key = ItemKey.decode(op["key"])
        request = json.loads(op["request"])
        try:
            if op["name"] == "work":
                observed = self.client.observe_work(key, op["external_id"]) if op["external_id"] else Observation("lost")
                self.store.reconcile_work(op, observed)
                return
            if op["name"] == "receipt":
                comment = self.client.get_comment(key, op["external_id"]) if op["external_id"] else None
                matched = comment is not None and request["marker"] in comment.get("body", "")
            elif op["name"] == "rank":
                comment = self.client.get_comment(key, request["external_id"])
                matched = comment is not None and comment.get("body") == request["body"]
            elif op["name"] == "labels":
                matched = self.client.get_labels(key) & set(request["managed"]) == set(request["desired"])
            else:
                matched = False
            self.store.update_operation(op, "completed" if matched else "lost")
        except Exception:
            if op["name"] == "work":
                self.store.reconcile_work(op, Observation("lost"))
            else:
                self.store.update_operation(op, "lost")

    def reconcile_external(self) -> list[dict]:
        pending = self.store.pending_operations()
        for operation in pending:
            self._reconcile_operation(operation)
        self._recovered = True
        return [{"key": op["key"], "name": op["name"],
                 "result": self.store.operation(ItemKey.decode(op["key"]), op["name"], op["generation"])} for op in pending]
