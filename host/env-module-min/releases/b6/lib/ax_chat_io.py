"""Fixed, bounded chat-json runner input and output within a job workdir."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

MAX_RESPONSE = 131072
MAX_PROMPT = 131072
MODEL = re.compile(r'^[A-Za-z0-9._:/@+-]{1,120}$')


def _directory(work: Path) -> int:
    return os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def prepare(work: Path, request: dict) -> None:
    model, prompt = request.get('model'), request.get('prompt')
    if (not isinstance(model, str) or not MODEL.fullmatch(model) or not isinstance(prompt, str)
            or not prompt.strip() or len(prompt.encode('utf-8')) > MAX_PROMPT):
        raise ValueError('invalid chat request')
    document = {'model': model, 'max_tokens': 4096, 'messages': [{'role': 'user', 'content': prompt}]}
    payload = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    directory = _directory(work)
    try:
        try:
            os.stat('response.json', dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('stale chat response')
        fd = os.open('request.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
    finally:
        os.close(directory)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate json key')
        result[key] = value
    return result


def collect(work: Path) -> dict:
    directory = _directory(work)
    try:
        fd = os.open('response.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('invalid response file')
            payload = source.read(MAX_RESPONSE + 1)
    finally:
        os.close(directory)
    if len(payload) > MAX_RESPONSE:
        raise ValueError('response too large')
    def reject_constant(_):
        raise ValueError('invalid json number')
    result = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=reject_constant)
    if not isinstance(result, dict) or set(result) - {'ok', 'exit_code', 'json', 'text', 'usage', 'model', 'stop_reason'}:
        raise ValueError('invalid response envelope')
    if result.get('ok') is not True or type(result.get('exit_code')) is not int or result['exit_code'] != 0 or 'json' not in result:
        raise ValueError('unsuccessful response')
    if ('text' in result and not isinstance(result['text'], str)
            or 'model' in result and not isinstance(result['model'], str)
            or 'stop_reason' in result and result['stop_reason'] is not None and not isinstance(result['stop_reason'], str)
            or 'usage' in result and (not isinstance(result['usage'], dict)
                or any(type(value) is not int for value in result['usage'].values()))):
        raise ValueError('invalid response envelope')
    clean = {'ok': True, 'exit_code': 0, 'json': result['json']}
    if len(json.dumps(clean, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_RESPONSE:
        raise ValueError('response too large')
    return clean
