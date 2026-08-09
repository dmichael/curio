import httpx

from resolver.arweave_cache import store_arweave
from resolver.config import Settings


async def test_keep_fully_fetches_and_verifies_the_same_core_cache():
    settings = Settings(
        arweave_internal="http://core.internal",
        arweave_cold_timeout=17,
        seed_pin_timeout=999,
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        headers = {"x-cache": "HIT"} if len(calls) == 2 else {}
        return httpx.Response(200, headers=headers, content=b"cached bytes")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await store_arweave("A" * 43, "/manifest/item.png", settings, client) == "stored"

    assert [(request.method, str(request.url)) for request in calls] == [
        ("GET", f"http://core.internal/{'A' * 43}/manifest/item.png"),
        ("GET", f"http://core.internal/{'A' * 43}/manifest/item.png"),
    ]
    assert [request.extensions["timeout"]["read"] for request in calls] == [17, 17]


async def test_keep_fails_without_a_same_core_native_hit():
    settings = Settings(arweave_internal="http://core.internal")

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"x-cache": "MISS"}, content=b"bytes")
    )) as client:
        assert await store_arweave("B" * 43, "", settings, client) == "failed"
