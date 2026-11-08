"""Registered OpenAI-compatible embedding endpoint with a mandatory outbound guard."""
from __future__ import annotations

import math
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from axcommon.redact import from_environment


class EmbeddingUnavailable(Exception):
    """Safe, non-sensitive reason why indexing and search are unavailable."""


class OpenAIEmbedding:
    def __init__(self, *, endpoint, token_file, dimensions, resources, redactor, timeout=10.0):
        self.endpoint, self.token_file = endpoint, token_file
        self.dimensions, self.resources, self.redactor, self.timeout = dimensions, resources, redactor, timeout

    @classmethod
    def from_environment(cls, resources, environ=None):
        env = os.environ if environ is None else environ
        try:
            dimensions = int(env.get('AX_EMBEDDING_DIMENSIONS', '0'))
        except ValueError:
            dimensions = 0
        return cls(endpoint=env.get('AX_EMBEDDING_URL', ''), token_file=env.get('AX_EMBEDDING_TOKEN_FILE', ''),
                   dimensions=dimensions, resources=resources, redactor=from_environment(env))

    def validate_text(self, text):
        r = self.redactor
        try:
            if not r.index_path or not r.pii_list_path or not r.reload_index_if_changed() or not r.reload_pii_list():
                raise EmbeddingUnavailable('inspection_unavailable')
            if r.scan(text):
                raise EmbeddingUnavailable('sensitive_text')
        except EmbeddingUnavailable:
            raise
        except Exception:
            raise EmbeddingUnavailable('inspection_unavailable') from None

    def embed(self, provider, model, text):
        self.validate_text(text)
        try:
            registered = (self.resources.registered_model(provider, model) and
                          self.resources.registered_embedding_endpoint(provider, model, self.endpoint))
        except Exception:
            registered = False
        if not registered:
            raise EmbeddingUnavailable('unregistered_embedding_endpoint')
        if 'claude' in provider.casefold() or 'claude' in model.casefold() or 'anthropic' in provider.casefold():
            raise EmbeddingUnavailable('unsupported_provider')
        try:
            url = urlsplit(self.endpoint)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or not url.path.endswith('/v1/embeddings'):
                raise ValueError
            if type(self.dimensions) is not int or self.dimensions < 1:
                raise ValueError
        except ValueError:
            raise EmbeddingUnavailable('invalid_configuration') from None
        try:
            token = Path(self.token_file).read_text(encoding='utf-8').strip() if self.token_file else ''
        except (OSError, ValueError):
            token = ''
        if not token:
            raise EmbeddingUnavailable('credential_unavailable')
        try:
            response = httpx.post(self.endpoint, headers={'Authorization': 'Bearer ' + token},
                                  json={'model': model, 'input': text}, timeout=self.timeout, follow_redirects=False, trust_env=False)
            response.raise_for_status()
            data = response.json()['data']
            if len(data) != 1 or data[0].get('index') != 0:
                raise ValueError
            vector = data[0]['embedding']
            if not isinstance(vector, list):
                raise ValueError
            if len(vector) != self.dimensions:
                raise EmbeddingUnavailable('dimension_mismatch')
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
                raise ValueError
            return vector
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise EmbeddingUnavailable('provider_unavailable_or_invalid_response') from None
