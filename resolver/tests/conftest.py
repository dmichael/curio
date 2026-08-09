import pytest
from starlette.testclient import TestClient

from resolver import app as app_module

app_module.get_settings.cache_clear()


@pytest.fixture(scope="session")
def http_client():
    """One TestClient for the whole run. The MCP session manager driven by the
    app's lifespan can only be started once per process, so route-level tests
    must share a single lifespan instead of opening their own TestClient.
    Routes read get_settings() per request, so tests can still vary settings
    around this long-lived client (clear the lru_cache after monkeypatching)."""
    app_module.get_settings.cache_clear()
    with TestClient(app_module.app) as tc:
        yield tc
    app_module.get_settings.cache_clear()
