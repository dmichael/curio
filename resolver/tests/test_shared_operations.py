"""The transport adapters delegate their duplicated mutations to operations."""

import json

from resolver import app as app_module
from resolver import mcp_server, operations
from resolver.config import get_settings
from resolver.favorites import get_favorites
from resolver.mcp_server import mcp
from resolver.overrides import get_registry
from resolver.resolve import Resolved


async def test_rest_and_mcp_share_mutations_but_keep_favorite_payloads_distinct(
    http_client, tmp_path, monkeypatch
):
    monkeypatch.setenv("RESOLVER_OVERRIDES_PATH", str(tmp_path / "overrides.toml"))
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    get_registry.cache_clear()
    get_favorites.cache_clear()

    async def unresolved(ref, *_args, **_kwargs):
        return Resolved(ref, ref, "play", "ipfs", False, source_kind="ipfs", final_ref=ref)

    # Resolve is owned by each transport adapter, while all work after it is
    # shared. Count the shared calls while retaining their real behavior.
    original_pin = operations.resolved_with_optional_pin
    original_override = operations.create_override
    original_favorite = operations.create_favorite
    calls = []

    async def tracked_pin(*args, **kwargs):
        calls.append("pin")
        return await original_pin(*args, **kwargs)

    async def tracked_override(*args, **kwargs):
        calls.append("override")
        return await original_override(*args, **kwargs)

    async def tracked_favorite(*args, **kwargs):
        calls.append("favorite")
        return await original_favorite(*args, **kwargs)

    monkeypatch.setattr(app_module, "resolve_ref", unresolved)
    monkeypatch.setattr(mcp_server, "resolve_ref", unresolved)
    monkeypatch.setattr(operations, "resolve_ref", unresolved)
    monkeypatch.setattr(operations, "resolved_with_optional_pin", tracked_pin)
    monkeypatch.setattr(operations, "create_override", tracked_override)
    monkeypatch.setattr(operations, "create_favorite", tracked_favorite)

    assert http_client.get("/resolve", params={"ref": "ipfs://bafyREST", "pin": 1}).status_code == 200
    content, _ = await mcp.call_tool(
        "resolve", {"ref": "ipfs://bafyMCP", "pin": True, "curator_token": "test-curator-token"}
    )
    assert json.loads(content[0].text)["pin_scheduled"] is False

    entry = {"replacement": "ipfs://bafyALT", "status": "alternate-master"}
    assert http_client.post("/override", json={"ref": "ipfs://bafyRESTDEAD", **entry}).status_code == 201
    content, _ = await mcp.call_tool(
        "add_override",
        {"ref": "ipfs://bafyMCPDEAD", **entry, "curator_token": "test-curator-token"},
    )
    assert json.loads(content[0].text)["replacement_resolved"] is False

    rest_favorite = http_client.post("/favorites", params={"ref": "ipfs://bafyRESTFAV"})
    assert rest_favorite.status_code == 201
    content, _ = await mcp.call_tool(
        "add_favorite", {"ref": "ipfs://bafyMCPFAV", "curator_token": "test-curator-token"}
    )
    mcp_favorite = json.loads(content[0].text)
    assert calls == ["pin", "pin", "override", "override", "favorite", "favorite"]

    # REST keeps renderer-ready fields; the MCP tool's longstanding compact
    # creation payload deliberately does not add them.
    assert rest_favorite.json()["resolved_url"] is None
    assert rest_favorite.json()["playback_method"] == "play"
    assert "resolved_url" not in mcp_favorite
    assert "playback_method" not in mcp_favorite

    get_settings.cache_clear()
    get_registry.cache_clear()
    get_favorites.cache_clear()
