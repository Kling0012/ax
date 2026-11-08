"""In-memory GitHub/worker boundary. Never connects to a network."""
from copy import deepcopy
from plugins.subloop.change.collection import Observation


class FakeGitHub:
    def __init__(self):
        self.items = {}
        self.comments = {}
        self.labels = {}
        self.work = {}
        self.reads = []
        self.writes = []
        self.list_errors = set()
        self.fail_after_post = False
        self.fail_after_labels = False
        self._next_comment = 1

    def list_items(self, issue_repo, *, state):
        self.reads.append(("list_items", issue_repo, state))
        if issue_repo in self.list_errors:
            raise RuntimeError("provider unavailable")
        return list(self.items.get(issue_repo, []))

    def list_comments(self, key):
        self.reads.append(("list_comments", key))
        return deepcopy(list(self.comments.get(key, {}).values()))

    def get_comment(self, key, external_id):
        self.reads.append(("get_comment", key, external_id))
        return deepcopy(self.comments.get(key, {}).get(str(external_id)))

    def post_comment(self, key, body, marker):
        cid = str(self._next_comment)
        self._next_comment += 1
        self.comments.setdefault(key, {})[cid] = {"id": cid, "body": body, "marker": marker,
            "author": "example-owner", "is_bot": False, "created_at": "2026-11-07T01:00:00.000Z", "revision": "r1"}
        self.writes.append(("comment", key, cid))
        if self.fail_after_post:
            raise RuntimeError("response lost after posting")
        return cid

    def edit_comment(self, key, external_id, body):
        self.comments[key][str(external_id)]["body"] = body
        self.writes.append(("edit_comment", key, str(external_id)))

    def get_labels(self, key):
        self.reads.append(("get_labels", key))
        return set(self.labels.get(key, set()))

    def change_labels(self, key, *, add, remove):
        self.labels[key] = (self.get_labels(key) - remove) | add
        self.writes.append(("labels", key, set(add), set(remove)))
        if self.fail_after_labels:
            raise RuntimeError("response lost after labels")

    def observe_work(self, key, external_id):
        self.reads.append(("observe_work", key, external_id))
        return self.work.get(str(external_id), Observation("lost"))
