import json
import time

import pytest

from frame_art import google_photos as gp

CLIENT = {
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "SEKRIT",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, *replies):
        self.replies = [FakeResponse(*r) for r in replies]
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.replies.pop(0)


def test_auth_url_uses_pkce_and_never_the_secret():
    url = gp.auth_url(CLIENT, "st", "chal")
    assert "code_challenge_method=S256" in url and "code_challenge=chal" in url
    assert "photospicker.mediaitems.readonly" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765" in url
    assert "state=st" in url and "SEKRIT" not in url


@pytest.mark.parametrize(
    "pasted",
    [
        "http://127.0.0.1:8765/?state=S&iss=https://accounts.google.com&code=4/C"
        "&scope=https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
        "  4/C  ",
    ],
)
def test_exchange_takes_the_address_or_the_bare_code(tmp_path, pasted):
    http = FakeHttp(
        (200, {"access_token": "A", "refresh_token": "R", "expires_in": 3599})
    )
    token = tmp_path / "google-photos" / "token.json"
    gp.exchange(http, CLIENT, pasted, "VER", "S", token)
    sent = http.calls[0]
    assert sent["method"] == "POST" and sent["url"] == CLIENT["token_uri"]
    assert sent["data"]["code"] == "4/C" and sent["data"]["code_verifier"] == "VER"
    assert sent["data"]["redirect_uri"] == gp.REDIRECT
    assert token.stat().st_mode & 0o777 == 0o600
    saved = json.loads(token.read_text())
    assert saved["refresh_token"] == "R" and saved["expires_at"] > time.time() + 3000
    assert gp.signed_in(token)


def test_exchange_refuses_another_sign_in(tmp_path):
    http = FakeHttp()
    with pytest.raises(ValueError):
        gp.exchange(http, CLIENT, "http://127.0.0.1:8765/?state=X&code=C", "V", "S",
                    tmp_path / "t.json")  # fmt: skip
    with pytest.raises(ValueError):
        gp.exchange(http, CLIENT, "http://127.0.0.1:8765/?state=S", "V", "S",
                    tmp_path / "t.json")  # fmt: skip
    assert http.calls == [] and not (tmp_path / "t.json").exists()


def test_exchange_reports_googles_refusal(tmp_path):
    http = FakeHttp(
        (400, {"error": "invalid_grant", "error_description": "Bad Request"})
    )
    with pytest.raises(ValueError, match="invalid_grant"):
        gp.exchange(http, CLIENT, "C", "V", "S", tmp_path / "t.json")


def _token(path, expires_at):
    path.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R",
                                "expires_at": expires_at}))  # fmt: skip


def test_access_token_reuses_a_valid_token(tmp_path):
    _token(tmp_path / "t.json", time.time() + 1000)
    http = FakeHttp()
    assert gp.access_token(http, CLIENT, tmp_path / "t.json") == "OLD"
    assert http.calls == []


def test_access_token_refreshes_and_rewrites(tmp_path):
    path = tmp_path / "t.json"
    _token(path, time.time() - 1)
    http = FakeHttp((200, {"access_token": "NEW", "expires_in": 3599}))
    assert gp.access_token(http, CLIENT, path) == "NEW"
    sent = http.calls[0]["data"]
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "R"
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "NEW" and saved["refresh_token"] == "R"
    assert path.stat().st_mode & 0o777 == 0o600


def test_expired_sign_in_needs_a_new_one(tmp_path):
    path = tmp_path / "t.json"
    _token(path, time.time() - 1)
    http = FakeHttp((400, {"error": "invalid_grant"}))
    with pytest.raises(gp.SignInNeeded):
        gp.access_token(http, CLIENT, path)
    assert not gp.signed_in(path)
    with pytest.raises(gp.SignInNeeded):
        gp.access_token(FakeHttp(), CLIENT, tmp_path / "missing.json")


def test_client_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path))
    assert gp.client_path() == tmp_path / "google-photos" / "client.json"
    assert not gp.has_client() and gp.load_client() is None
    gp.client_path().parent.mkdir()
    gp.client_path().write_text(json.dumps({"installed": CLIENT}))
    assert gp.has_client() and gp.load_client() == CLIENT
