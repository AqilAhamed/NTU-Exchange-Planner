"""Currency conversion to Singapore dollars.

Deliberately refuses rather than guesses. Live rates arrive with the finance
lane; until then this agent returns a structured "not available" and names what
it would need. Shipping a hardcoded rate table would mean printing a number
that looks authoritative, is undated, and drifts silently — precisely the class
of defect this system is built to avoid.
"""

from __future__ import annotations

import re

from graph.domain import Conversion
from graph.state import LaneResult

LANE = "conversion"
TARGET = "SGD"

_AMOUNT = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\s*([A-Z]{3}|euros?|dollars?|pounds?|yen|won|kroner|krona)\b",
    re.IGNORECASE,
)

_CURRENCY_WORDS = {
    "euro": "EUR", "euros": "EUR",
    "pound": "GBP", "pounds": "GBP",
    "yen": "JPY",
    "won": "KRW",
    "kroner": "DKK", "krona": "SEK",
    "dollar": "USD", "dollars": "USD",
}


def parse_request(message: str) -> tuple[float, str] | None:
    """Read ``(amount, currency)`` out of the student's message."""
    match = _AMOUNT.search(message or "")
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        amount *= 1_000
    elif suffix == "m":
        amount *= 1_000_000
    raw = match.group(3).lower()
    currency = _CURRENCY_WORDS.get(raw, raw.upper())
    return (amount, currency) if len(currency) == 3 else None


def run(message: str) -> dict:
    """Report what conversion was asked for, and that rates are not yet wired."""
    request = parse_request(message)
    if request is None:
        return {
            "lane_results": {
                LANE: LaneResult(lane=LANE, status="skipped", note="no currency amount found")
            }
        }

    amount, currency = request
    if currency == TARGET:
        return {
            "conversion_results": [
                Conversion(
                    kind="currency",
                    summary=f"{amount:,.2f} SGD is already in Singapore dollars.",
                    details={"amount": amount, "from": currency, "to": TARGET, "supported": True},
                )
            ],
            "lane_results": {LANE: LaneResult(lane=LANE, status="complete", note="identity")},
        }

    return {
        "conversion_results": [
            Conversion(
                kind="currency",
                summary=(
                    f"I can't convert {amount:,.2f} {currency} to SGD right now — this build "
                    "has no live exchange-rate source, and I won't quote a stale rate as if it "
                    "were today's."
                ),
                details={
                    "amount": amount,
                    "from": currency,
                    "to": TARGET,
                    "supported": False,
                    "reason": "no_fx_provider",
                },
            )
        ],
        "caveats": [
            f"Exchange rates are not available in this build, so {currency} amounts are shown "
            "unconverted, in their original currency."
        ],
        "lane_results": {
            LANE: LaneResult(
                lane=LANE, status="complete", note=f"{currency} to SGD unavailable (no FX provider)"
            )
        },
    }
