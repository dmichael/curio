import json

import httpx
import pytest

from resolver import mcp_server
from resolver.config import get_settings
from resolver.favorites import get_favorites
from resolver.mcp_server import mcp
from resolver.overrides import get_registry
from resolver.static_store import ResolutionStatus, StaticStore


def no_net() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD" and request.url.host == "127.0.0.1":
            return httpx.Response(200, headers={"content-type": "image/png"})
        if request.url.path == "/api/v0/pin/add":
            return httpx.Response(200, json={"Pins": ["bafyCID"]})
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
        "lookup",
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


def test_mcp_transport_uses_nonlocal_request_origin(http_client, monkeypatch):
    """Exercise the real streamable-HTTP transport, not call_tool()."""
    async def stored(ref, _settings, _client, origin):
        return {"ref": ref, "media_url": f"{origin}/resolve?ref={ref}", "status": "ready"}, True

    monkeypatch.setattr(mcp_server.operations, "store_reference", stored)
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
    assert "http://curio.example/resolve?ref=ipfs://bafyCID/a.png" in response.text


def test_mcp_transport_uses_trusted_forwarded_origin_and_guard(http_client, monkeypatch):
    """The mounted MCP transport and Host/Origin guard share proxy origin logic."""
    async def stored(ref, _settings, _client, origin):
        return {"ref": ref, "media_url": f"{origin}/resolve?ref={ref}", "status": "ready"}, True

    monkeypatch.setattr(mcp_server.operations, "store_reference", stored)
    monkeypatch.setenv("RESOLVER_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    get_settings.cache_clear()
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "Host": "resolver.internal:8090",
        "Origin": "https://curio.example",
        "Forwarded": "for=198.51.100.7;proto=https;host=curio.example",
    }
    initialize = {"jsonrpc": "2.0", "id": 31, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
    }}
    old_client = http_client._transport.client
    http_client._transport.client = ("127.0.0.1", 50000)
    try:
        assert http_client.post("/mcp", headers=headers, json=initialize).status_code == 200
        assert http_client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }).status_code == 202
        response = http_client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 32, "method": "tools/call",
            "params": {"name": "resolve", "arguments": {"ref": "ipfs://bafyCID/a.png"}},
        })
    finally:
        http_client._transport.client = old_client
        get_settings.cache_clear()
    assert response.status_code == 200
    assert "https://curio.example/resolve?ref=ipfs://bafyCID/a.png" in response.text


async def test_mcp_resolve_tool_round_trips():
    async with no_net() as client:
        mcp_server.set_client(client)
        payload = await call("resolve", {"ref": "ipfs://bafyCID/art.png"})
    assert payload["status"] == "ready"
    assert payload["final_ref"] == "ipfs://bafyCID/art.png"
    assert "/resolve?ref=" in payload["media_url"]


async def test_mcp_lookup_tool_found_and_playable(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    StaticStore(str(tmp_path / "media")).record_resolution(
        canonical_ref="ipfs://bafyOK/art.png",
        ref="ipfs://bafyOK/art.png",
        final_ref="ipfs://bafyOK/art.png",
        media_path="/ipfs/bafyOK/art.png",
        status=ResolutionStatus.READY,
        media_type="image/png",
    )
    try:
        found = await call("lookup", {"ref": "/ipfs/bafyOK/art.png"})  # any spelling
    finally:
        get_settings.cache_clear()
    assert found == {
        "found": True,
        "playable": True,
        "reason": None,
        "media_path": "/ipfs/bafyOK/art.png",
        "status": "ready",
        "media_type": "image/png",
    }


async def test_mcp_lookup_tool_found_and_not_playable(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    StaticStore(str(tmp_path / "media")).record_resolution(
        canonical_ref="ipfs://bafyDEAD/art.png",
        ref="ipfs://bafyDEAD/art.png",
        final_ref="ipfs://bafyDEAD/art.png",
        media_path="/ipfs/bafyDEAD/art.png",
        status=ResolutionStatus.FAILED,
        reason="providers gone",
    )
    try:
        found = await call("lookup", {"ref": "ipfs://bafyDEAD/art.png"})
    finally:
        get_settings.cache_clear()
    assert found["playable"] is False
    assert found["reason"] == "providers gone"


async def test_mcp_lookup_tool_unknown_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    try:
        found = await call("lookup", {"ref": "ipfs://bafyNEVERSEEN/art.png"})
    finally:
        get_settings.cache_clear()
    assert found == {"found": False}


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
            },
        )
        assert added["replaced"] is False
        assert added["replacement_resolved"] is True

        listed = await call("list_overrides", {})
        assert listed["count"] == 1
        assert listed["entries"][0]["note"] == "test master"

        removed = await call("remove_override", {"ref": "/ipfs/bafyDEAD/art.png"})
        assert removed["removed"]["ref"] == "ipfs://bafyDEAD/art.png"
        assert (await call("list_overrides", {}))["count"] == 0


