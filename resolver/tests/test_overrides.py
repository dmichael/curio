"""The override registry: dead canonical refs resolve to operator-recorded
replacements, disclosed, never silently."""

import os
import tomllib

import httpx
import pytest

from resolver.config import Settings
from resolver.overrides import (
    DuplicateOverride,
    Override,
    OverrideNotFound,
    OverrideRegistry,
    RegistryUnparseable,
    validate_entry,
)
from resolver.resolve import resolve_ref

REGISTRY = """
[[override]]
ref = "https://hodlers.example/media/149"
replacement = "ipfs://bafyREPL/horizon-149.mp4"
status = "operator-attested"
token = "eip155:1/erc721:0xabc/149"
source = "local copy; hodlers.example lapsed"
note = "Horizon #149"

[[override]]
ref = "ipfs://bafyDEAD/art.png"
replacement = "ipfs://bafyALT/master.png"
status = "alternate-master"

[[override]]
ref = "https://bad.example/x"
replacement = "https://bad.example/y"
status = "not-a-tier"

[[override]]
ref = "ipfs://bafySELF/a.png"
replacement = "https://gw.example/ipfs/bafySELF/a.png"
status = "operator-attested"
"""


def settings_with(overrides_path) -> Settings:
    return Settings(
        ipfs_internal="http://ipfs.internal",
        arweave_internal="http://ar.internal",
        ipfs_api="http://kubo.internal",
        ipfs_public_base="http://box:8080",
        arweave_public_base="http://box:3000",
        overrides_path=str(overrides_path),
    )


def registry_file(tmp_path, text=REGISTRY):
    path = tmp_path / "overrides.toml"
    path.write_text(text)
    return path


