"""Récupération des destinations depuis l'API JSON publique de MYA.

Adapté du scraper du projet MYA-Chatbot (conversion HTML vers texte et
normalisation des champs), avec en plus des retries à backoff exponentiel et
des erreurs explicites (aucun échec silencieux).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

log = logging.getLogger("mya.scraper")

HEADERS_BASE = {
    "User-Agent": "mya-mcp/1.0 (+serveur MCP personnel, lecture seule de l'API publique)",
    "Accept": "application/json",
}
TIMEOUT_S = 30
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 1.0
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ScrapeError(RuntimeError):
    """Échec du scrape (réseau, format inattendu...)."""


# Conversion HTML (Quill) vers texte brut

_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4"}


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.chunks.append("\n")
        elif tag == "li":
            self.chunks.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.chunks.append(f" ({self._href})")
            self._href = None
        elif tag in _BLOCK_TAGS and tag != "li":  # li : le saut de ligne est émis à l'ouverture
            self.chunks.append("\n")

    def handle_data(self, data):
        self.chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self.chunks)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
        return text.strip()


def html_to_text(value) -> str:
    if not value or not isinstance(value, str):
        return ""
    parser = _HTMLToText()
    parser.feed(value)
    parser.close()
    return parser.get_text()


# Normalisation d'une fiche

_TEXT_FIELDS = [
    ("overview", "Overview"),
    ("administrative", "Administrative (application, visa, insurance)"),
    ("accomodation", "Accommodation"),  # faute de l'API conservée dans la clé
    ("courses", "Courses and academic calendar"),
    ("cost", "Cost of living / budget"),
]


def parse_university(raw: dict, site_base: str) -> dict:
    """Transforme la réponse brute de l'API en fiche normalisée."""
    uid = raw.get("id")
    if not isinstance(uid, int):
        raise ValueError(f"id manquant ou invalide dans la fiche : {uid!r}")
    parts = []
    for key, label in _TEXT_FIELDS:
        text = html_to_text(raw.get(key))
        if text:
            parts.append(f"### {label}\n{text}")
    specs = raw.get("specializations") or []
    if not isinstance(specs, list):
        specs = []
    return {
        "id": uid,
        "url": f"{site_base}/university/{uid}",
        "university_name": (raw.get("name") or "").strip(),
        "country": (raw.get("country") or "").strip() or None,
        "language": (raw.get("language") or "").strip() or None,
        "diploma": raw.get("diploma"),
        "spots": raw.get("spots"),
        "gpa_requirement": raw.get("gpa"),
        "extra_charge_cents": raw.get("extracharge"),
        "erasmus": raw.get("erasmus"),
        "semester": raw.get("semester"),
        "specializations": [str(s).strip() for s in specs if str(s).strip()],
        "display": raw.get("display"),
        "suspended": raw.get("suspended"),
        "updated_at": raw.get("updatedAt"),
        "images": [i for i in (raw.get("image1"), raw.get("image2"), raw.get("image3")) if i],
        "full_text": "\n\n".join(parts),
    }


# Appels HTTP avec retries


async def _get_json(client: httpx.AsyncClient, url: str):
    """GET JSON avec retries (backoff exponentiel + jitter). Lève ScrapeError."""
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await client.get(url)
            if resp.status_code in RETRY_STATUS:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
            last = exc
            retriable = not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in RETRY_STATUS)
            if not retriable or attempt == MAX_ATTEMPTS:
                break
            delay = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning("GET %s a échoué (%s), tentative %d/%d, nouvel essai dans %.1fs",
                        url, exc, attempt, MAX_ATTEMPTS, delay)
            await asyncio.sleep(delay)
    raise ScrapeError(f"GET {url} : échec après {MAX_ATTEMPTS} tentatives max ({last!r})")


@dataclass
class ScrapeResult:
    destinations: list[dict] = field(default_factory=list)
    failed_ids: list[int] = field(default_factory=list)
    listing_count: int = 0
    scraped_at: str = ""


async def fetch_all_universities(api_base: str, site_base: str, concurrency: int = 8) -> ScrapeResult:
    """Récupère la liste puis le détail de chaque université.

    Lève ScrapeError si la liste est inaccessible ou invalide. Les fiches
    détaillées en échec (après retries) sont listées dans failed_ids, à
    l'appelant de décider du seuil acceptable.
    """
    headers = {**HEADERS_BASE, "Origin": site_base, "Referer": f"{site_base}/programs"}
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT_S, follow_redirects=True) as client:
        listing = await _get_json(client, f"{api_base}/universities")
        if not isinstance(listing, list):
            raise ScrapeError(f"Liste des universités : format inattendu ({type(listing).__name__})")
        ids = [it["id"] for it in listing if isinstance(it, dict) and isinstance(it.get("id"), int)]
        if len(ids) != len(listing):
            log.warning("%d entrées de la liste sans id valide ignorées", len(listing) - len(ids))
        log.info("%d universités listées, récupération des fiches (concurrence %d)", len(ids), concurrency)

        sem = asyncio.Semaphore(concurrency)
        result = ScrapeResult(listing_count=len(ids), scraped_at=datetime.now(timezone.utc).isoformat())

        async def one(uid: int) -> None:
            async with sem:
                try:
                    raw = await _get_json(client, f"{api_base}/universities/{uid}")
                    result.destinations.append(parse_university(raw, site_base))
                except Exception as exc:  # noqa: BLE001 - on journalise et on compte l'échec
                    log.error("Fiche %s en échec : %r", uid, exc)
                    result.failed_ids.append(uid)

        await asyncio.gather(*(one(i) for i in ids))
    result.destinations.sort(key=lambda d: d["id"])
    return result
