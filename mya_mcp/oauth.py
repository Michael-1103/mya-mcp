"""Serveur d'autorisation OAuth 2.1 mono-utilisateur (PKCE S256, DCR, refresh avec rotation).

S'appuie sur le support OAuth du SDK mcp (endpoints /register, /authorize, /token, /revoke et les
métadonnées .well-known). Ce module fournit le "provider" : stockage SQLite (tokens hachés en
SHA-256), page de consentement protégée par un mot de passe (MCP_PASSWORD) et limitation des essais.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("mya.oauth")


def _patch_revocation_request() -> None:
    """Le SDK 1.30 exige `client_secret` dans /revoke, même pour un client public (RFC 7009 l'omet).

    On le rend optionnel, sinon un client public ne pourrait pas révoquer ses tokens.
    """
    try:
        from mcp.server.auth.handlers import revoke

        revoke.RevocationRequest.model_fields["client_secret"].default = None
        revoke.RevocationRequest.model_rebuild(force=True)
    except Exception:  # noqa: BLE001 - pas bloquant, la révocation resterait seulement plus stricte
        log.warning("Impossible d'assouplir RevocationRequest (client_secret reste requis)")


def _advertise_public_clients() -> None:
    """Annonce l'auth "none" (clients publics + PKCE) dans les métadonnées, comme le fait /register."""
    try:
        from mcp.server.auth import routes

        original = routes.build_metadata

        def build_metadata(*args, **kwargs):
            md = original(*args, **kwargs)
            methods = list(md.token_endpoint_auth_methods_supported or [])
            if "none" not in methods:
                md.token_endpoint_auth_methods_supported = ["none", *methods]
            return md

        routes.build_metadata = build_metadata
    except Exception:  # noqa: BLE001
        log.warning("Impossible d'ajouter 'none' aux méthodes d'auth annoncées")


_patch_revocation_request()
_advertise_public_clients()

SCOPE = "mya"
EXTRA_SCOPE = "offline_access"  # accepté pour les clients qui le demandent
ACCESS_TTL_S = 3600  # access token : 1 h
REFRESH_TTL_S = 60 * 24 * 3600  # refresh token : 60 jours (renouvelé à chaque rotation)
CODE_TTL_S = 300  # code d'autorisation : 5 min
TXN_TTL_S = 600  # demande d'autorisation en attente : 10 min
MAX_TXN = 500
MAX_CLIENTS = 500
MAX_ATTEMPTS_PER_TXN = 5
IP_MAX_FAILS = 5
GLOBAL_MAX_FAILS = 50
FAIL_WINDOW_S = 900  # 15 min

# Redirections acceptées à l'enregistrement : callbacks Claude hébergés et boucle locale (Claude Code)
ALLOWED_HTTPS_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def redirect_uri_allowed(uri: str) -> bool:
    if uri in ALLOWED_HTTPS_REDIRECTS:
        return True
    p = urlparse(uri)
    return p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"} and not p.fragment


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS codes (
    hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
    resource TEXT, expires_at INTEGER NOT NULL, grant_id TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tokens_grant ON tokens(grant_id);
"""


class MyaOAuthProvider:
    """Implémente OAuthAuthorizationServerProvider pour un seul utilisateur (le propriétaire)."""

    def __init__(self, db_path, public_url: str, password: str) -> None:
        self.db_path = db_path
        self.public_url = public_url
        self._pw_digest = hashlib.sha256(password.encode()).digest()
        self._lock = threading.Lock()
        self._txns: dict[str, dict] = {}
        self._fails: dict[str, list[float]] = {}
        self._global_fails: list[float] = []
        self.secure_cookie = public_url.startswith("https://")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # Clients (DCR)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._conn() as c:
            row = c.execute("SELECT data FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "Au moins une redirect_uri est requise")
        bad = [u for u in uris if not redirect_uri_allowed(u)]
        if bad:
            raise RegistrationError(
                "invalid_redirect_uri",
                "redirect_uri non autorisée (seuls le callback Claude et la boucle locale http://localhost sont acceptés)")
        client_info.scope = f"{SCOPE} {EXTRA_SCOPE}"  # scopes unifiés, ils n'ont pas d'effet ici
        now = int(time.time())
        with self._conn() as c:
            # nettoyage : clients de plus de 7 jours qui n'ont jamais obtenu de token
            c.execute(
                "DELETE FROM clients WHERE created_at < ? AND client_id NOT IN (SELECT client_id FROM tokens)",
                (now - 7 * 86400,))
            n = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            if n >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Trop de clients enregistrés")
            c.execute("INSERT INTO clients(client_id, data, created_at) VALUES(?,?,?)",
                      (client_info.client_id, client_info.model_dump_json(), now))

    # Autorisation : la page de consentement

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        for k in [k for k, t in self._txns.items() if t["expires"] < now]:
            del self._txns[k]
        while len(self._txns) >= MAX_TXN:
            self._txns.pop(next(iter(self._txns)))
        txn = secrets.token_urlsafe(24)
        self._txns[txn] = {
            "client_id": client.client_id, "client_name": client.client_name,
            "params": params.model_dump(mode="json"), "csrf": secrets.token_urlsafe(24),
            "attempts": 0, "expires": now + TXN_TTL_S,
        }
        return f"{self.public_url}/consent?txn={txn}"

    def _page(self, body: str, status: int = 200, csrf: str | None = None) -> HTMLResponse:
        doc = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Connexion au serveur MYA</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem;line-height:1.5}}
h1{{font-size:1.25rem}} .box{{border:1px solid #8886;border-radius:8px;padding:1.25rem}}
label{{display:block;margin:1rem 0 .25rem}} input[type=password]{{width:100%;padding:.5rem;box-sizing:border-box;font-size:1rem}}
button{{padding:.5rem 1rem;font-size:1rem;margin-right:.5rem;cursor:pointer}}
.err{{color:#b00020}} small{{opacity:.75}}
</style></head><body><div class="box">{body}</div></body></html>"""
        resp = HTMLResponse(doc, status_code=status)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # pas de form-action : Chrome l'applique aussi à la redirection finale vers le client
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        if csrf:
            resp.set_cookie("mya_csrf", csrf, max_age=TXN_TTL_S, httponly=True, samesite="strict",
                            secure=self.secure_cookie, path="/consent")
        return resp

    def _form(self, txn_id: str, txn: dict, error: str | None = None, status: int = 200) -> HTMLResponse:
        redirect = urlparse(txn["params"]["redirect_uri"])
        name = html.escape(txn["client_name"] or "Application inconnue")
        err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
        body = f"""<h1>Autoriser l'accès aux destinations MYA</h1>
<p><strong>{name}</strong> demande l'accès à ce serveur MCP (lecture seule).<br>
<small>Retour après validation vers : <code>{html.escape(redirect.netloc)}</code></small></p>
{err}
<form method="post" action="/consent" autocomplete="off">
<input type="hidden" name="txn" value="{html.escape(txn_id)}">
<input type="hidden" name="csrf" value="{html.escape(txn['csrf'])}">
<label for="pw">Mot de passe du serveur</label>
<input id="pw" name="password" type="password" required autofocus autocomplete="current-password">
<p><button type="submit" name="action" value="allow">Autoriser</button>
<button type="submit" name="action" value="deny" formnovalidate>Refuser</button></p>
</form>"""
        return self._page(body, status=status, csrf=txn["csrf"])

    def _expired(self) -> HTMLResponse:
        return self._page("<h1>Demande expirée</h1><p>Relancez la connexion depuis votre application.</p>", 400)

    def _client_ip(self, request: Request) -> str:
        return request.client.host if request.client else "?"

    def _locked(self, ip: str) -> bool:
        now = time.time()
        self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW_S]
        self._global_fails = [t for t in self._global_fails if now - t < FAIL_WINDOW_S]
        return len(self._fails[ip]) >= IP_MAX_FAILS or len(self._global_fails) >= GLOBAL_MAX_FAILS

    async def consent_get(self, request: Request) -> Response:
        txn_id = request.query_params.get("txn", "")
        txn = self._txns.get(txn_id)
        if not txn or txn["expires"] < time.time():
            return self._expired()
        return self._form(txn_id, txn)

    async def consent_post(self, request: Request) -> Response:
        form = await request.form()
        txn_id = str(form.get("txn", ""))
        txn = self._txns.get(txn_id)
        if not txn or txn["expires"] < time.time():
            return self._expired()
        csrf = str(form.get("csrf", ""))
        cookie = request.cookies.get("mya_csrf", "")
        if not (hmac.compare_digest(csrf, txn["csrf"]) and hmac.compare_digest(cookie, txn["csrf"])):
            log.warning("Consentement refusé : jeton CSRF invalide")
            return self._page("<h1>Requête refusée</h1><p>Jeton de sécurité invalide. Relancez la connexion.</p>", 403)
        params = txn["params"]
        redirect_uri = params["redirect_uri"]
        if form.get("action") == "deny":
            del self._txns[txn_id]
            url = construct_redirect_uri(redirect_uri, error="access_denied", state=params.get("state"))
            return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

        ip = self._client_ip(request)
        if self._locked(ip):
            log.warning("Trop d'essais de mot de passe depuis %s", ip)
            return self._page("<h1>Trop d'essais</h1><p>Réessayez dans quelques minutes.</p>", 429)
        supplied = hashlib.sha256(str(form.get("password", "")).encode()).digest()
        if not hmac.compare_digest(supplied, self._pw_digest):
            now = time.time()
            self._fails.setdefault(ip, []).append(now)
            self._global_fails.append(now)
            txn["attempts"] += 1
            log.warning("Mot de passe incorrect depuis %s (essai %d)", ip, txn["attempts"])
            if txn["attempts"] >= MAX_ATTEMPTS_PER_TXN:
                del self._txns[txn_id]
                return self._page("<h1>Trop d'essais</h1><p>Relancez la connexion depuis votre application.</p>", 429)
            return self._form(txn_id, txn, "Mot de passe incorrect.", status=401)

        del self._txns[txn_id]
        code = secrets.token_urlsafe(32)
        scopes = params.get("scopes") or [SCOPE]
        auth_code = AuthorizationCode(
            code=code, scopes=scopes, expires_at=time.time() + CODE_TTL_S, client_id=txn["client_id"],
            code_challenge=params["code_challenge"], redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=params.get("resource"), subject="owner")
        with self._conn() as c:
            c.execute("DELETE FROM codes WHERE expires_at < ?", (int(time.time()) - 3600,))
            c.execute("INSERT INTO codes(hash, data, expires_at) VALUES(?,?,?)",
                      (_h(code), auth_code.model_dump_json(), int(auth_code.expires_at)))
        url = construct_redirect_uri(redirect_uri, code=code, state=params.get("state"))
        resp = RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
        resp.delete_cookie("mya_csrf", path="/consent")
        return resp

    # Codes et tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        with self._conn() as c:
            row = c.execute("SELECT data, used FROM codes WHERE hash = ?", (_h(authorization_code),)).fetchone()
            if not row:
                return None
            if row[1]:
                # réutilisation d'un code : on révoque tout ce qui en est issu (RFC 6749 section 4.1.2)
                log.warning("Code d'autorisation réutilisé, révocation des tokens associés")
                c.execute("DELETE FROM tokens WHERE grant_id = ?", (_h(authorization_code),))
                return None
        return AuthorizationCode.model_validate_json(row[0])

    def _issue(self, c: sqlite3.Connection, client_id: str, scopes: list[str], resource: str | None,
               grant_id: str) -> OAuthToken:
        now = int(time.time())
        c.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
        access, refresh = "mya_at_" + secrets.token_urlsafe(32), "mya_rt_" + secrets.token_urlsafe(32)
        rows = [(_h(access), "access", client_id, json.dumps(scopes), resource, now + ACCESS_TTL_S, grant_id, now),
                (_h(refresh), "refresh", client_id, json.dumps(scopes), resource, now + REFRESH_TTL_S, grant_id, now)]
        c.executemany("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?)", rows)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL_S,
                          scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        h = _h(authorization_code.code)
        with self._conn() as c:
            cur = c.execute("UPDATE codes SET used = 1 WHERE hash = ? AND used = 0", (h,))  # usage unique, atomique
            if cur.rowcount != 1:
                raise TokenError("invalid_grant", "authorization code already used")
            return self._issue(c, client.client_id, authorization_code.scopes, authorization_code.resource, h)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT client_id, scopes, expires_at, resource FROM tokens WHERE hash = ? AND kind = 'refresh'",
                (_h(refresh_token),)).fetchone()
        if not row or row[2] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=row[0], scopes=json.loads(row[1]),
                            expires_at=row[2], resource=row[3], subject="owner")

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        h = _h(refresh_token.token)
        with self._conn() as c:
            row = c.execute("SELECT grant_id FROM tokens WHERE hash = ? AND kind = 'refresh'", (h,)).fetchone()
            cur = c.execute("DELETE FROM tokens WHERE hash = ? AND kind = 'refresh'", (h,))  # rotation
            if not row or cur.rowcount != 1:
                raise TokenError("invalid_grant", "refresh token already used")
            return self._issue(c, client.client_id, scopes or refresh_token.scopes, refresh_token.resource, row[0])

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT client_id, scopes, expires_at, resource FROM tokens WHERE hash = ? AND kind = 'access'",
                (_h(token),)).fetchone()
        if not row or row[2] < time.time():
            return None
        return AccessToken(token=token, client_id=row[0], scopes=json.loads(row[1]), expires_at=row[2],
                           resource=row[3], subject="owner")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _h(token.token)
        with self._conn() as c:
            row = c.execute("SELECT grant_id FROM tokens WHERE hash = ?", (h,)).fetchone()
            if row:  # on révoque la paire entière (accès et refresh)
                c.execute("DELETE FROM tokens WHERE grant_id = ?", (row[0],))
