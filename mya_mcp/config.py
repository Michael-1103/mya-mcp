"""Configuration lue depuis les variables d'environnement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"Variable d'environnement {name} invalide : {raw!r} (entier attendu)") from exc
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise SystemExit(f"Variable d'environnement {name}={value} hors limites [{lo}, {hi}]")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    password: str
    public_url: str
    allow_no_auth: bool
    forwarded_allow_ips: str
    refresh_hour_utc: int
    max_age_hours: int
    api_base: str
    site_base: str
    concurrency: int
    max_drop_ratio: float
    max_failed_ratio: float
    retry_delay_minutes: int
    log_level: str

    @property
    def oauth_db_path(self) -> Path:
        return self.data_dir / "oauth.db"

    @property
    def oauth_enabled(self) -> bool:
        return not self.allow_no_auth

    @property
    def resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mya.db"


def load_settings() -> Settings:
    """Charge et valide la configuration. Quitte avec un message clair si invalide."""
    password = os.environ.get("MCP_PASSWORD", "")
    public_url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    allow_no_auth = _bool("ALLOW_NO_AUTH")
    if not allow_no_auth:
        if len(password) < 12:
            raise SystemExit(
                "MCP_PASSWORD est obligatoire (12 caractères minimum, par exemple openssl rand -hex 32). "
                "C'est le mot de passe de la page de connexion OAuth. "
                "ALLOW_NO_AUTH=1 permet de démarrer sans auth, pour un test local uniquement."
            )
        parsed = urlparse(public_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment \
                or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            raise SystemExit(
                "PUBLIC_URL est obligatoire : URL publique du serveur, en https, sans chemin "
                "(ex. https://mya-mcp.exemple.org). http n'est accepté que pour localhost."
            )
    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_int("PORT", 8000, 1, 65535),
        password=password,
        public_url=public_url,
        allow_no_auth=allow_no_auth,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*").strip() or "*",
        refresh_hour_utc=_int("REFRESH_HOUR_UTC", 3, 0, 23),
        max_age_hours=_int("MAX_AGE_HOURS", 24, 1),
        api_base=os.environ.get("MYA_API_BASE", "https://mya.epitech.eu/api").rstrip("/"),
        site_base=os.environ.get("MYA_SITE_BASE", "https://mya.epitech.eu").rstrip("/"),
        concurrency=_int("SCRAPE_CONCURRENCY", 8, 1, 32),
        max_drop_ratio=_int("MAX_DROP_PERCENT", 30, 1, 100) / 100,
        max_failed_ratio=_int("MAX_FAILED_PERCENT", 5, 0, 100) / 100,
        retry_delay_minutes=_int("RETRY_DELAY_MINUTES", 30, 1),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
