"""store_upload: uploaded bytes -> Kubo (pinned, CIDv1) with provenance recorded."""

import hashlib
import io
import json

import httpx
import pytest
from starlette.datastructures import Headers, UploadFile

from resolver.config import Settings
from resolver.store import store_upload


def settings_with(tmp_path, **kw) -> Settings:
    return Settings(
        ipfs_api="http://kubo.internal",
        ipfs_public_base="http://box:8080",
        seed_capture_dir=str(tmp_path / "captures"),
        **kw,
    )


def upload(data: bytes, filename="master.png", content_type="image/png") -> UploadFile:
    return UploadFile(
        io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type})
    )


async def test_store_upload_adds_with_cidv1_and_records_provenance(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"Hash": "bafySTORED", "Name": "master.png"})

    settings = settings_with(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await store_upload(upload(b"the-master-bytes"), settings, client)

    assert seen["path"] == "/api/v0/add"
    assert seen["params"] == {"cid-version": "1"}
    assert result["cid"] == "bafySTORED"
    assert result["sha256"] == hashlib.sha256(b"the-master-bytes").hexdigest()
    assert result["bytes"] == len(b"the-master-bytes")
    assert result["resolved_url"] == "http://box:8080/ipfs/bafySTORED"

    record = json.loads((tmp_path / "captures" / "captures.jsonl").read_text())
    assert record["source"] == "upload:master.png"
    assert record["cid"] == "bafySTORED"
    assert record["content_type"] == "image/png"
    assert record["wallet"] is None


async def test_store_upload_refuses_oversize_before_kubo(tmp_path):
    settings = settings_with(tmp_path, seed_recover_max_bytes=8)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("oversize upload must not reach Kubo")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="exceeds"):
            await store_upload(upload(b"way more than eight bytes"), settings, client)
    assert not (tmp_path / "captures" / "captures.jsonl").exists()


async def test_store_upload_kubo_failure_records_nothing(tmp_path):
    settings = settings_with(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await store_upload(upload(b"bytes"), settings, client)
    assert not (tmp_path / "captures" / "captures.jsonl").exists()


async def test_store_expect_cid_pins_only_on_round_trip(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v0/add":
            return httpx.Response(200, json={"Hash": "QmCANONICAL"})
        return httpx.Response(200, json={"Pins": ["QmCANONICAL"]})

    settings = settings_with(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await store_upload(
            upload(b"canonical bytes"), settings, client, expect_cid="QmCANONICAL"
        )

    # a Qm expectation adds unpinned at CIDv0 (no cid-version param), then pins
    assert calls[0] == ("/api/v0/add", {"pin": "false"})
    assert calls[1] == ("/api/v0/pin/add", {"arg": "/ipfs/QmCANONICAL"})
    assert result["cid"] == "QmCANONICAL"
    assert (tmp_path / "captures" / "captures.jsonl").exists()


async def test_store_expect_cid_mismatch_refuses_and_records_nothing(tmp_path):
    from resolver.store import CidMismatch

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"Hash": "QmSOMETHINGELSE"})

    settings = settings_with(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CidMismatch, match="not the canonical content"):
            await store_upload(
                upload(b"wrong bytes"), settings, client, expect_cid="QmCANONICAL"
            )

    assert calls == ["/api/v0/add"]  # never pinned
    assert not (tmp_path / "captures" / "captures.jsonl").exists()
