"""Serveur MCP (FastMCP, streamable HTTP) : outils, authentification OAuth 2.1 et /health."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from . import __version__, db
from .config import Settings, load_settings
from .oauth import MyaOAuthProvider
from .scheduler import Refresher

log = logging.getLogger("mya.server")

YesNo = Literal["YES", "NO"]
Semester = Literal["Full-year only", "Full-year or semester", "Semester only"]


def _json(obj) -> str:
    """JSON compact (sans espaces), accents conservés."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def build_mcp(settings: Settings, provider: MyaOAuthProvider | None = None) -> FastMCP:
    auth_kwargs: dict = {}
    if provider is not None:
        # OAuth 2.1 : le SDK expose /register, /authorize, /token, /revoke et les métadonnées .well-known
        auth_kwargs = {
            "auth_server_provider": provider,
            "auth": AuthSettings(
                issuer_url=settings.public_url,
                resource_server_url=settings.resource_url,
                client_registration_options=ClientRegistrationOptions(enabled=True, default_scopes=["mya"]),
                revocation_options=RevocationOptions(enabled=True),
                validate_token_resource=False,
            ),
        }
    mcp = FastMCP(
        "mya-epitech",
        instructions=(
            "Read-only access to the partner universities (international destinations) listed on "
            "mya.epitech.eu for Epitech students. Typical flow: list_filters to learn valid values, "
            "list_destinations or search_destinations to find candidates, get_destination for the "
            "full sheet, compare_destinations to compare several."
        ),
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
        # L'accès est protégé par OAuth, et le reverse proxy gère le nom d'hôte
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        **auth_kwargs,
    )
    mcp._mcp_server.version = __version__  # sinon serverInfo affiche la version du SDK
    path = settings.db_path

    def ensure_data() -> None:
        if db.count_destinations(path) == 0:
            raise ValueError(
                "Data not available yet: the first scrape of mya.epitech.eu has not completed. "
                "Retry in a minute (see get_data_status).")

    @mcp.tool()
    def list_destinations(
        country: str | None = None,
        language: str | None = None,
        erasmus: YesNo | None = None,
        semester: Semester | None = None,
        max_gpa: float | None = None,
        diploma: YesNo | None = None,
        specialization: str | None = None,
        max_extra_charge_eur: float | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List MYA partner universities (summary only, no long text), sorted by country then name.

        All filters are optional and combined with AND; text filters ignore case and accents.
        - country: exact country name (e.g. "Poland", "USA"); see list_filters.
        - language: teaching language, substring match (e.g. "English").
        - erasmus: "YES" or "NO".
        - semester: "Full-year only", "Full-year or semester" or "Semester only".
        - max_gpa: keep destinations whose GPA requirement is <= this value (0 means no requirement).
        - diploma: "YES" if the destination allows a diploma track, else "NO".
        - specialization: substring of a specialization name (e.g. "security").
        - max_extra_charge_eur: maximum extra tuition/charge in euros (0 = only free destinations).
        - limit (1-100, default 20) and offset for pagination.
        Returns {total, offset, count, results[]}; use get_destination(id) for the full sheet.
        """
        ensure_data()
        return _json(db.list_destinations(
            path, _clamp(limit, 1, 100), max(0, int(offset)), country=country, language=language,
            erasmus=erasmus, semester=semester, max_gpa=max_gpa, diploma=diploma,
            specialization=specialization, max_extra_charge_eur=max_extra_charge_eur))

    @mcp.tool()
    def get_destination(id: int) -> str:
        """Get the complete sheet of one destination by id, including full_text.

        full_text contains the converted HTML sections: overview, administrative steps
        (application, visa, insurance), accommodation, courses and academic calendar, and cost of living.
        Fields: gpa_requirement (0 = none), spots (total number of places), extra_charge_cents,
        erasmus, semester, specializations, url (public page on mya.epitech.eu).
        """
        ensure_data()
        dest = db.get_destination(path, int(id))
        if dest is None:
            raise ValueError(f"No destination with id {id}. Use list_destinations or search_destinations.")
        return _json(dest)

    @mcp.tool()
    def search_destinations(
        query: str,
        country: str | None = None,
        language: str | None = None,
        erasmus: YesNo | None = None,
        semester: Semester | None = None,
        max_gpa: float | None = None,
        diploma: YesNo | None = None,
        specialization: str | None = None,
        max_extra_charge_eur: float | None = None,
        limit: int = 10,
    ) -> str:
        """Full-text search over names, countries, specializations and all sheet texts.

        Accent- and case-insensitive; simple plurals and word prefixes are handled; the query is
        plain words (no special syntax). Same optional filters as list_destinations.
        Results are ranked by relevance (higher score = better) and include a snippet where matches
        are wrapped in [brackets]. 'mode' tells how it matched: fts_and (all words), fts_or (any word)
        or like (substring fallback). limit is 1-50 (default 10).
        """
        ensure_data()
        return _json(db.search_destinations(
            path, query, _clamp(limit, 1, 50), country=country, language=language, erasmus=erasmus,
            semester=semester, max_gpa=max_gpa, diploma=diploma, specialization=specialization,
            max_extra_charge_eur=max_extra_charge_eur))

    @mcp.tool()
    def list_filters() -> str:
        """List the distinct values usable as filters, with the number of destinations for each.

        Returns countries, languages, semesters, erasmus, diploma, specializations and the
        distinct GPA requirement values. Call this first to avoid guessing exact values.
        """
        ensure_data()
        return _json(db.list_filters(path))

    @mcp.tool()
    def compare_destinations(ids: list[int]) -> str:
        """Compare 2 to 6 destinations side by side (summary fields, no long text).

        Returns the destinations and a 'differences' list naming the fields whose values differ.
        Use get_destination for the detailed texts (cost, accommodation, courses...).
        """
        ensure_data()
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not 2 <= len(ids) <= 6:
            raise ValueError("Provide between 2 and 6 distinct destination ids.")
        found = db.get_destinations(path, ids)
        missing = [i for i in ids if i not in {d["id"] for d in found}]
        items = []
        for d in found:
            d.pop("full_text", None)
            d.pop("images", None)
            d.pop("scraped_at", None)
            items.append(d)
        fields = ["country", "language", "diploma", "spots", "gpa_requirement", "extra_charge_cents",
                  "erasmus", "semester", "specializations", "suspended"]
        diffs = [f for f in fields if len({json.dumps(d.get(f), sort_keys=True) for d in items}) > 1]
        out = {"destinations": items, "differences": diffs}
        if missing:
            out["missing_ids"] = missing
        return _json(out)

    @mcp.tool()
    def get_data_status() -> str:
        """Report data freshness: last successful scrape, destination count, last error, recent changes.

        recent_changes lists additions, removals and field modifications detected between scrapes
        (field values are shortened; full_text changes only record that the text changed).
        """
        meta = db.get_meta(path)
        ts = meta.get("last_success_at")
        age = None
        if ts:
            from datetime import datetime, timezone
            age = round((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds())
        return _json({
            "last_success_at": ts, "data_age_seconds": age,
            "destinations": db.count_destinations(path),
            "last_attempt_at": meta.get("last_attempt_at"), "last_error": meta.get("last_error") or None,
            "refresh_hour_utc": settings.refresh_hour_utc, "source": settings.site_base,
            "recent_changes": db.recent_changes(path, 20) if ts else [],
        })

    @mcp.resource("mya://about")
    def about() -> str:
        """What this server exposes."""
        return ("Epitech MYA partner universities: use list_filters, list_destinations, "
                "search_destinations, get_destination, compare_destinations, get_data_status.")

    return mcp


def build_app(settings: Settings | None = None) -> Starlette:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # une ligne par requête serait trop bavard
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    provider = None
    if settings.oauth_enabled:
        provider = MyaOAuthProvider(settings.oauth_db_path, settings.public_url, settings.password)
    else:
        log.warning("ALLOW_NO_AUTH=1 : serveur SANS authentification (usage local uniquement)")
    refresher = Refresher(settings)
    mcp = build_mcp(settings, provider)
    if provider is not None:
        # pages de consentement : publiques (le mot de passe est demandé sur la page)
        mcp.custom_route("/consent", methods=["GET"])(provider.consent_get)
        mcp.custom_route("/consent", methods=["POST"])(provider.consent_post)
    app = mcp.streamable_http_app()

    async def health(_request):
        meta = db.get_meta(settings.db_path)
        age = refresher.data_age_seconds()
        n = db.count_destinations(settings.db_path)
        status = "no_data" if n == 0 else ("stale" if refresher.is_stale() else "ok")
        # Toujours 200 tant que le process vit : une source MYA en panne ne doit pas redémarrer le conteneur
        return JSONResponse({
            "status": status, "version": __version__, "destinations": n,
            "data_age_seconds": None if age is None else round(age),
            "last_success_at": meta.get("last_success_at"),
        })

    app.add_route("/health", health, methods=["GET"])

    if provider is not None:
        async def resource_metadata(_request):
            # alias à la racine (RFC 9728) : certains clients sondent ce chemin avant celui avec /mcp
            return JSONResponse({"resource": settings.resource_url,
                                 "authorization_servers": [settings.public_url],
                                 "bearer_methods_supported": ["header"]})

        app.add_route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"])

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        task = asyncio.create_task(refresher.run_forever(), name="mya-refresh")
        try:
            async with original(a):
                yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    app = build_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(),
                proxy_headers=True, forwarded_allow_ips=settings.forwarded_allow_ips)


if __name__ == "__main__":
    main()
