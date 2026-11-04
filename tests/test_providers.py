import base64
import io

from PIL import Image

from frame_art import providers


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


def test_openrouter_provider_paints_at_4k():
    http = FakeHttp(200, {"id": "g", "usage": {"cost": 0.12}, "choices": [
        {"message": {"images": [{"image_url": {"url": png_data_url(64, 36)}}]}}]})  # fmt: skip
    p = providers.OpenRouterProvider(http, "sk")
    out = p.paint(Image.new("RGB", (300, 400)), "p", "16:9")
    assert isinstance(out, providers.Painted) and out.cost == 0.12
    assert out.image.size == (64, 36) and p.name == "openrouter"
    assert http.calls[0]["json"]["image_config"] == {
        "aspect_ratio": "16:9",
        "image_size": "4K",
    }


def test_image_provider_needs_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path))
    assert providers.image_provider(None) is None
    (tmp_path / "openrouter").mkdir()
    (tmp_path / "openrouter" / "key").write_text("sk-secret\n")
    assert isinstance(providers.image_provider(None), providers.OpenRouterProvider)


def test_plain_errors_never_carry_the_key():
    http = FakeHttp(402, {"error": {"message": "requires at least $1.00 in balance"}})
    p = providers.OpenRouterProvider(http, "sk-secret")
    try:
        p.paint(Image.new("RGB", (10, 10)), "p")
    except providers.PaintFailed as e:
        msg = providers.plain_error(e)
    assert msg == "OpenRouter: balance too low" and "sk-secret" not in msg
    no_image = providers.PaintFailed("OpenRouter returned no image")
    assert providers.plain_error(no_image) == "OpenRouter: no image came back"
    assert providers.plain_error(
        providers.PaintFailed("OpenRouter returned 500: x")
    ) == ("OpenRouter returned 500: x")
