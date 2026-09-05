"""Foreign exchange, to Singapore dollars, with its date attached.

Two rules, both about honesty rather than accuracy:

* **A rate always travels with its source and its date.** A converted figure
  with no "as of" is indistinguishable from a made-up one six months later.
* **Failure returns ``None``, never a fallback rate.** The previous design
  sketched a static table; a hardcoded number that looks authoritative and
  drifts silently is worse than an unconverted figure clearly labelled in its
  original currency.

Rates are cached for a day, which is the resolution the source itself
publishes at.
"""

from __future__ import annotations

from dataclasses import dataclass

from services import cache

BASE = "SGD"
ENDPOINT = "https://api.frankfurter.app/latest"
SOURCE_NAME = "Frankfurter (European Central Bank reference rates)"
TIMEOUT_SECONDS = 15.0
FX_TTL = cache.DAY


@dataclass(frozen=True)
class Rate:
    """One exchange rate, with the provenance a reader needs to trust it."""

    currency: str
    to_currency: str
    rate: float
    as_of: str
    source: str = SOURCE_NAME

    def convert(self, amount: float) -> float:
        return round(amount * self.rate, 2)

    @property
    def label(self) -> str:
        return f"1 {self.currency} = {self.rate:g} {self.to_currency} ({self.source}, {self.as_of})"


def _fetch(currency: str) -> dict | None:
    import httpx

    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(ENDPOINT, params={"base": currency, "symbols": BASE})
            if response.status_code != 200:
                return None
            payload = response.json()
    except Exception:  # noqa: BLE001 - an unreachable rate service is a normal state
        return None
    rate = (payload.get("rates") or {}).get(BASE)
    if not isinstance(rate, (int, float)) or rate <= 0:
        return None
    return {"rate": float(rate), "as_of": str(payload.get("date") or "")}


def rate_to_sgd(currency: str, db_path=None) -> Rate | None:
    """Today's rate from ``currency`` into SGD, or ``None`` if unavailable."""
    code = (currency or "").strip().upper()
    if len(code) != 3:
        return None
    if code == BASE:
        return Rate(currency=BASE, to_currency=BASE, rate=1.0, as_of="", source="identity")

    key = cache.make_key("fx", code, BASE)
    hit = cache.get(key, db_path=db_path)
    if not hit:
        hit = _fetch(code)
        if hit is None:
            return None
        cache.set(key, hit, namespace="fx", ttl_seconds=FX_TTL, db_path=db_path)
    try:
        return Rate(
            currency=code, to_currency=BASE, rate=float(hit["rate"]), as_of=str(hit.get("as_of", ""))
        )
    except (KeyError, TypeError, ValueError):
        return None


def to_sgd(amount: float, currency: str, db_path=None) -> tuple[float | None, Rate | None]:
    """Convert an amount into SGD. ``(None, None)`` when no rate is available."""
    rate = rate_to_sgd(currency, db_path=db_path)
    if rate is None:
        return None, None
    return rate.convert(amount), rate
