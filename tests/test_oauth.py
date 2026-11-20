"""Flux OAuth complet : métadonnées, DCR, consentement, PKCE, refresh, révocation, accès /mcp."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sqlite3
import time as _time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from mya_mcp.server import build_app
from test_all import fake_scrape, make_dest, settings

CB = "https://claude.ai/api/mcp/auth_callback"
PW = "mot-de-passe-de-test"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
BASE = "http://localhost:8000"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(c, redirect=CB):
    return c.post("/register", json={
        "redirect_uris": [redirect], "client_name": "Test <b>x</b>", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def start_authorize(c, client_id, challenge, redirect=CB, state="st1"):
    r = c.get("/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "resource": f"{BASE}/mcp"}, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["location"]


def form_fields(page):
    return (re.search(r'name="txn" value="([^"]+)"', page.text).group(1),
            re.search(r'name="csrf" value="([^"]+)"', page.text).group(1))


def get_code(c, client_id, challenge, redirect=CB, password=PW):
    page = c.get(start_authorize(c, client_id, challenge, redirect))
    assert page.status_code == 200
    txn, csrf = form_fields(page)
    return c.post("/consent", data={"txn": txn, "csrf": csrf, "password": password, "action": "allow"},
                  follow_redirects=False)


def code_of(r):
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def exchange(c, client_id, verifier, code, redirect=CB):
    return c.post("/token", data={"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                  "code_verifier": verifier, "redirect_uri": redirect})


def bearer(token):
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def full_login(c):
    cid = register(c).json()["client_id"]
    verifier, challenge = pkce()
    r = get_code(c, cid, challenge)
    assert r.status_code == 303 and parse_qs(urlparse(r.headers["location"]).query)["state"] == ["st1"]
    tok = exchange(c, cid, verifier, code_of(r))
    assert tok.status_code == 200, tok.text
    return cid, tok.json()


@pytest.fixture()
def c(tmp_path, monkeypatch):
    fake_scrape(monkeypatch, [make_dest(i) for i in range(1, 6)])
    with TestClient(build_app(settings(tmp_path)), base_url=BASE) as client:  # lifespan inclus
        yield client


def test_health_public_and_401_with_resource_metadata(c):
    assert c.get("/health").status_code == 200
    r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    # le mot de passe n'est PAS un token Bearer valide
    assert c.post("/mcp", json=INIT, headers=bearer(PW)).status_code == 401
    assert c.post("/mcp", json=INIT, headers=bearer("nope")).status_code == 401


def test_metadata(c):
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{BASE}/mcp" and [a.rstrip("/") for a in prm["authorization_servers"]] == [BASE]
    assert c.get("/.well-known/oauth-protected-resource").json()["resource"] == f"{BASE}/mcp"
    md = c.get("/.well-known/oauth-authorization-server").json()
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert "none" in md["token_endpoint_auth_methods_supported"]
    for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert md[k].startswith(BASE + "/")


def test_register_validates_redirects(c):
    assert register(c).status_code == 201
    assert register(c, "http://localhost:53682/callback").status_code == 201
    assert register(c, "http://127.0.0.1:1234/cb").status_code == 201
    for bad in ("https://evil.example/cb", "http://evil.example/cb",
                "https://claude.ai.evil.example/api/mcp/auth_callback", "https://claude.ai/other"):
        assert register(c, bad).status_code == 400, bad


def test_authorize_rejects_unregistered_redirect_and_plain_pkce(c):
    cid = register(c).json()["client_id"]
    _, ch = pkce()
    r = c.get("/authorize", params={"client_id": cid, "redirect_uri": "https://evil.example/cb", "response_type": "code",
                                    "code_challenge": ch, "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers
    r = c.get("/authorize", params={"client_id": cid, "redirect_uri": CB, "response_type": "code",
                                    "code_challenge": ch, "code_challenge_method": "plain"}, follow_redirects=False)
    assert "/consent" not in r.headers.get("location", "")


def test_consent_page_escaping_and_headers(c):
    cid = register(c).json()["client_id"]
    page = c.get(start_authorize(c, cid, pkce()[1]))
    assert "Test &lt;b&gt;x&lt;/b&gt;" in page.text and "<b>x</b>" not in page.text
    assert page.headers["x-frame-options"] == "DENY" and "no-store" in page.headers["cache-control"]
    cookie = page.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_wrong_password_then_right_and_txn_single_use(c):
    cid = register(c).json()["client_id"]
    page = c.get(start_authorize(c, cid, pkce()[1]))
    txn, csrf = form_fields(page)
    r = c.post("/consent", data={"txn": txn, "csrf": csrf, "password": "faux", "action": "allow"}, follow_redirects=False)
    assert r.status_code == 401 and "incorrect" in r.text and "location" not in r.headers
    r = c.post("/consent", data={"txn": txn, "csrf": csrf, "password": PW, "action": "allow"}, follow_redirects=False)
    assert r.status_code == 303 and "code=" in r.headers["location"]
    r = c.post("/consent", data={"txn": txn, "csrf": csrf, "password": PW, "action": "allow"}, follow_redirects=False)
    assert r.status_code == 400


def test_csrf_required(c):
    cid = register(c).json()["client_id"]
    page = c.get(start_authorize(c, cid, pkce()[1]))
    txn, csrf = form_fields(page)
    r = c.post("/consent", data={"txn": txn, "csrf": "mauvais", "password": PW, "action": "allow"}, follow_redirects=False)
    assert r.status_code == 403
    c.cookies.clear()  # bon champ mais sans cookie
    r = c.post("/consent", data={"txn": txn, "csrf": csrf, "password": PW, "action": "allow"}, follow_redirects=False)
    assert r.status_code == 403


def test_deny(c):
    cid = register(c).json()["client_id"]
    txn, csrf = form_fields(c.get(start_authorize(c, cid, pkce()[1])))
    r = c.post("/consent", data={"txn": txn, "csrf": csrf, "password": "", "action": "deny"}, follow_redirects=False)
    assert r.status_code == 303 and "error=access_denied" in r.headers["location"] and "state=st1" in r.headers["location"]


def test_rate_limit_per_ip(c):
    statuses = []
    for _ in range(6):
        cid = register(c).json()["client_id"]
        txn, csrf = form_fields(c.get(start_authorize(c, cid, pkce()[1])))
        statuses.append(c.post("/consent", data={"txn": txn, "csrf": csrf, "password": "faux", "action": "allow"},
                               follow_redirects=False).status_code)
    assert statuses == [401] * 5 + [429]
    cid = register(c).json()["client_id"]
    assert get_code(c, cid, pkce()[1]).status_code == 429  # même avec le bon mot de passe pendant le blocage


def test_too_many_attempts_on_one_txn(c, monkeypatch):
    monkeypatch.setattr("mya_mcp.oauth.IP_MAX_FAILS", 100)
    cid = register(c).json()["client_id"]
    txn, csrf = form_fields(c.get(start_authorize(c, cid, pkce()[1])))
    codes = [c.post("/consent", data={"txn": txn, "csrf": csrf, "password": "faux", "action": "allow"},
                    follow_redirects=False).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 429]
    assert c.post("/consent", data={"txn": txn, "csrf": csrf, "password": PW, "action": "allow"},
                  follow_redirects=False).status_code == 400  # demande invalidée


def test_pkce_and_code_reuse(c):
    cid = register(c).json()["client_id"]
    v, ch = pkce()
    code = code_of(get_code(c, cid, ch))
    bad = exchange(c, cid, secrets.token_urlsafe(48), code)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert exchange(c, cid, v, code, redirect="https://claude.com/api/mcp/auth_callback").status_code == 400
    good = exchange(c, cid, v, code)
    assert good.status_code == 200
    access = good.json()["access_token"]
    assert c.post("/mcp", json=INIT, headers=bearer(access)).status_code == 200
    again = exchange(c, cid, v, code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    # la réutilisation du code révoque les tokens déjà émis
    assert c.post("/mcp", json=INIT, headers=bearer(access)).status_code == 401


def test_code_belongs_to_client_and_expires(c, monkeypatch):
    cid = register(c).json()["client_id"]
    other = register(c).json()["client_id"]
    v, ch = pkce()
    code = code_of(get_code(c, cid, ch))
    assert exchange(c, other, v, code).status_code == 400
    real = _time.time
    monkeypatch.setattr("time.time", lambda: real() + 400)
    res = exchange(c, cid, v, code)
    assert res.status_code == 400 and res.json()["error"] == "invalid_grant"


def test_full_flow_mcp_refresh_revoke(c):
    cid, tok = full_login(c)
    assert tok["expires_in"] == 3600 and tok["token_type"].lower() == "bearer"
    r = c.post("/mcp", json=INIT, headers=bearer(tok["access_token"]))
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "mya-epitech"
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=bearer(tok["access_token"]))
    assert len(r.json()["result"]["tools"]) == 6

    new = c.post("/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]})
    assert new.status_code == 200, new.text
    nt = new.json()
    assert nt["refresh_token"] != tok["refresh_token"] and nt["access_token"] != tok["access_token"]
    old = c.post("/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]})
    assert old.status_code == 400 and old.json()["error"] == "invalid_grant"  # rotation : l'ancien est mort

    assert c.post("/mcp", json=INIT, headers=bearer(nt["access_token"])).status_code == 200
    assert c.post("/revoke", data={"token": nt["access_token"], "client_id": cid}).status_code == 200
    assert c.post("/mcp", json=INIT, headers=bearer(nt["access_token"])).status_code == 401
    assert c.post("/token", data={"grant_type": "refresh_token", "client_id": cid,
                                  "refresh_token": nt["refresh_token"]}).status_code == 400


def test_tokens_hashed_and_survive_restart(tmp_path, monkeypatch):
    fake_scrape(monkeypatch, [make_dest(1)])
    s = settings(tmp_path)
    with TestClient(build_app(s), base_url=BASE) as c1:
        _, tok = full_login(c1)
    rows = sqlite3.connect(s.oauth_db_path).execute("SELECT hash FROM tokens").fetchall()
    assert rows and all(tok["access_token"] not in h[0] and tok["refresh_token"] not in h[0] for h in rows)
    with TestClient(build_app(s), base_url=BASE) as c2:  # "redémarrage" du serveur
        assert c2.post("/mcp", json=INIT, headers=bearer(tok["access_token"])).status_code == 200


def test_access_token_expires_after_one_hour(c, monkeypatch):
    _, tok = full_login(c)
    real = _time.time
    monkeypatch.setattr("time.time", lambda: real() + 3700)
    assert c.post("/mcp", json=INIT, headers=bearer(tok["access_token"])).status_code == 401


def test_config_validation_and_no_auth_mode(tmp_path, monkeypatch):
    from mya_mcp.config import load_settings
    for k in ("MCP_PASSWORD", "PUBLIC_URL", "ALLOW_NO_AUTH"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        load_settings()  # rien de défini
    monkeypatch.setenv("MCP_PASSWORD", "x" * 12)
    with pytest.raises(SystemExit):
        load_settings()  # PUBLIC_URL manquante
    monkeypatch.setenv("PUBLIC_URL", "http://exemple.org")
    with pytest.raises(SystemExit):
        load_settings()  # http refusé hors localhost
    monkeypatch.setenv("PUBLIC_URL", "https://exemple.org/")
    assert load_settings().public_url == "https://exemple.org"
    monkeypatch.setenv("MCP_PASSWORD", "court")
    with pytest.raises(SystemExit):
        load_settings()  # mot de passe trop court
    monkeypatch.setenv("ALLOW_NO_AUTH", "1")
    assert not load_settings().oauth_enabled

    fake_scrape(monkeypatch, [make_dest(1)])
    with TestClient(build_app(settings(tmp_path, allow_no_auth=True, password="", public_url=""))) as c:
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200
        assert c.get("/consent").status_code in (404, 405)
