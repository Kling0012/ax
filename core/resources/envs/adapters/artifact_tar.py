"""Decode an environment's artifact tar without extracting paths to disk."""
import hashlib
import io
import re
import tarfile


def decode_artifact_tar(data):
    if not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
        raise ValueError('invalid artifact archive')
    artifacts = []
    names = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            for item in archive:
                if (not item.isfile() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', item.name)
                        or item.name in names or item.size > 32 * 1024 * 1024):
                    raise ValueError('invalid artifact')
                names.add(item.name)
                content = archive.extractfile(item).read()
                if len(content) != item.size:
                    raise ValueError('truncated artifact')
                artifacts.append({'name': item.name, 'size': len(content),
                                  'sha256': hashlib.sha256(content).hexdigest(), 'content': content})
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError('invalid artifact archive') from error
    return {'artifacts': artifacts}
