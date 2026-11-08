"""Fail-closed resource registry client for outbound embedding checks."""
from axcommon.client import ApiClient


class EmbeddingRegistryClient(ApiClient):
    def _registered(self, **params):
        try:
            result = self.get("/v1/embedding-registration", params=params)
            return isinstance(result, dict) and result.get("registered") is True
        except Exception:
            return False

    def registered_model(self, provider, model):
        return self._registered(provider=provider, model=model)

    def registered_embedding_endpoint(self, provider, model, endpoint):
        return self._registered(provider=provider, model=model, endpoint=endpoint)
