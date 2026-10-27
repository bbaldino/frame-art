import base64
import io

import pytest
from PIL import Image

from frame_art import openrouter


def png_data_url(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, status_code, payload):
        self.response = FakeResponse(status_code, payload)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.response


def test_edit_sends_image_and_reads_result():
    http = FakeHttp(
        200,
        {
            "id": "gen-1",
            "usage": {"cost": 0.118},
            "choices": [
                {"message": {"images": [{"image_url": {"url": png_data_url(64, 36)}}]}}
            ],
        },
    )
    out = openrouter.edit_image(
        http, "sk-secret", Image.new("RGB", (3000, 2000)), "spooky"
    )
    assert (
        out.image.size == (64, 36)
        and out.cost == 0.118
        and out.generation_id == "gen-1"
    )
    assert http.calls[0]["method"] == "POST" and http.calls[0]["url"] == openrouter.URL
    body = http.calls[0]["json"]
    assert body["model"] == openrouter.MODEL
    assert body["modalities"] == ["image", "text"]
    assert body["usage"] == {"include": True}
    assert body["image_config"] == {"aspect_ratio": "3:2", "image_size": "4K"}
    assert body["messages"][0]["content"][0] == {"type": "text", "text": "spooky"}
    assert http.calls[0]["headers"]["Authorization"] == "Bearer sk-secret"
    sent = Image.open(
        io.BytesIO(
            base64.b64decode(
                body["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
            )
        )
    )
    assert max(sent.size) == 2048


@pytest.mark.parametrize(
    "status,payload",
    [
        (402, {"error": {"message": "Insufficient credits"}}),
        (200, {"error": {"message": "boom"}}),
        (200, {"choices": [{"message": {"content": "no can do"}}]}),
    ],
)
def test_edit_failures_never_mention_the_key(status, payload):
    with pytest.raises(openrouter.EditFailed) as ex:
        openrouter.edit_image(
            FakeHttp(status, payload), "sk-secret", Image.new("RGB", (10, 10)), "p"
        )
    assert "sk-secret" not in str(ex.value)


def test_failure_includes_status_and_message():
    with pytest.raises(openrouter.EditFailed, match="402.*Insufficient credits"):
        openrouter.edit_image(
            FakeHttp(402, {"error": {"message": "Insufficient credits"}}),
            "k",
            Image.new("RGB", (10, 10)),
            "p",
        )


def test_nearest_aspect():
    assert openrouter.nearest_aspect(3840, 2160) == "16:9"
    assert openrouter.nearest_aspect(2000, 2600) == "3:4"


def test_edit_asks_for_the_given_aspect():
    http = FakeHttp(
        200,
        {
            "choices": [
                {"message": {"images": [{"image_url": {"url": png_data_url(16, 9)}}]}}
            ]
        },
    )
    openrouter.edit_image(http, "k", Image.new("RGB", (1600, 1000)), "p", aspect="16:9")
    assert http.calls[0]["json"]["image_config"]["aspect_ratio"] == "16:9"
