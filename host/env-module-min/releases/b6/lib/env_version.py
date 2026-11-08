"""Read the version and content digest of the running environment release."""
import hashlib
import os
from pathlib import Path


def version(release: Path) -> dict:
    try:
        value = (release / 'VERSION').read_text(encoding='utf-8').strip()
        value = value if value and '\n' not in value and '\r' not in value else 'unknown'
    except (OSError, UnicodeError):
        value = 'unknown'
    try:
        digest = hashlib.sha256()
        def fail(error):
            raise error

        files = []
        for directory, dirs, names in os.walk(release, onerror=fail, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            files.extend(Path(directory) / name for name in dirs + names)
        files.sort()
        for path in files:
            if path.is_symlink():
                raise OSError('symlink in release')
            if path.is_dir():
                continue
            if not path.is_file():
                raise OSError('unsupported release entry')
            name = path.relative_to(release).as_posix().encode('utf-8')
            content = path.read_bytes()
            digest.update(len(name).to_bytes(8, 'big'))
            digest.update(name)
            digest.update(len(content).to_bytes(8, 'big'))
            digest.update(content)
        result = 'sha256:' + digest.hexdigest() if files else 'unknown'
    except (OSError, UnicodeError, ValueError):
        result = 'unknown'
    return {'version': value, 'digest': result}
