import httpx
import pytest

from resolver.config import Settings
from resolver.refs import canonical_ref_key, ipfs_parts
from resolver.resolve import resolve_ref

SETTINGS = Settings(
    ipfs_internal="http://ipfs.internal",
    arweave_internal="http://ar.internal",
    ipfs_api="http://kubo.internal",
    ipfs_public_base="http://box:8080",
    arweave_public_base="http://box:3000",
    ssrf_dns_check=False,
)

TXID = "abcdefghijklmnopqrstuvwxyz0123456789_ABCDEF"  # 43 chars


def fake_net(routes: dict[str, dict] | None = None) -> httpx.AsyncClient:
    """Client whose transport serves a url -> Response-kwargs table; else 404.

    Keys are plain URLs, or "METHOD url" when HEAD and GET must differ.
    """
    table = routes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        spec = table.get(f"{request.method} {request.url}") or table.get(str(request.url))
        if spec is None:
            return httpx.Response(404)
        return httpx.Response(**spec)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def no_net() -> httpx.AsyncClient:
    """Client that fails the test on any network call."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- mechanical cases (no probing needed) ---


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("ipfs://bafyCID/art.png", "http://box:8080/ipfs/bafyCID/art.png"),
        ("/ipfs/bafyCID/art.png", "http://box:8080/ipfs/bafyCID/art.png"),
        ("https://ipfs.io/ipfs/bafyCID/art.png", "http://box:8080/ipfs/bafyCID/art.png"),
    ],
)
async def test_ipfs_refs_with_extension_resolve_without_probing(ref, expected):
    async with no_net() as client:
        result = await resolve_ref(ref, SETTINGS, client)
    assert result.resolved_url == expected
    assert result.resolved is True
    assert result.provider == "ipfs"


async def test_existing_query_is_preserved_and_skips_the_probe():
    async with no_net() as client:
        result = await resolve_ref(
            "https://ipfs.io/ipfs/bafyCID?filename=piece.mp4", SETTINGS, client
        )
    assert result.resolved_url == "http://box:8080/ipfs/bafyCID?filename=piece.mp4"


# --- reference parsing (refs.py) ---

CIDV1 = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
CIDV0 = "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"


@pytest.mark.parametrize(
    "ref,expected",
    [
        (f"https://{CIDV1}.ipfs.dweb.link/art.png", (CIDV1, "/art.png")),
        (f"https://{CIDV1}.ipfs.w3s.link", (CIDV1, "")),
        (f"https://{CIDV0}.ipfs.cf-ipfs.com/x", (CIDV0, "/x")),
    ],
)
def test_subdomain_gateway_urls_parse_to_cid_and_path(ref, expected):
    assert ipfs_parts(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "https://www.ipfs.tech/",  # first label not CID-shaped
        "https://foo.ipfs.tech/",  # ditto, and no dot after the gateway label
        f"https://{CIDV1}.notipfs.com/",  # second label must be exactly "ipfs"
    ],
)
def test_ordinary_hosts_are_not_mistaken_for_subdomain_gateways(ref):
    assert ipfs_parts(ref) is None


def test_canonical_ref_key_collapses_subdomain_gateway_urls():
    assert canonical_ref_key(f"https://{CIDV1}.ipfs.dweb.link/x") == f"ipfs://{CIDV1}/x"


async def test_html_work_is_sent_not_played():
    async with fake_net({"https://example.com/runtime/index.html": {
        "status_code": 200, "headers": {"content-type": "text/html"}, "content": b"<html>"
    }}) as client:
        result = await resolve_ref("https://example.com/runtime/index.html", SETTINGS, client)
    assert result.playback_method == "send"
    assert "/media/" in result.resolved_url


async def test_unrecognized_ref_is_flagged():
    async with no_net() as client:
        result = await resolve_ref("not a reference", SETTINGS, client)
    assert result.resolved is False


# --- bare-CID filename hint (feedback_ff1_url_extension_hint) ---


async def test_bare_cid_gets_filename_hint_from_probed_content_type():
    routes = {
        "http://ipfs.internal/ipfs/bafyCID": {
            "status_code": 200,
            "headers": {"content-type": "image/jpeg"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("https://ipfs.io/ipfs/bafyCID", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyCID?filename=art.jpg"
    assert result.playback_method == "play"
    assert result.content_type == "image/jpeg"


async def test_bare_cid_html_runtime_work_is_sent_without_hint():
    routes = {
        "http://ipfs.internal/ipfs/bafyCID": {
            "status_code": 200,
            "headers": {"content-type": "text/html"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyCID", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyCID"
    assert result.playback_method == "send"


async def test_bare_cid_probe_failure_degrades_to_plain_url():
    async with fake_net() as client:  # every probe 404s
        result = await resolve_ref("ipfs://bafyCID", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyCID"
    assert result.resolved is True
    assert result.note is not None


async def test_directory_cid_descends_to_its_single_file():
    # Kubo signature for a directory: HEAD 200 with no Content-Type; the API
    # `ls` lists children. The child file then gets the usual hint.
    routes = {
        "HEAD http://ipfs.internal/ipfs/bafyDIR": {"status_code": 200},
        "POST http://kubo.internal/api/v0/ls?arg=%2Fipfs%2FbafyDIR": {
            "status_code": 200,
            "json": {"Objects": [{"Links": [
                {"Name": "ce7b8685", "Hash": "QmChild", "Size": 30610185, "Type": 2},
            ]}]},
        },
        "HEAD http://ipfs.internal/ipfs/bafyDIR/ce7b8685": {
            "status_code": 200,
            "headers": {"content-type": "image/jpeg"},
        },
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyDIR", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyDIR/ce7b8685?filename=art.jpg"
    assert result.original_ref == "ipfs://bafyDIR"
    assert result.playback_method == "play"


async def test_directory_cid_with_several_files_descends_to_the_largest():
    routes = {
        "HEAD http://ipfs.internal/ipfs/bafyDIR": {"status_code": 200},
        "POST http://kubo.internal/api/v0/ls?arg=%2Fipfs%2FbafyDIR": {
            "status_code": 200,
            "json": {"Objects": [{"Links": [
                {"Name": "small.png", "Hash": "QmS", "Size": 10, "Type": 2},
                {"Name": "big.png", "Hash": "QmB", "Size": 9000, "Type": 2},
                {"Name": "sub", "Hash": "QmD", "Size": 99999, "Type": 1},  # dir: ignored
            ]}]},
        },
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyDIR", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyDIR/big.png"


# --- arweave ---


@pytest.mark.parametrize("ref", [f"ar://{TXID}", f"https://arweave.net/{TXID}"])
async def test_arweave_refs_target_the_box(ref):
    routes = {
        f"http://ar.internal/{TXID}": {
            "status_code": 200,
            "headers": {"content-type": "video/mp4"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref(ref, SETTINGS, client)
    assert result.resolved_url == f"http://box:3000/arweave/{TXID}"
    assert result.provider == "arweave"
    assert result.playback_method == "play"
    assert result.content_type == "video/mp4"


@pytest.mark.parametrize("ref", [f"ar://{TXID}/49", f"https://arweave.net/{TXID}/49"])
async def test_arweave_manifest_path_is_preserved(ref):
    # Path manifests resolve txid/sub to a distinct resource — dropping the
    # path serves the manifest root instead of the addressed content.
    routes = {
        f"http://ar.internal/{TXID}/49": {
            "status_code": 200,
            "headers": {"content-type": "video/mp4"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref(ref, SETTINGS, client)
    assert result.resolved_url == f"http://box:3000/arweave/{TXID}/49"
    assert result.provider == "arweave"


async def test_arweave_query_is_preserved():
    routes = {
        f"http://ar.internal/{TXID}": {
            "status_code": 200,
            "headers": {"content-type": "video/mp4"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref(f"https://arweave.net/{TXID}?foo=1", SETTINGS, client)
    assert result.resolved_url == f"http://box:3000/arweave/{TXID}?foo=1"
    assert result.provider == "arweave"


async def test_arweave_manifest_metadata_recurses():
    media_txid = "z" * 43
    routes = {
        f"http://ar.internal/{TXID}/49": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"name": "FV #49", "image": f"ar://{media_txid}"},
        },
        f"http://ar.internal/{media_txid}": {
            "status_code": 200,
            "headers": {"content-type": "image/png"},
        },
    }
    async with fake_net(routes) as client:
        result = await resolve_ref(f"ar://{TXID}/49", SETTINGS, client)
    assert result.resolved_url == f"http://box:3000/arweave/{media_txid}"
    assert result.title == "FV #49"


# --- tokenURI metadata (feedback_nft_largest_image) ---

METADATA_URL = "http://ipfs.internal/ipfs/bafyMETA/meta.json"


async def test_json_cid_resolves_through_metadata_to_animation_url():
    routes = {
        "http://ipfs.internal/ipfs/bafyMETA": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"name": "Piece", "animation_url": "ipfs://bafyANIM/index.html"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyMETA", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyANIM/index.html"
    assert result.playback_method == "send"
    assert result.title == "Piece"
    assert result.provider == "ipfs"


async def test_largest_image_wins_by_content_length_not_field_name():
    routes = {
        METADATA_URL: {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {
                "name": "machines",
                "image": "ipfs://bafySMALL/art.png",
                "image_url": "ipfs://bafyBIG/art.png",
            },
        },
        "http://ipfs.internal/ipfs/bafySMALL/art.png": {
            "status_code": 200,
            "headers": {"content-type": "image/png"},
            "content": b"0" * 100,
        },
        "http://ipfs.internal/ipfs/bafyBIG/art.png": {
            "status_code": 200,
            "headers": {"content-type": "image/png"},
            "content": b"0" * 5200,
        },
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyMETA/meta.json", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyBIG/art.png"
    assert result.title == "machines"


async def test_tezos_artifact_uri_wins_over_images():
    routes = {
        METADATA_URL: {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {
                "name": "tez",
                "artifactUri": "ipfs://bafyART/work.mp4",
                "displayUri": "ipfs://bafyDISP/preview.png",
            },
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyMETA/meta.json", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyART/work.mp4"


async def test_metadata_recursion_is_bounded():
    routes = {
        "http://ipfs.internal/ipfs/bafyLOOP": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"animation_url": "ipfs://bafyLOOP"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("ipfs://bafyLOOP", SETTINGS, client)
    assert result.resolved is False


# --- verse.works scrape ---

VERSE_URL = "https://verse.works/artworks/foo"


async def test_verse_page_scrapes_token_uri_and_recurses():
    page = r'window.__DATA__ = "{\"tokenUri\":\"ipfs://bafyMETA/meta.json\"}"'
    routes = {
        VERSE_URL: {"status_code": 200, "text": page},
        METADATA_URL: {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"name": "V", "animation_url": "ipfs://bafyANIM/index.html"},
        },
    }
    async with fake_net(routes) as client:
        result = await resolve_ref(VERSE_URL, SETTINGS, client)
    assert result.provider == "verse"
    assert result.resolved is True
    assert result.resolved_url == "http://box:8080/ipfs/bafyANIM/index.html"
    assert result.playback_method == "send"
    assert result.title == "V"


async def test_verse_iframe_url_is_sent():
    page = r'"{\"iframeUrl\":\"https://player.verse.works/piece.html\"}"'
    routes = {VERSE_URL: {"status_code": 200, "text": page}}
    async with fake_net(routes) as client:
        result = await resolve_ref(VERSE_URL, SETTINGS, client)
    assert result.resolved_url == "https://player.verse.works/piece.html"
    assert result.playback_method == "send"
    assert result.provider == "verse"


async def test_verse_og_image_fallback_uses_source_variant():
    page = '<meta property="og:image" content="https://verse.works/image/w/some%2Fpath.png@2x"/>'
    routes = {VERSE_URL: {"status_code": 200, "text": page}}
    async with fake_net(routes) as client:
        result = await resolve_ref(VERSE_URL, SETTINGS, client)
    assert result.resolved_url == "https://verse.works/image/source/some%2Fpath.png"
    assert result.playback_method == "play"


async def test_verse_edition_page_falls_back_to_base_artwork_page():
    # Edition sub-pages carry only verse's site-generic og:image; the base
    # artwork page has the real one.
    generic = '<meta property="og:image" content="https://verse.works/opengraph-image.png?cafe"/>'
    real = '<meta property="og:image" content="https://verse.works/image/w1400/static%2Fart.jpg@jpeg"/>'
    routes = {
        "https://verse.works/artworks/uuid-1/485:0": {"status_code": 200, "text": generic},
        "https://verse.works/artworks/uuid-1": {"status_code": 200, "text": real},
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("https://verse.works/artworks/uuid-1/485:0", SETTINGS, client)
    assert result.resolved is False
    assert "media fetch failed" in (result.note or "")


async def test_verse_fetch_failure_is_reported_not_raised():
    async with fake_net() as client:  # verse page 404s
        result = await resolve_ref(VERSE_URL, SETTINGS, client)
    assert result.resolved is False
    assert result.provider == "verse"


# --- data: URIs (fully on-chain tokenURIs) ---


async def test_base64_data_uri_metadata_recurses():
    import base64
    import json

    payload = base64.b64encode(
        json.dumps({"name": "Onchain", "image": "ipfs://bafyIMG/one.png"}).encode()
    ).decode()
    async with no_net() as client:
        result = await resolve_ref(f"data:application/json;base64,{payload}", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyIMG/one.png"
    assert result.title == "Onchain"
    assert result.provider == "data"
    assert result.resolved is True


async def test_plain_data_uri_metadata_recurses():
    ref = 'data:application/json,{"animation_url":"ipfs://bafyANIM/index.html"}'
    async with no_net() as client:
        result = await resolve_ref(ref, SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyANIM/index.html"
    assert result.playback_method == "send"


async def test_data_uri_media_passes_through():
    ref = "data:image/svg+xml;base64,PHN2Zy8+"
    async with no_net() as client:
        result = await resolve_ref(ref, SETTINGS, client)
    assert "/media/" in result.resolved_url
    assert result.resolved is True
    assert result.content_type == "image/svg+xml"
    assert result.playback_method == "play"


async def test_undecodable_data_uri_is_flagged_not_raised():
    async with no_net() as client:
        result = await resolve_ref("data:application/json;base64,%%%", SETTINGS, client)
    assert result.resolved is False
    assert result.note is not None


# --- direct tokenURI over http ---


async def test_direct_json_url_is_treated_as_token_metadata():
    routes = {
        "https://api.example.com/token/1.json": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"name": "One", "image": "ipfs://bafyIMG/one.png"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("https://api.example.com/token/1.json", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyIMG/one.png"
    assert result.title == "One"
    assert result.provider == "token-metadata"


async def test_extensionless_url_sniffed_as_json_is_metadata():
    routes = {
        "https://api.example.com/token/1": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "json": {"name": "One", "image": "ipfs://bafyIMG/one.png"},
        }
    }
    async with fake_net(routes) as client:
        result = await resolve_ref("https://api.example.com/token/1", SETTINGS, client)
    assert result.resolved_url == "http://box:8080/ipfs/bafyIMG/one.png"
