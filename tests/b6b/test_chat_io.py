"""Fixed host-side chat-json input and bounded output."""
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / 'host/env-module-min/releases/b6/lib/ax_chat_io.py'
spec = importlib.util.spec_from_file_location('ax_chat_io_test', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_fixed_request_and_successful_output(tmp_path):
    module.prepare(tmp_path, {'model': 'example/model', 'prompt': 'inspect'})
    assert json.loads((tmp_path / 'request.json').read_text()) == {
        'model': 'example/model', 'max_tokens': 4096, 'messages': [{'role': 'user', 'content': 'inspect'}]}
    (tmp_path / 'response.json').write_text(json.dumps({'ok': True, 'exit_code': 0, 'json': {'safe': True}, 'text': 'not returned'}))
    assert module.collect(tmp_path) == {'ok': True, 'exit_code': 0, 'json': {'safe': True}}
    with pytest.raises((ValueError, OSError)):
        module.prepare(tmp_path, {'model': 'example/model', 'prompt': 'again'})


@pytest.mark.parametrize('text', [
    '{"ok":false,"exit_code":0,"json":{}}', '{"ok":true,"exit_code":true,"json":{}}',
    '{"ok":true,"exit_code":0,"json":{},"unknown":1}', '{"ok":true,"ok":false,"exit_code":0,"json":{}}',
    '{"ok":true,"exit_code":0,"json":NaN}', 'x' * (module.MAX_RESPONSE + 1),
])
def test_bad_response_is_rejected(tmp_path, text):
    (tmp_path / 'response.json').write_text(text)
    with pytest.raises((ValueError, OSError)):
        module.collect(tmp_path)


def test_symlink_and_existing_request_are_rejected(tmp_path):
    other = tmp_path / 'other'
    other.write_text('{"ok":true,"exit_code":0,"json":{}}')
    (tmp_path / 'response.json').symlink_to(other)
    with pytest.raises((ValueError, OSError)):
        module.collect(tmp_path)
    with pytest.raises((ValueError, OSError)):
        module.prepare(tmp_path, {'model': 'example/model', 'prompt': 'test'})
