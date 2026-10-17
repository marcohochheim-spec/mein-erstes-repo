"""tastytrade REST authentication: OAuth refresh token -> access token -> DXLink quote token.

Credentials are read from the environment only (never written anywhere):
    TASTYTRADE_CLIENT_ID, TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN
Optional:
    TASTYTRADE_ENV = production (default) | sandbox
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

API_URLS = {
    "production": "https://api.tastyworks.com",
    "sandbox": "https://api.cert.tastyworks.com",
}
USER_AGENT = "strategy-tests/0.1"


class AuthError(RuntimeError):
    pass


@dataclass
class QuoteToken:
    token: str
    dxlink_url: str
    level: str


def _api_url() -> str:
    env = os.environ.get("TASTYTRADE_ENV", "production").strip().lower()
    if env in ("prod", "live"):
        env = "production"
    if env not in API_URLS:
        raise AuthError(f"Unbekanntes TASTYTRADE_ENV={env!r} (erlaubt: production, sandbox)")
    return API_URLS[env]


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuthError(f"Umgebungsvariable {name} ist nicht gesetzt")
    return value


def get_access_token() -> str:
    """Exchange the long-lived refresh token for a short-lived access token."""
    resp = requests.post(
        f"{_api_url()}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "refresh_token": _require("TASTYTRADE_REFRESH_TOKEN"),
            "client_id": _require("TASTYTRADE_CLIENT_ID"),
            "client_secret": _require("TASTYTRADE_CLIENT_SECRET"),
        },
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        # Never echo the request body: it contains the secrets.
        raise AuthError(f"OAuth-Token-Abruf fehlgeschlagen: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()["access_token"]


def get_quote_token(access_token: str | None = None) -> QuoteToken:
    """Fetch the DXLink streamer token (valid ~24h)."""
    access_token = access_token or get_access_token()
    resp = requests.get(
        f"{_api_url()}/api-quote-tokens",
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise AuthError(f"Quote-Token-Abruf fehlgeschlagen: HTTP {resp.status_code} {resp.text[:300]}")
    data = resp.json()["data"]
    return QuoteToken(token=data["token"], dxlink_url=data["dxlink-url"], level=data.get("level", "?"))