async def test_mcp_list_overrides_raw_matches_rest(override_env):
    async with no_net() as client:
        mcp_server.set_client(client)
        await call(
            "add_override",
            {
                "ref": "ipfs://bafyDEAD/art.png",
                "replacement": "ipfs://bafyALT/master.png",
                "status": "alternate-master",
            },
        )
        raw = await call("list_overrides", {"raw": True})
    assert "ipfs://bafyDEAD/art.png" in raw["raw"]
    assert "ipfs://bafyALT/master.png" in raw["raw"]


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
            "add_favorite", {"ref": "ipfs://bafyFAV/art.png", "note": "living room"}
        )
        assert added["key"] == "ipfs://bafyFAV/art.png"
        assert added["resolved"] is True

        listed = await call("list_favorites", {})
        assert listed["count"] == 1
        assert listed["favorites"][0]["note"] == "living room"
        # entries arrive resolved — resolved_url is renderer-ready
        assert listed["favorites"][0]["resolved"] is True
        assert listed["favorites"][0]["resolved_url"].endswith("/ipfs/bafyFAV/art.png")

        removed = await call("remove_favorite", {"ref": "/ipfs/bafyFAV/art.png"})
        assert removed["removed"]["ref"] == "ipfs://bafyFAV/art.png"
        assert (await call("list_favorites", {}))["count"] == 0


async def test_mcp_add_favorite_response_matches_rest_shape(favorites_env):
    """Regression test: add_favorite once omitted resolved_url and
    playback_method that REST's POST /favorites always included."""
    async with no_net() as client:
        mcp_server.set_client(client)
        added = await call("add_favorite", {"ref": "ipfs://bafyFAV2/art.png"})
    assert added["resolved"] is True
    assert added["resolved_url"].endswith("/ipfs/bafyFAV2/art.png")
    assert added["playback_method"] == "play"


async def test_mcp_library_status_smoke(tmp_path, monkeypatch):
    # Arweave inventory now comes from the resolutions catalogue at
    # static_root — isolate it so another test's registered txid can't leak in.
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/pin/ls":
            return httpx.Response(200, json={"Keys": {"bafyA": {"Type": "recursive"}}})
        if request.url.path == "/api/v0/repo/stat":
            return httpx.Response(200, json={"RepoSize": 4096, "NumObjects": 12})
        if request.method == "HEAD" and request.url.host == "127.0.0.1":
            return httpx.Response(200, headers={"x-cache": "HIT"})
        raise AssertionError(f"unexpected network call: {request.url}")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mcp_server.set_client(client)
            payload = await call("library_status", {})
    finally:
        get_settings.cache_clear()
    assert payload["ipfs"] == {"pinned": 1, "repo_size_bytes": 4096, "repo_objects": 12}
    assert payload["arweave"] == {"known_warmed": 0, "currently_cached": 0}
    assert payload["registry"] == {"overrides": None, "favorites": None}


async def test_mcp_override_tools_error_when_disabled(monkeypatch):
    monkeypatch.delenv("RESOLVER_OVERRIDES_PATH", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="RESOLVER_OVERRIDES_PATH"):
            await mcp.call_tool("list_overrides", {})
    finally:
        get_settings.cache_clear()


async def test_mcp_instructions_describe_the_service():
    instructions = mcp.instructions or ""
    assert "no user authentication" in instructions
    assert "public internet" in instructions
    assert "POST /resolve" in instructions
