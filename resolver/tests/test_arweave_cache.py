import httpx

from resolver.arweave_cache import keep_arweave
from resolver.config import Settings


async def test_keep_fully_fetches_and_verifies_the_same_core_cache():
    settings = Settings(arweave_internal="http://core.internal")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        headers = {"x-cache": "HIT"} if len(calls) == 2 else {}
        return httpx.Response(200, headers=headers, content=b"cached bytes")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keep_arweave("A" * 43, "/manifest/item.png", settings, client) == "kept"

    assert [(request.method, str(request.url)) for request in calls] == [
        ("GET", f"http://core.internal/{'A' * 43}/manifest/item.png"),
        ("GET", f"http://core.internal/{'A' * 43}/manifest/item.png"),
    ]


async def test_keep_fails_without_a_same_core_native_hit():
    settings = Settings(arweave_internal="http://core.internal")

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"x-cache": "MISS"}, content=b"bytes")
    )) as client:
        assert await keep_arweave("B" * 43, "", settings, client) == "failed"
