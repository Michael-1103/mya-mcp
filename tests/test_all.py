"""Tests hors réseau : scraper, base, recherche, refresh en échec, authentification."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mya_mcp import db, scheduler
from mya_mcp.config import Settings
from mya_mcp.scraper import ScrapeError, ScrapeResult, html_to_text, parse_university
from mya_mcp.server import build_app


def make_dest(i: int, **kw) -> dict:
    d = {
        "id": i, "url": f"https://x/university/{i}", "university_name": f"Université {i}",
        "country": "Pologne" if i % 2 else "Canada", "language": "English", "diploma": "NO",
        "spots": 3, "gpa_requirement": 0, "extra_charge_cents": 0, "erasmus": "YES",
        "semester": "Full-year only", "specializations": ["Security", "Réseaux"], "display": "YES",
        "suspended": "NO", "updated_at": "2026-01-01", "images": [],
        "full_text": f"### Overview\nÉtudiants et logement numéro {i}. Cours de cryptographie.",
    }
    d.update(kw)
    return d


def settings(tmp: Path, **kw) -> Settings:
    base = dict(
        data_dir=tmp, host="127.0.0.1", port=0, auth_token="secret", allow_no_auth=False,
        refresh_hour_utc=3, max_age_hours=24, api_base="http://invalid", site_base="http://invalid",
        concurrency=2, max_drop_ratio=0.3, max_failed_ratio=0.05, retry_delay_minutes=30, log_level="INFO")
    base.update(kw)
    return Settings(**base)


def fake_scrape(monkeypatch, dests, failed=()):
    async def _f(*a, **k):
        return ScrapeResult(destinations=list(dests), failed_ids=list(failed),
                            listing_count=len(dests) + len(failed), scraped_at=db.now_iso())
    monkeypatch.setattr(scheduler, "fetch_all_universities", _f)


def test_html_to_text_and_parse():
    assert html_to_text("<p>Salut <b>toi</b></p><ul><li>a</li><li>b</li></ul>") == "Salut toi\n\n- a\n- b"
    assert html_to_text(None) == ""
    d = parse_university({"id": 7, "name": " Foo ", "gpa": 2.5, "overview": "<p>Hi</p>",
                          "accomodation": "<p>Room</p>", "image1": "a.webp"}, "https://s")
    assert d["url"] == "https://s/university/7" and d["university_name"] == "Foo"
    assert "### Overview\nHi" in d["full_text"] and "Room" in d["full_text"]
    with pytest.raises(ValueError):
        parse_university({"name": "sans id"}, "https://s")


def test_search_filters_and_sanitizing(tmp_path):
    p = tmp_path / "mya.db"
    db.build_and_swap(p, [make_dest(i) for i in range(1, 7)], db.now_iso())
    assert db.search_destinations(p, "etudiant", 5)["count"] == 5  # accents ignorés
    assert db.search_destinations(p, "cours", 5)["count"] > 0  # pluriel / préfixe
    assert db.search_destinations(p, "universites", 5)["count"] > 0
    assert db.search_destinations(p, 'foo" OR NEAR( * ) -- ;', 5)["mode"] in {"like", "fts_or", "fts_and"}
    assert db.search_destinations(p, '"""', 5)["count"] == 0
    r = db.search_destinations(p, "logement", 10, country="pologne")
    assert r["count"] == 3 and all(x["country"] == "Pologne" for x in r["results"])
    assert db.list_destinations(p, 10, 0, specialization="reseaux")["total"] == 6
    assert db.list_destinations(p, 10, 0, max_extra_charge_eur=0, max_gpa=0)["total"] == 6
    assert db.list_destinations(p, 2, 4)["count"] == 2


def test_changes_history(tmp_path):
    p = tmp_path / "mya.db"
    db.build_and_swap(p, [make_dest(i) for i in range(1, 5)], "2026-01-01T00:00:00+00:00")
    assert db.recent_changes(p) == []  # pas de bruit au premier chargement
    new = [make_dest(1), make_dest(2, spots=9), make_dest(3), make_dest(5)]  # 4 supprimé, 5 ajouté
    rep = db.build_and_swap(p, new, "2026-01-02T00:00:00+00:00")
    assert (rep["added"], rep["removed"], rep["modified"]) == (1, 1, 1)
    kinds = {(c["change_type"], c["dest_id"], c.get("field")) for c in db.recent_changes(p)}
    assert kinds == {("added", 5, None), ("removed", 4, None), ("modified", 2, "spots")}


def test_refresh_failures_keep_old_db(tmp_path, monkeypatch):
    s = settings(tmp_path)
    r = scheduler.Refresher(s)
    good = [make_dest(i) for i in range(1, 41)]
    fake_scrape(monkeypatch, good)
    assert asyncio.run(r.refresh())["ok"]
    assert db.count_destinations(s.db_path) == 40

    # 0 destination
    fake_scrape(monkeypatch, [])
    assert not asyncio.run(r.refresh())["ok"]
    # chute anormale (40 vers 10)
    fake_scrape(monkeypatch, good[:10])
    assert not asyncio.run(r.refresh())["ok"]
    # trop de fiches en échec
    fake_scrape(monkeypatch, good[:30], failed=range(100, 110))
    assert not asyncio.run(r.refresh())["ok"]

    # exception réseau
    async def boom(*a, **k):
        raise ScrapeError("API down")
    monkeypatch.setattr(scheduler, "fetch_all_universities", boom)
    assert not asyncio.run(r.refresh())["ok"]

    assert db.count_destinations(s.db_path) == 40  # ancienne base intacte
    meta = db.get_meta(s.db_path)
    assert "API down" in meta["last_error"] and meta["last_success_at"]
    assert not list(tmp_path.glob("*.new*"))

    # un échec isolé reprend l'ancienne fiche
    fake_scrape(monkeypatch, [d for d in good if d["id"] != 7], failed=[7])
    assert asyncio.run(r.refresh())["ok"] and db.count_destinations(s.db_path) == 40


def test_auth_and_health(tmp_path):
    app = build_app(settings(tmp_path))
    c = TestClient(app)  # sans lifespan : pas de scrape réseau
    assert c.get("/health").status_code == 200
    assert c.get("/health").json()["status"] == "no_data"
    assert c.post("/mcp", json={}).status_code == 401
    assert c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/mcp", json={}, headers={"Authorization": "Basic secret"}).status_code == 401
