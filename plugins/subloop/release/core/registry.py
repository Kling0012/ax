"""Product data, version policy and dry-run precedence. No host discovery."""
from __future__ import annotations

from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import Path
import re

import yaml


class Held(ValueError):
    """A local publication precondition failed; other products can continue."""


class ProductRegistry:
    def __init__(self, document: dict):
        if document.get('schema_version') != 1 or not isinstance(document.get('products'), list):
            raise Held('invalid_product_registry')
        self.products = {}
        repos = set()
        for source in document['products']:
            p = deepcopy(source)
            required = ('id', 'project', 'repo', 'destination', 'operations', 'publish_paths',
                        'version', 'notes', 'entry', 'checks', 'post_publish')
            if any(k not in p for k in required):
                raise Held('product_fields_missing')
            if p['id'] in self.products or p['repo'] in repos:
                raise Held('duplicate_product')
            if not all(isinstance(p[k], str) and p[k] for k in ('id', 'project', 'repo', 'destination')):
                raise Held('invalid_product_identity')
            entry = p['entry']
            if not p['publish_paths'] or not p['operations'] or not isinstance(entry, dict):
                raise Held('invalid_product_scope')
            if entry.get('kind') == 'entrypoint' and not entry.get('requires'):
                raise Held('invalid_product_entry')
            if entry.get('kind') == 'unresolved' and not entry.get('reason'):
                raise Held('invalid_product_entry')
            if p['version'].get('kind') not in ('semver', 'increment_last'):
                raise Held('unknown_version_policy')
            try:
                re.compile(p['version']['tag_pattern'])
            except (KeyError, re.error):
                raise Held('invalid_tag_pattern') from None
            for key in ('quiet_min', 'max_wait_min', 'lock_wait_min', 'build_wait_min'):
                value = p.get(key)
                if value is not None and (type(value) is not int or value < 1):
                    raise Held('invalid_product_deadline')
            if type(p.get('enabled', True)) is not bool or type(p.get('dry_run', True)) is not bool:
                raise Held('invalid_product_mode')
            self.products[p['id']] = p
            repos.add(p['repo'])

    @classmethod
    def load(cls, path: str | Path):
        return cls(yaml.safe_load(Path(path).read_text(encoding='utf-8')))

    def get(self, product_id: str) -> dict:
        if product_id not in self.products:
            raise Held('product_unregistered')
        return deepcopy(self.products[product_id])

    def for_repo(self, repo: str) -> dict:
        found = [p for p in self.products.values() if p['repo'] == repo]
        if len(found) != 1:
            raise Held('product_unregistered')
        return deepcopy(found[0])


def target_changed(product: dict, paths: list[str] | None) -> bool:
    if paths is None or not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise Held('changed_paths_unavailable')
    return any(fnmatchcase(path, pattern) for path in paths for pattern in product['publish_paths'])


def dry_run(*layers: dict) -> bool:
    """Call with explicit, environment, repo, global in that order."""
    for layer in layers:
        if 'dry_run' in layer and layer['dry_run'] is not None:
            value = layer['dry_run']
            if value in ('true', '1'):
                return True
            if value in ('false', '0'):
                return False
            if type(value) is not bool:
                raise Held('invalid_dry_run')
            return value
    return True


def next_version(product: dict, latest_tag: str | None, changes: list[dict]) -> str:
    policy = product['version']
    if not isinstance(latest_tag, str) or re.fullmatch(policy['tag_pattern'], latest_tag) is None:
        raise Held('latest_tag_unavailable_or_invalid')
    if policy['kind'] == 'increment_last':
        match = re.search(r'(\d+)(?!.*\d)', latest_tag)
        if not match:
            raise Held('tag_has_no_number')
        return latest_tag[:match.start()] + str(int(match.group()) + 1) + latest_tag[match.end():]
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', latest_tag)
    if not match or not changes:
        raise Held('version_changes_unavailable')
    rules = policy.get('bump', {})
    levels = []
    for change in changes:
        kind = rules.get(change.get('kind'))
        if kind not in ('major', 'minor', 'patch'):
            raise Held('unclassified_version_change')
        levels.append(('patch', 'minor', 'major').index(kind))
    major, minor, patch = map(int, match.groups())
    level = max(levels)
    return f'v{major + 1}.0.0' if level == 2 else (f'v{major}.{minor + 1}.0' if level == 1 else f'v{major}.{minor}.{patch + 1}')


def make_notes(product: dict, version: str, changes: list[dict], *, prs: list[int], diff_ref: str,
               fragment_refs: list[str]) -> dict:
    if not diff_ref:
        raise Held('notes_diff_reference_required')
    sections = {}
    for language in product['notes']['languages']:
        lines = [f'# {version}', '']
        for change in changes:
            if change.get('kind') not in product['notes']['include']:
                continue
            text = (change.get('text') or {}).get(language)
            if not isinstance(text, str) or not text.strip():
                raise Held('notes_language_missing')
            lines.append('- ' + text.strip())
        if len(lines) == 2:
            raise Held('notes_empty')
        sections[language] = '\n'.join(lines) + '\n'
    return {'version': version, 'languages': sections,
            'sources': {'prs': list(prs), 'diff_ref': diff_ref, 'fragment_refs': list(fragment_refs)}}
