#!/usr/bin/env python3
"""Prepare HQ configuration from an existing AX profile without printing values."""
import argparse
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
import yaml


def origin_value(value):
    try:
        url = urlsplit(value)
        hostname = url.hostname
        port = url.port
        if (url.scheme != 'https' or not hostname or not url.netloc
                or '@' in url.netloc or url.path or '?' in value or '#' in value
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        if url.netloc.startswith('['):
            ipaddress.IPv6Address(hostname)
            if not re.fullmatch(r'\[[0-9a-fA-F:.]+\](?::[0-9]+)?', url.netloc):
                raise ValueError
        elif not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]+)?', url.netloc):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError('origin must be an https origin without path or credentials')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('profile', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--origin', action='append', required=True, type=origin_value)
    args = parser.parse_args()
    profile = yaml.safe_load(args.profile.read_text())
    access = profile['hq_access']
    keys = ('iss', 'jwks_url', 'aud_secret', 'allowed_users_file', 'email_slot')
    if not isinstance(access, dict) or any(not isinstance(access.get(k), str) or not access[k] for k in keys):
        raise ValueError('profile_hq_access_invalid')
    for key in ('iss', 'jwks_url'):
        url = urlsplit(access[key])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError('profile_access_url_invalid')
    if access['aud_secret'] != 'hq-access' or access['allowed_users_file'] != 'hq-allowed-users':
        raise ValueError('profile_secret_names_mismatch')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}', access['email_slot']):
        raise ValueError('profile_email_slot_invalid')
    ipaddress.ip_address(profile['public_proxy_ip'])
    documents = {
        'hq-access.yaml': {'hq_access': {k: access[k] for k in keys}, 'origins': args.origin},
        'hq-allowed-users.yaml': {'file': access['allowed_users_file']},
        'secret-slots.yaml': {'slots': {access['email_slot']: {'file': access['email_slot'], 'target': {'local': True}, 'operations': ['write'], 'max_length': 65536, 'pattern': r'(?s).+'}}},
    }
    if not args.output.is_dir():
        raise ValueError('output_directory_required')
    if any((args.output / name).exists() for name in documents):
        raise ValueError('output_already_exists')
    for name, document in documents.items():
        fd = os.open(args.output / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            yaml.safe_dump(document, stream, sort_keys=False)


if __name__ == '__main__':
    main()
