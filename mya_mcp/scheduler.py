"""Refresh des données : validation, remplacement atomique et planification quotidienne."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from . import db
from .config import Settings
from .scraper import ScrapeError, fetch_all_universities

log = logging.getLogger("mya.scheduler")


class Refresher:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._lock = asyncio.Lock()

    def data_age_seconds(self) -> float | None:
        ts = db.get_meta(self.s.db_path).get("last_success_at")
        if not ts:
            return None
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()

    def is_stale(self) -> bool:
        age = self.data_age_seconds()
        return age is None or age > self.s.max_age_hours * 3600

    async def refresh(self) -> dict:
        """Un cycle de scrape complet. Ne touche à la base actuelle qu'en cas de succès validé."""
        async with self._lock:
            path = self.s.db_path
            try:
                res = await fetch_all_universities(self.s.api_base, self.s.site_base, self.s.concurrency)
                dests = res.destinations
                if not dests:
                    raise ScrapeError("Le scrape a renvoyé 0 destination, base conservée")
                if res.listing_count and len(res.failed_ids) > self.s.max_failed_ratio * res.listing_count:
                    raise ScrapeError(
                        f"{len(res.failed_ids)}/{res.listing_count} fiches en échec "
                        f"(seuil {self.s.max_failed_ratio:.0%}), base conservée")
                previous = await asyncio.to_thread(db.read_all, path)
                if res.failed_ids:
                    # reprise des anciennes fiches pour les rares échecs, sinon elles sont omises
                    for uid in res.failed_ids:
                        old = previous.get(uid)
                        if old:
                            old = dict(old)
                            old["specializations"] = json.loads(old.get("specializations") or "[]")
                            old["images"] = json.loads(old.get("images") or "[]")
                            dests.append(old)
                            log.warning("Fiche %s : ancienne version conservée", uid)
                        else:
                            log.warning("Fiche %s : échec et pas d'ancienne version, omise", uid)
                    dests.sort(key=lambda d: d["id"])
                if len(previous) >= 20 and len(dests) < len(previous) * (1 - self.s.max_drop_ratio):
                    raise ScrapeError(
                        f"Chute anormale ({len(previous)} vers {len(dests)} destinations, "
                        f"seuil {self.s.max_drop_ratio:.0%}), base conservée")
                report = await asyncio.to_thread(db.build_and_swap, path, dests, res.scraped_at)
                report["failed_ids"] = res.failed_ids
                log.info("Refresh OK : %s", report)
                return {"ok": True, **report}
            except Exception as exc:  # noqa: BLE001 - toute erreur laisse l'ancienne base en place
                log.error("Refresh en échec, ancienne base conservée : %r", exc)
                await asyncio.to_thread(db.record_attempt_failure, path, repr(exc))
                return {"ok": False, "error": repr(exc)}

    def _next_daily(self, now: datetime) -> datetime:
        target = now.replace(hour=self.s.refresh_hour_utc, minute=0, second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)

    async def run_forever(self) -> None:
        """Refresh au démarrage si nécessaire, puis chaque jour à REFRESH_HOUR_UTC.

        Après un échec, nouvel essai après RETRY_DELAY_MINUTES.
        """
        try:
            if self.is_stale():
                log.info("Données absentes ou trop anciennes, refresh au démarrage")
                result = await self.refresh()
            else:
                result = {"ok": True}
            while True:
                now = datetime.now(timezone.utc)
                nxt = self._next_daily(now)
                if not result["ok"]:
                    nxt = min(nxt, now + timedelta(minutes=self.s.retry_delay_minutes))
                log.info("Prochain refresh à %s", nxt.isoformat(timespec="seconds"))
                await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
                result = await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - filet de sécurité
            log.exception("La boucle de refresh s'est arrêtée de façon inattendue")
            raise
