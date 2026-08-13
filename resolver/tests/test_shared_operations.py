"""REST and MCP delegate shared workflows to operations."""

import json

from resolver import operations
from resolver.config import get_settings
from resolver.favorites import get_favorites
from resolver.mcp_server import mcp
from resolver.overrides import get_registry
from resolver.resolve import Resolved


async def test_rest_and_mcp_share_resolution_override_and_favorite_workflows(
    http_client, tmp_path, monkeypatch
):
    monkeypatch.setenv("RESOLVER_OVERRIDES_PATH", str(tmp_path / "overrides.toml"))
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    get_registry.cache_clear()
    get_favorites.cache_clear()

    async def unresolved(ref, *_args, **_kwargs):
        return Resolved(ref, ref, "play", "ipfs", False, source_kind="ipfs", final_ref=ref)

    original_store = operations.store_reference
    original_override = operations.create_override
    original_favorite = operations.create_favorite
    calls = []

    async def tracked_store(*args, **kwargs):
        calls.append("store")
        return await original_store(*args, **kwargs)

    async def tracked_override(*args, **kwargs):
        calls.append("override")
        return await original_override(*args, **kwargs)

    async def tracked_favorite(*args, **kwargs):
        calls.append("favorite")
        return await original_favorite(*args, **kwargs)

    monkeypatch.setattr(operations, "resolve_ref", unresolved)
    monkeypatch.setattr(operations, "store_reference", tracked_store)
    monkeypatch.setattr(operations, "create_override", tracked_override)
    monkeypatch.setattr(operations, "create_favorite", tracked_favorite)

    rest = http_client.post("/resolve", params={"ref": "ipfs://bafyREST"})
    assert rest.status_code == 422 and rest.json()["status"] == "failed"
    content, _ = await mcp.call_tool("resolve", {"ref": "ipfs://bafyMCP"})
    assert json.loads(content[0].text)["status"] == "failed"

    entry = {"replacement": "ipfs://bafyALT", "status": "alternate-master"}
    assert http_client.post(
        "/override", json={"ref": "ipfs://bafyRESTDEAD", **entry}
    ).status_code == 201
    content, _ = await mcp.call_tool(
        "add_override", {"ref": "ipfs://bafyMCPDEAD", **entry}
    )
    assert json.loads(content[0].text)["replacement_resolved"] is False

    rest_favorite = http_client.post(
        "/favorites", params={"ref": "ipfs://bafyRESTFAV"}
    )
    assert rest_favorite.status_code == 201
    content, _ = await mcp.call_tool(
        "add_favorite", {"ref": "ipfs://bafyMCPFAV"}
    )
    mcp_favorite = json.loads(content[0].text)

    assert calls == ["store", "store", "override", "override", "favorite", "favorite"]
    assert rest_favorite.json()["resolved_url"] is None
    assert rest_favorite.json()["playback_method"] == "play"
    # REST and MCP share operations.FavoriteCreation.response(): same shape.
    assert mcp_favorite["resolved_url"] is None
    assert mcp_favorite["playback_method"] == "play"

    get_settings.cache_clear()
    get_registry.cache_clear()
    get_favorites.cache_clear()
