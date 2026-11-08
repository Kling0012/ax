"""Deployment permission adapter; card gateway must authenticate its evidence.

The injected card gateway is trusted deployment configuration, never item data.
Its verified_answer(scope) must independently authenticate the answer event and
return its immutable scope, expiry and decision. A todo's answer field alone is
not evidence: ledger creation permits a caller to supply that field.
"""
from datetime import datetime, timezone

from .ports import DependencyUnavailable


class PermissionAdapter:
    def __init__(self, *, control=None, cards=None, clock=None):
        self.control = control
        self.cards = cards
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def check(self, *, project, repo, destination, operation):
        if not all(isinstance(v, str) and v.strip() for v in (project, repo, destination, operation)):
            return {"allowed": False}
        try:
            result = self.control.check_permission(dict(project=project, repo=repo,
                                                        destination=destination, operation=operation))
            if isinstance(result, dict) and result.get("allowed") is True:
                return result
        except Exception:
            pass
        return {"allowed": False}

    def instruction(self, item, *, repo, pr, head_sha):
        if (not isinstance(repo, str) or not repo.strip() or type(pr) is not int or pr <= 0
                or not isinstance(head_sha, str) or not head_sha.strip()):
            return None
        scope = {"repo": repo, "pr": pr, "head_sha": head_sha, "operation": "merge"}
        try:
            answer = self.cards.verified_answer(dict(scope))
            if not isinstance(answer, dict) or answer.get("authenticated") is not True:
                return None
            if any(type(answer.get(k)) is not type(v) or answer.get(k) != v for k, v in scope.items()):
                return None
            if answer.get("decision") != "approve" or answer.get("active") is not True:
                return None
            if not all(isinstance(answer.get(k), str) and answer[k].strip() for k in ("card_id", "instruction_ref")):
                return None
            expiry = datetime.fromisoformat(answer["expires_at"].replace("Z", "+00:00"))
            now = self.clock()
            if expiry.tzinfo is None or now.tzinfo is None or expiry <= now:
                return None
        except Exception:
            return None
        return {**scope, "source": "user", "card_id": answer["card_id"],
                "instruction_ref": answer["instruction_ref"], "active": True}

    def wait(self, item, *, repo, pr, head_sha, reason):
        if not isinstance(repo, str) or not repo.strip() or not isinstance(reason, str) or not reason.strip():
            raise DependencyUnavailable("card_scope_invalid")
        try:
            result = self.cards.wait(item, repo=repo, pr=pr, head_sha=head_sha, reason=reason)
            if not isinstance(result, str) or not result.strip():
                raise ValueError("missing card id")
            return result
        except Exception as exc:
            raise DependencyUnavailable("card_unavailable") from exc


class OnlinePermissionAdapter(PermissionAdapter):
    """HQ is the sole authority for scoped, expiring individual approval."""
    def __init__(self, *, control, client, journal, clock=None):
        super().__init__(control=control, cards=None, clock=clock)
        self.client, self.journal = client, journal

    def authorize_scoped(self, item, *, repo, pr, head_sha, input_revision, operation):
        import hashlib
        import json
        from datetime import timedelta
        try:
            identity = {'project': item['project'], 'item': {k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')},
                        'repo': repo.lower(), 'pr': pr, 'head_sha': head_sha,
                        'input_revision': input_revision, 'operation': operation}
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if self.client is None:
                return {'status': 'unknown'}
            with self.journal.lock:
                saved = self.journal.get('online_approval', key)
                now = self.clock()
                if not saved or datetime.fromisoformat(saved['scope']['expires_at'].replace('Z', '+00:00')) <= now:
                    scope = {**identity, 'expires_at': (now + timedelta(hours=1)).isoformat()}
                    from axcommon.change_approval import validate_scope
                    validate_scope(scope)
                    request_id = 'change-' + hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    saved = {'request_id': request_id, 'scope': scope, 'approval_id': None}
                    self.journal.put('online_approval', key, saved)
                if saved.get('approval_id') is None:
                    response = self.client.request(saved['request_id'], saved['scope'])
                    approval_id = response.get('approval_id') if isinstance(response, dict) else None
                    if not isinstance(approval_id, str) or not approval_id:
                        return {'status': 'unknown'}
                    saved = {**saved, 'approval_id': approval_id}
                    self.journal.put('online_approval', key, saved)
            result = self.client.check(saved['approval_id'], saved['scope'])
            if (not isinstance(result, dict) or result.get('approval_id') != saved['approval_id'] or
                    result.get('scope') != saved['scope']):
                return {'status': 'unknown'}
            if datetime.fromisoformat(saved['scope']['expires_at'].replace('Z', '+00:00')) <= self.clock():
                return {'status': 'expired'}
            return result
        except Exception:
            return {'status': 'unknown'}

    def online_instruction(self, item, *, repo, pr, head_sha, input_revision):
        result = self.authorize_scoped(item, repo=repo, pr=pr, head_sha=head_sha,
                                input_revision=input_revision, operation='merge')
        if result.get('status') != 'approved':
            return None
        approval_id = result['approval_id']
        return {'repo': repo, 'pr': pr, 'head_sha': head_sha, 'operation': 'merge',
                'source': 'user', 'card_id': approval_id, 'instruction_ref': approval_id, 'active': True}

    def wait(self, item, *, repo, pr, head_sha, reason):
        # Requests are created with their full scope by authorize().
        raise DependencyUnavailable('online_approval_required')
