"""Production construction for release; unavailable integrations remain closed."""
from pathlib import Path

from .registry import ProductRegistry
from .service import ReleaseService
from .store import Store
from ..gate.integration import GitPublicationSource, PersistentReleaseGate
from ..gate.publication import JsonlReviewStore, PublicationGate
from ..gate.adapters import RepositoryAllowlist
from ..gate.scanners import SecretScanner, InternalScanner, image_gps_clear, S8ReviewerSelector


class UnavailablePorts:
    def credentials(self, product):
        return False

    def __getattr__(self, name):
        raise NotImplementedError('release_dependency_unavailable')


class UnavailableSelector:
    def select(self, request):
        raise NotImplementedError('review_selector_unavailable')


def _unavailable(*args):
    raise NotImplementedError('publication_dependency_unavailable')


class ProductionReleaseService(ReleaseService):
    def __init__(self, *args, sink, **kwargs):
        super().__init__(*args, **kwargs)
        self.sink = sink

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        finally:
            try:
                self.store.flush(self.sink.send)
            except Exception:
                pass


def build_release_service(root, data_dir, host, sink, *, products_path=None,
                          pii_path=None, hosts_path=None, terms_path=None,
                          select_model=None, execute_model=None, source=None, ports=None, repositories_path=None):
    root, data_dir = Path(root), Path(data_dir)
    registry = ProductRegistry.load(products_path or root / 'config' / 'products.yaml')
    store = Store(data_dir / 'release.db', host=host)
    redactor = SecretScanner(pii_path or '/run/secrets/pii-list',
                             config_path=root / 'config' / 'redact.yaml')
    selector = S8ReviewerSelector(select_model or _unavailable, execute_model or _unavailable)
    gate = PublicationGate(redactor=redactor, pii_list_available=redactor.redactor.pii_list_ok,
                           internal_information_check=InternalScanner(
                               hosts_path or root / 'config' / 'publish-internal-hosts.txt',
                               terms_path or root / 'config' / 'publish-internal-terms.txt'),
                           image_gps_check=image_gps_clear, selector=selector,
                           records=JsonlReviewStore(data_dir / 'publish-reviews.jsonl'))
    persistent_gate = PersistentReleaseGate(gate, source or GitPublicationSource(RepositoryAllowlist(repositories_path or root / 'config' / 'publish-repositories.yaml')), store)
    return ProductionReleaseService(registry, store, ports or UnavailablePorts(), sink=sink,
                                    publication_gate=persistent_gate)
