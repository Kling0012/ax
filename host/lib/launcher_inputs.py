"""Prepare optional launcher inputs without evaluating the installation env file."""
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yaml

PUBLISH = (
    ('AX_PUBLISH_REPOSITORIES_HOST_FILE', '/run/publish/repositories.yaml', False),
    ('AX_PUBLISH_REPOSITORY_HOST_DIR', '/publish-repositories', True),
    ('AX_PUBLISH_HOSTS_HOST_FILE', '/run/publish/internal-hosts', False),
    ('AX_PUBLISH_TERMS_HOST_FILE', '/run/publish/internal-terms', False),
)
CONFIG = {
    'resources': ('AX_ENVIRONMENTS_HOST_FILE', 'AX_ENVIRONMENTS_FILE', '/run/ax-config/environments.yaml'),
    'sl-release': ('AX_PRODUCTS_HOST_FILE', 'AX_PRODUCTS_FILE', '/run/ax-config/products.yaml'),
    'sl-change': ('AX_CHANGE_TARGETS_HOST_FILE', 'AX_CHANGE_TARGETS_FILE', '/run/ax-config/change-targets.yaml'),
}
RECIPES = 'AX_CHANGE_RECIPES_HOST_FILE'
RECIPE_TARGET = '/ax/plugins/subloop/change/verification/recipes.yaml'
URL = 'AX_SCHEDULED_PUBLISHER_URL_PREFIXES'
NAMES = {item[0] for item in PUBLISH} | {item[0] for item in CONFIG.values()} | {URL, RECIPES}
INVALID = object()


def plain(raw):
    if raw.startswith(("'", '"')):
        if len(raw) < 2 or raw[-1] != raw[0]:
            return INVALID
        raw = raw[1:-1]
    if any(c in raw for c in "\\$`'\"#") or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return INVALID
    return raw


def load(home):
    values = {}
    try:
        with (home / 'config' / 'launcher.env').open(encoding='utf-8', newline='') as stream:
            for line in stream:
                key, sep, raw = line.partition('=')
                key = key.strip()
                if key not in NAMES or key in os.environ:
                    continue
                if key in values or not sep:
                    values[key] = INVALID
                else:
                    values[key] = plain(raw.removesuffix('\n'))
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        values.update({name: INVALID for name in NAMES if name not in os.environ})
    values.update({name: os.environ[name] for name in NAMES if name in os.environ})
    return values


def path_value(value, directory=False):
    if not isinstance(value, str) or not value or '$' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
        return None
    return str(path.resolve())


def write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(content)


def url_value(value):
    if not isinstance(value, str) or plain(value) != value or any(c.isspace() for c in value):
        return ''
    if not value:
        return ''
    try:
        for prefix in value.split(','):
            url = urlsplit(prefix)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
                return ''
            _ = url.port
    except ValueError:
        return ''
    return value


def main():
    home, state, nonce, *services = sys.argv[1:]
    values = load(Path(home))
    output = Path(state) / 'deploy'
    fragments = []
    if 'sl-release' in services:
        paths = [path_value(values.get(key), directory) for key, _, directory in PUBLISH]
        if all(paths):
            fragment = {'services': {'sl-release': {'environment': {
                'AX_PUBLISH_REPOSITORIES_FILE': '/run/publish/repositories.yaml',
                'AX_PUBLISH_HOSTS_FILE': '/run/publish/internal-hosts',
                'AX_PUBLISH_TERMS_FILE': '/run/publish/internal-terms'},
                'volumes': [{'type': 'bind', 'source': path, 'target': target, 'read_only': True}
                            for path, (_, target, _) in zip(paths, PUBLISH)]}}}
            dest = output / f'publish-review-{nonce}.compose.yaml'
            write(dest, yaml.safe_dump(fragment, sort_keys=False))
            fragments.append(('fragment', dest))
        elif any(key in values for key, _, _ in PUBLISH):
            print('ax-up: publication review inputs unavailable', file=sys.stderr)
    for service, (key, container_key, target) in CONFIG.items():
        if service not in services or key not in values:
            continue
        path = path_value(values[key])
        # A configured but invalid source must not fall back to a bundled input.
        config = {'environment': {container_key: target if path else '/run/ax-config/invalid-input'}}
        if path:
            config['volumes'] = [{'type': 'bind', 'source': path, 'target': target, 'read_only': True}]
        else:
            print(f'ax-up: {key} unavailable', file=sys.stderr)
        dest = output / f'launcher-{service}-{nonce}.compose.yaml'
        write(dest, yaml.safe_dump({'services': {service: config}}, sort_keys=False))
        fragments.append(('fragment', dest))
    if 'sl-change' in services and RECIPES in values:
        path = path_value(values[RECIPES])
        if path is None:
            # Hide the bundled recipe when an explicitly configured input is invalid.
            sentinel = output / f'launcher-invalid-recipes-{nonce}'
            write(sentinel, '')
            path = str(sentinel)
            print('ax-up: AX_CHANGE_RECIPES_HOST_FILE unavailable', file=sys.stderr)
        dest = output / f'launcher-recipes-{nonce}.compose.yaml'
        service = {'volumes': [{'type': 'bind', 'source': path,
                                'target': RECIPE_TARGET, 'read_only': True}]}
        write(dest, yaml.safe_dump({'services': {'sl-change': service}}, sort_keys=False))
        fragments.append(('fragment', dest))
    if 'sl-scheduled-publisher' in services and URL in values:
        dest = output / f'launcher-url-{nonce}.env'
        write(dest, f'{URL}={url_value(values[URL])}\n')
        fragments.append(('env', dest))
    for kind, path in fragments:
        print(f'{kind} {path}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, yaml.YAMLError):
        print('ax-up: launcher inputs unavailable', file=sys.stderr)
        sys.exit(1)
