import json

import httpx
import pytest

from resolver import mcp_server
from resolver.config import get_settings
from resolver.favorites import get_favorites
from resolver.mcp_server import mcp
from resolver.overrides import get_registry


def no_net() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def call(tool: str, args: dict) -> dict:
    content, _ = await mcp.call_tool(tool, args)
    return json.loads(content[0].text)


@pytest.fixture
def override_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_OVERRIDES_PATH", str(tmp_path / "overrides.toml"))
    monkeypatch.setenv("RESOLVER_IPFS_PUBLIC_BASE", "http://box:8080")
    get_settings.cache_clear()
    get_registry.cache_clear()
    yield
    get_settings.cache_clear()
    get_registry.cache_clear()


async def test_mcp_exposes_the_curated_tools():
    tools = {tool.name for tool in await mcp.list_tools()}
    assert tools == {
        "resolve",
        "wallet_tokens",
        "seed_wallet",
        "seed_status",
        "health",
        "library_status",
        "list_overrides",
        "add_override",
        "remove_override",
        "list_favorites",
        "add_favorite",
        "remove_favorite",
    }


def test_mcp_transport_uses_nonlocal_request_origin(http_client):
    """Exercise the real streamable-HTTP transport, not call_tool()."""
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
    }}
    previous_base = http_client.base_url
    http_client.base_url = "http://curio.example"
    try:
        assert http_client.post("/mcp", headers=headers, json=initialize).status_code == 200
        assert http_client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }).status_code == 202
        response = http_client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "resolve", "arguments": {"ref": "ipfs://bafyCID/a.png"}},
        })
    finally:
        http_client.base_url = previous_base
    assert response.status_code == 200
    assert "http://curio.example/ipfs/bafyCID/a.png" in response.text


async def test_mcp_resolve_tool_round_trips():
    async with no_net() as client:
        mcp_server.set_client(client)
        payload = await call("resolve", {"ref": "ipfs://bafyCID/art.png"})
    assert payload["resolved"] is True
    assert payload["resolved_url"].endswith("/ipfs/bafyCID/art.png")


async def test_mcp_override_tools_round_trip(override_env):
    async with no_net() as client:
        mcp_server.set_client(client)
        added = await call(
            "add_override",
            {
                "ref": "ipfs://bafyDEAD/art.png",
                "replacement": "ipfs://bafyALT/master.png",
                "status": "alternate-master",
                "note": "test master",
                "curator_token": "test-curator-token",
            },
        )
        assert added["replaced"] is False
        assert added["replacement_resolved"] is True

        listed = await call("list_overrides", {})
        assert listed["count"] == 1
        assert listed["entries"][0]["note"] == "test master"

        removed = await call("remove_override", {"ref": "/ipfs/bafyDEAD/art.png", "curator_token": "test-curator-token"})
        assert removed["removed"]["ref"] == "ipfs://bafyDEAD/art.png"
        assert (await call("list_overrides", {}))["count"] == 0


@pytest.fixture
def favorites_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    get_favorites.cache_clear()
    yield
    get_settings.cache_clear()
    get_favorites.cache_clear()


async def test_mcp_favorite_tools_round_trip(favorites_env):
    async with no_net() as client:
        mcp_server.set_client(client)
        added = await call(
            "add_favorite", {"ref": "ipfs://bafyFAV/art.png", "note": "living room", "curator_token": "test-curator-token"}
        )
        assert added["key"] == "ipfs://bafyFAV/art.png"
        assert added["resolved"] is True

        listed = await call("list_favorites", {})
        assert listed["count"] == 1
        assert listed["favorites"][0]["note"] == "living room"
        # entries arrive resolved — resolved_url is renderer-ready
        assert listed["favorites"][0]["resolved"] is True
        assert listed["favorites"][0]["resolved_url"].endswith("/ipfs/bafyFAV/art.png")

        removed = await call("remove_favorite", {"ref": "/ipfs/bafyFAV/art.png", "curator_token": "test-curator-token"})
        assert removed["removed"]["ref"] == "ipfs://bafyFAV/art.png"
        assert (await call("list_favorites", {}))["count"] == 0


async def test_mcp_library_status_smoke():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/pin/ls":
            return httpx.Response(200, json={"Keys": {"bafyA": {"Type": "recursive"}}})
        if request.url.path == "/api/v0/repo/stat":
            return httpx.Response(200, json={"RepoSize": 4096, "NumObjects": 12})
        raise AssertionError(f"unexpected network call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mcp_server.set_client(client)
        payload = await call("library_status", {})
    assert payload["ipfs"] == {"pinned": 1, "repo_size_bytes": 4096, "repo_objects": 12}
    assert set(payload["arweave"]["retained"]) == {"kept", "pending", "failed", "operation"}
    assert "not an AR.IO r81 pin API" in payload["arweave"]["retained"]["operation"]
    assert payload["registry"] == {"overrides": None, "favorites": None, "captures": None}


async def test_mcp_override_tools_error_when_disabled(monkeypatch):
    monkeypatch.delenv("RESOLVER_OVERRIDES_PATH", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="RESOLVER_OVERRIDES_PATH"):
            await mcp.call_tool("list_overrides", {})
    finally:
        get_settings.cache_clear()


async def test_mcp_instructions_come_from_the_skill():
    assert "# Curio" in (mcp.instructions or "")
    assert "/resolve" in (mcp.instructions or "")