def fake_net(routes: dict[str, dict] | None = None) -> httpx.AsyncClient:
    table = routes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        spec = table.get(f"{request.method} {request.url}") or table.get(str(request.url))
        if spec is None:
            return httpx.Response(404)
        return httpx.Response(**spec)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def no_net() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_override_serves_replacement_with_disclosure(tmp_path):
    settings = settings_with(registry_file(tmp_path))
    ref = "https://hodlers.example/media/149"
    async with no_net() as client:
        result = await resolve_ref(ref, settings, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyREPL/horizon-149.mp4"
    assert result.resolved is True
    assert result.substituted is True
    assert result.substituted_ref == ref
    assert result.substitution_status == "operator-attested"
    assert result.original_ref == ref


async def test_override_matches_any_spelling_of_the_ref(tmp_path):
    # Registry says ipfs://bafyDEAD/art.png; the request arrives as a
    # public-gateway URL for the same content.
    settings = settings_with(registry_file(tmp_path))
    async with no_net() as client:
        result = await resolve_ref("https://ipfs.io/ipfs/bafyDEAD/art.png", settings, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyALT/master.png"
    assert result.substituted is True
    assert result.substitution_status == "alternate-master"


async def test_dead_ref_inside_live_metadata_is_substituted(tmp_path):
    # The usual shape: metadata resolves fine, its animation_url is dead.
    settings = settings_with(registry_file(tmp_path))
    routes = {
        "http://ipfs.internal/ipfs/bafyMETA": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"name": "Horizon #149", "animation_url": "https://hodlers.example/media/149"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyMETA", settings, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyREPL/horizon-149.mp4"
    assert result.original_ref == "ipfs://bafyMETA"
    assert result.title == "Horizon #149"
    assert result.substituted is True
    assert result.substituted_ref == "https://hodlers.example/media/149"
    assert result.substitution_status == "operator-attested"


async def test_invalid_status_entry_is_ignored(tmp_path):
    settings = settings_with(registry_file(tmp_path))
    async with fake_net() as client:  # probe 404s; direct passthrough
        result = await resolve_ref("https://bad.example/x", settings, client)
    assert result.substituted is False
    assert result.resolved_url == "https://bad.example/x"


async def test_self_referential_entry_is_ignored(tmp_path):
    # replacement canonicalizes to the ref itself — would loop; skipped.
    settings = settings_with(registry_file(tmp_path))
    async with no_net() as client:
        result = await resolve_ref("ipfs://bafySELF/a.png", settings, client)
    assert result.substituted is False
    assert result.resolved_url == "http://box:8080/ipfs/bafySELF/a.png"


async def test_registry_reloads_on_mtime_change(tmp_path):
    path = registry_file(tmp_path)
    settings = settings_with(path)
    ref = "https://hodlers.example/media/149"
    async with no_net() as client:
        first = await resolve_ref(ref, settings, client)
        assert first.resolved_url == "http://box:8080/ipfs/bafyREPL/horizon-149.mp4"

        path.write_text(REGISTRY.replace("bafyREPL/horizon-149.mp4", "bafyNEW/found-master.mp4"))
        stat = os.stat(path)
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

        second = await resolve_ref(ref, settings, client)
    assert second.resolved_url == "http://box:8080/ipfs/bafyNEW/found-master.mp4"


async def test_broken_edit_keeps_the_previous_table(tmp_path):
    path = registry_file(tmp_path)
    settings = settings_with(path)
    ref = "https://hodlers.example/media/149"
    async with no_net() as client:
        await resolve_ref(ref, settings, client)  # load the good table

        path.write_text("[[override]\nnot toml")
        stat = os.stat(path)
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

        result = await resolve_ref(ref, settings, client)
    assert result.substituted is True  # still serving the last good registry


async def test_missing_registry_file_is_empty(tmp_path):
    settings = settings_with(tmp_path / "does-not-exist.toml")
    async with no_net() as client:
        result = await resolve_ref("ipfs://bafyCID/art.png", settings, client)
    assert result.substituted is False
    assert result.resolved_url == "http://box:8080/ipfs/bafyCID/art.png"


# --- write path: upsert/remove rewrite the file, atomically and strictly ---

VALID_REGISTRY = """\
# a hand-written comment that machine writes are allowed to drop
[[override]]
ref = "ipfs://bafyDEAD/art.png"
replacement = "ipfs://bafyALT/master.png"
status = "alternate-master"
"""


def entry(ref="https://gone.example/work", replacement="ipfs://bafyNEW/m.mp4", **kw):
    return Override(ref=ref, replacement=replacement, status=kw.pop("status", "alternate-master"), **kw)


def test_upsert_round_trips_and_is_visible_immediately(tmp_path):
    path = tmp_path / "overrides.toml"
    registry = OverrideRegistry(str(path))
    registry.upsert(entry(note='has "quotes", a \\ backslash,\nnewline, and émoji 🎨'))

    # write-through: found with no mtime poke, in the same second
    found = registry.lookup("https://gone.example/work")
    assert found is not None and found.replacement == "ipfs://bafyNEW/m.mp4"

    # the file itself is valid TOML and survives a full parse round trip
    parsed = tomllib.loads(path.read_text())["override"]
    assert parsed[0]["ref"] == "https://gone.example/work"
    assert parsed[0]["note"] == 'has "quotes", a \\ backslash,\nnewline, and émoji 🎨'
    assert path.read_text().startswith("# Operator exception registry")


def test_upsert_duplicate_needs_replace_and_matches_any_spelling(tmp_path):
    path = tmp_path / "overrides.toml"
    path.write_text(VALID_REGISTRY)
    registry = OverrideRegistry(str(path))

    # same content, different spelling: a gateway URL for the recorded ipfs ref
    dup = entry(ref="https://ipfs.io/ipfs/bafyDEAD/art.png", replacement="ipfs://bafyOTHER/x.png")
    with pytest.raises(DuplicateOverride):
        registry.upsert(dup)

    registry.upsert(entry(), replace=False)  # distinct ref appends fine
    assert registry.upsert(dup, replace=True) is True
    entries = registry.entries()
    assert [e.replacement for e in entries] == ["ipfs://bafyOTHER/x.png", "ipfs://bafyNEW/m.mp4"]
    # replace edited in place: the replaced entry kept its position


def test_remove_by_alternate_spelling(tmp_path):
    path = tmp_path / "overrides.toml"
    path.write_text(VALID_REGISTRY)
    registry = OverrideRegistry(str(path))

    removed = registry.remove("/ipfs/bafyDEAD/art.png")
    assert removed.replacement == "ipfs://bafyALT/master.png"
    assert registry.entries() == []
    with pytest.raises(OverrideNotFound):
        registry.remove("ipfs://bafyDEAD/art.png")


def test_mutation_refuses_to_rewrite_a_broken_file(tmp_path):
    path = tmp_path / "overrides.toml"
    broken = "[[override]\nnot toml"
    path.write_text(broken)
    registry = OverrideRegistry(str(path))
    with pytest.raises(RegistryUnparseable):
        registry.upsert(entry())
    assert path.read_text() == broken  # untouched, byte for byte

    # invalid-but-parseable entries also block writes: a lenient read skips
    # them, but a rewrite would silently drop the operator's fixable typo
    path.write_text(VALID_REGISTRY.replace("alternate-master", "not-a-tier"))
    with pytest.raises(RegistryUnparseable):
        registry.remove("ipfs://bafyDEAD/art.png")


def test_upsert_creates_the_file_and_coexists_with_hand_entries(tmp_path):
    path = tmp_path / "missing" / "overrides.toml"
    registry = OverrideRegistry(str(path))
    with pytest.raises(OverrideNotFound):
        registry.raw_text()
    registry.upsert(entry())
    assert path.exists()

    # a later hand-added entry survives the next machine write...
    path.write_text(registry.raw_text() + VALID_REGISTRY)
    registry.upsert(entry(ref="ar://TX123", replacement="ipfs://bafyAR/m.png"))
    refs = {e.ref for e in registry.entries()}
    assert refs == {"https://gone.example/work", "ipfs://bafyDEAD/art.png", "ar://TX123"}
    # ...but its comment does not (accepted: the file is machine-managed)
    assert "hand-written comment" not in registry.raw_text()


@pytest.mark.parametrize(
    "raw,reason",
    [
        ({"replacement": "ipfs://x", "status": "alternate-master"}, "missing ref"),
        ({"ref": "ipfs://x", "status": "alternate-master"}, "missing replacement"),
        ({"ref": "ipfs://x", "replacement": "ipfs://y", "status": "nope"}, "not one of"),
        (
            {"ref": "ipfs://bafyX/a", "replacement": "https://gw.example/ipfs/bafyX/a",
             "status": "alternate-master"},
            "the ref itself",
        ),
        ("not a table", "not a table"),
    ],
)
def test_validate_entry_rejections(raw, reason):
    with pytest.raises(ValueError, match=reason):
        validate_entry(raw)
