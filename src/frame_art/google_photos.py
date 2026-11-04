"""Signing in to Google Photos (Picker API) and keeping that sign-in.

The client is a Google "Desktop app" OAuth client, kept in the keys dir. Sign-in
uses PKCE with a loopback redirect nobody listens on: Google sends the browser to
``http://127.0.0.1:8765/?code=...``, the page fails to load, and Brian pastes the
address back into frame-art. The token file stays in the data dir (chmod 600);
nothing here ever hands the client secret or a token to a page or a log.
docs/google-photos.md is the setup guide.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .keys import keys_dir
from .net import Http
from .photos import PhotoStore

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
REDIRECT = "http://127.0.0.1:8765"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
PICKER = "https://photospicker.googleapis.com/v1"
# Refresh a little early so a token never expires mid-request.
EARLY = 60


class SignInNeeded(Exception):
    """No sign-in, or Google has ended it (weekly, while the app is in Testing)."""


def client_path() -> Path:
    return keys_dir() / "google-photos" / "client.json"


def load_client() -> dict | None:
    """The OAuth client, for this module only; never pass it to a template or log."""
    try:
        data = json.loads(client_path().read_text())
    except (OSError, ValueError):
        return None
    c = data.get("installed") or data.get("web") if isinstance(data, dict) else None
    return c if isinstance(c, dict) and c.get("client_id") else None


def has_client() -> bool:
    return load_client() is not None


def new_pkce() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def auth_url(client: dict, state: str, challenge: str) -> str:
    query = {
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # always hand back a refresh token
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return client.get("auth_uri", AUTH_URI) + "?" + urlencode(query)


def _write(token_path: Path, token: dict) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(token, f)
    tmp.chmod(0o600)
    tmp.replace(token_path)


def _token_call(http: Http, client: dict, form: dict) -> dict:
    """POST to Google's token endpoint once; the reply, or raise with its error."""
    form = {"client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""), **form}  # fmt: skip
    try:
        resp = http.request("POST", client.get("token_uri", TOKEN_URI), data=form,
                            retries=0)  # fmt: skip
    except httpx.HTTPError as ex:
        # Never chain: the request, with the secret, hangs off the exception.
        raise ConnectionError(f"Google didn't answer: {type(ex).__name__}") from None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or "access_token" not in data:
        raise PermissionError(str(data.get("error") or f"HTTP {resp.status_code}"))
    return data


def _code(pasted: str, state: str) -> str:
    pasted = pasted.strip()
    if "://" not in pasted and "code=" not in pasted:
        if not pasted:
            raise ValueError("nothing pasted")
        return pasted
    query = parse_qs(urlparse(pasted).query)
    if "error" in query:
        raise ValueError(f"Google said: {query['error'][0]}")
    if query.get("state", [state])[0] != state:
        raise ValueError("that sign-in didn't match")
    if not query.get("code"):
        raise ValueError("no code in that address")
    return query["code"][0]


def exchange(http: Http, client: dict, pasted: str, verifier: str, state: str,
             token_path: Path) -> None:  # fmt: skip
    """Trade the pasted address (or bare code) for tokens and save them.

    Raises ValueError for a paste that isn't this sign-in's, or one Google refuses.
    """
    code = _code(pasted, state)
    try:
        t = _token_call(http, client, {
            "code": code, "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": REDIRECT,
        })  # fmt: skip
    except (PermissionError, ConnectionError) as e:
        raise ValueError(f"Google refused the sign-in: {e}") from None
    _write(token_path, {
        "access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
        "expires_at": time.time() + float(t.get("expires_in", 3600)),
    })  # fmt: skip


def signed_in(token_path: Path) -> bool:
    try:
        return bool(json.loads(token_path.read_text()).get("refresh_token"))
    except (OSError, ValueError):
        return False


def access_token(http: Http, client: dict, token_path: Path) -> str:
    """A current access token, refreshed if needed. Raises SignInNeeded."""
    try:
        token = json.loads(token_path.read_text())
    except (OSError, ValueError):
        raise SignInNeeded("not signed in") from None
    if token.get("expires_at", 0) - EARLY > time.time():
        return token["access_token"]
    if not token.get("refresh_token"):
        raise SignInNeeded("not signed in")
    try:
        t = _token_call(http, client, {
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
        })  # fmt: skip
    except PermissionError as e:
        if str(e) in ("invalid_grant", "unauthorized_client", "invalid_client"):
            # Ended by Google (weekly in Testing) or revoked: forget it.
            token_path.unlink(missing_ok=True)
            raise SignInNeeded(str(e)) from None
        raise ConnectionError(f"Google refused a refresh: {e}") from None
    token.update(access_token=t["access_token"],
                 expires_at=time.time() + float(t.get("expires_in", 3600)))  # fmt: skip
    if t.get("refresh_token"):
        token["refresh_token"] = t["refresh_token"]
    _write(token_path, token)
    return token["access_token"]


# -- Picking -------------------------------------------------------------------


@dataclass(frozen=True)
class PickSession:
    id: str
    picker_uri: str
    expires: float  # when Google stops waiting for the pick


def _picker(http: Http, token: str, method: str, url: str, **kw):
    """One Picker API call. Raises SignInNeeded on 401, ConnectionError otherwise."""
    try:
        resp = http.request(method, url, headers={"Authorization": f"Bearer {token}"},
                            **kw)  # fmt: skip
    except httpx.HTTPError as ex:
        raise ConnectionError(
            f"Google Photos didn't answer: {type(ex).__name__}"
        ) from None
    if resp.status_code == 401:
        raise SignInNeeded("Google Photos refused the sign-in")
    if not 200 <= resp.status_code < 300:
        raise ConnectionError(f"Google Photos returned {resp.status_code}")
    return resp


def _seconds(duration: str | None, default: float) -> float:
    """Google's "1799.96s" durations."""
    try:
        return float(str(duration).rstrip("s"))
    except ValueError:
        return default


def create_session(http: Http, token: str) -> PickSession:
    d = _picker(http, token, "POST", f"{PICKER}/sessions", json={}, retries=0).json()
    timeout = _seconds((d.get("pollingConfig") or {}).get("timeoutIn"), 30 * 60)
    return PickSession(d["id"], d["pickerUri"], time.time() + timeout)


def session_done(http: Http, token: str, id: str) -> bool:
    """True once Brian has pressed Done in the picker."""
    d = _picker(http, token, "GET", f"{PICKER}/sessions/{id}").json()
    return bool(d.get("mediaItemsSet"))


def picked_items(http: Http, token: str, id: str) -> list[dict]:
    """Every photo picked in session ``id`` (videos are left out)."""
    items, page = [], None
    while True:
        q = {"sessionId": id, "pageSize": 100}
        if page:
            q["pageToken"] = page
        d = _picker(http, token, "GET", f"{PICKER}/mediaItems?{urlencode(q)}").json()
        items += [i for i in d.get("mediaItems", []) if i.get("type") == "PHOTO"]
        page = d.get("nextPageToken")
        if not page:
            return items


def download(http: Http, token: str, item: dict) -> bytes:
    """The original file (``=d``) of a picked item."""
    return _picker(http, token, "GET", item["mediaFile"]["baseUrl"] + "=d").content


def delete_session(http: Http, token: str, id: str) -> None:
    """Close the session; Google drops it on its own later if this fails."""
    try:
        _picker(http, token, "DELETE", f"{PICKER}/sessions/{id}", retries=0)
    except (ConnectionError, SignInNeeded) as e:
        log.warning("couldn't close picker session: %s", e)


def import_picked(store: PhotoStore, http: Http, token: str, id: str) -> dict:
    """Copy the photos picked in session ``id`` into ``store``, then close it."""
    added, skipped, failed = 0, 0, []
    known = {p.source_id for p in store.all() if p.source == "google-photos"}
    for item in picked_items(http, token, id):
        name = item["mediaFile"].get("filename") or item["id"]
        if item["id"] in known:
            skipped += 1
            continue
        try:
            p = store.add(
                "google-photos", item["id"], download(http, token, item), name
            )
        except (ConnectionError, ValueError) as e:
            log.warning("%s: not added: %s", name, e)
            failed.append({"filename": name, "error": str(e)})
            continue
        if p is None:
            skipped += 1
        else:
            added += 1
    delete_session(http, token, id)
    return {"added": added, "skipped": skipped, "failed": failed}
