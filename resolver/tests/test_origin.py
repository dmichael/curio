"""Trusted reverse-proxy origin selection stays opt-in and fail-closed."""

from contextlib import contextmanager

import pytest

from resolver import app as app_module


@pytest.fixture
def origin_env(monkeypatch):
    for name in (
        "RESOLVER_PUBLIC_BASE_URL",
        "RESOLVER_TRUSTED_PROXY_CIDRS",
    ):
        monkeypatch.delenv(name, raising=False)
    app_module.get_settings.cache_clear()
    yield monkeypatch
    app_module.get_settings.cache_clear()


@contextmanager
def immediate_proxy(http_client, address="127.0.0.1"):
    old_client = http_client._transport.client
    http_client._transport.client = (address, 50000)
    try:
        yield
    finally:
        http_client._transport.client = old_client


def test_untrusted_forwarded_headers_are_ignored(http_client, origin_env):
    response = http_client.get(
        "/resolve",
        params={"ref": "ipfs://bafyCID/a.png"},
        headers={
            "Host": "direct.example",
            "Forwarded": "for=198.51.100.7;proto=https;host=forged.example",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "forged.example",
        },
    )
    assert response.json()["media_url"] == "http://direct.example/ipfs/bafyCID/a.png"


def test_trusted_proxy_accepts_rfc_forwarded_origin(http_client, origin_env):
    origin_env.setenv("RESOLVER_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    app_module.get_settings.cache_clear()
    with immediate_proxy(http_client):
        response = http_client.get(
            "/resolve",
            params={"ref": "ipfs://bafyCID/a.png"},
            headers={"Forwarded": "for=198.51.100.7;proto=https;host=curio.example:443"},
        )
    assert response.json()["media_url"] == "https://curio.example/ipfs/bafyCID/a.png"


def test_trusted_proxy_accepts_x_forwarded_origin(http_client, origin_env):
    origin_env.setenv("RESOLVER_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    app_module.get_settings.cache_clear()
    with immediate_proxy(http_client):
        response = http_client.get(
            "/resolve",
            params={"ref": "ipfs://bafyCID/a.png"},
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "curio.example:8443"},
        )
    assert response.json()["media_url"] == "https://curio.example:8443/ipfs/bafyCID/a.png"


@pytest.mark.parametrize(
    "headers",
    [
        {"Forwarded": "proto=https"},
        {"Forwarded": "proto=ftp;host=curio.example"},
        {"Forwarded": "proto=https;host=user@curio.example"},
        {"Forwarded": "proto=https;host=curio.example/path"},
        {"Forwarded": "proto=https;host=curio.example:70000"},
        {"Forwarded": "proto=https;host=curio.example:"},
        {"X-Forwarded-Proto": "https"},
        {"X-Forwarded-Proto": "ftp", "X-Forwarded-Host": "curio.example"},
        {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "curio.example/path"},
    ],
)
def test_malformed_trusted_forwarded_origin_is_ignored(http_client, origin_env, headers):
    origin_env.setenv("RESOLVER_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    app_module.get_settings.cache_clear()
    headers = {"Host": "direct.example", **headers}
    with immediate_proxy(http_client):
        response = http_client.get("/resolve", params={"ref": "ipfs://bafyCID/a.png"}, headers=headers)
    assert response.json()["media_url"] == "http://direct.example/ipfs/bafyCID/a.png"


def test_invalid_direct_host_is_controlled_and_mcp_uses_same_boundary(http_client, origin_env):
    response = http_client.get(
        "http://bad_host/resolve", params={"ref": "ipfs://bafyCID/a.png"},
    )
    assert response.status_code == 421
    # The mounted MCP transport has the same pre-route Host guard.
    response = http_client.post("http://bad_host/mcp")
    assert response.status_code == 421


def test_public_base_precedes_trusted_forwarded_origin(http_client, origin_env):
    origin_env.setenv("RESOLVER_PUBLIC_BASE_URL", "https://configured.example")
    origin_env.setenv("RESOLVER_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    app_module.get_settings.cache_clear()
    with immediate_proxy(http_client):
        response = http_client.get(
            "/resolve",
            params={"ref": "ipfs://bafyCID/a.png"},
            headers={"Forwarded": "proto=https;host=forwarded.example"},
        )
    assert response.json()["media_url"] == "https://configured.example/ipfs/bafyCID/a.png"
