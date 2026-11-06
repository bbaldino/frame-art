import httpx

from frame_art import net
from frame_art.net import Http


def client_answering(statuses: list[tuple[int, dict]]) -> tuple[Http, list[str]]:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        status, headers = statuses.pop(0)
        return httpx.Response(status, headers=headers, content=b"x")

    h = Http()
    h._client = httpx.Client(transport=httpx.MockTransport(handler))
    return h, seen


def test_429_waits_as_long_as_retry_after_says(monkeypatch):
    sleeps = []
    monkeypatch.setattr(net.time, "sleep", sleeps.append)
    h, seen = client_answering([(429, {"retry-after": "11"}), (200, {})])
    assert h.get_bytes("https://example.org/a.jpg", retries=1) == b"x"
    assert len(seen) == 2 and 11 in sleeps


def test_retry_after_is_capped_and_ignored_when_unreadable(monkeypatch):
    sleeps = []
    monkeypatch.setattr(net.time, "sleep", sleeps.append)
    h, _ = client_answering(
        [(429, {"retry-after": "3600"}), (503, {"retry-after": "soon"}), (200, {})]
    )
    h.get_bytes("https://example.org/a.jpg", retries=2)
    assert (
        net.MAX_RETRY_AFTER in sleeps and 2 in sleeps
    )  # 2: the second attempt's backoff
