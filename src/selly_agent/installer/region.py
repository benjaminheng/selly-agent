"""Guessing where the seller sells, so setup can confirm rather than interrogate.

Region is the first thing the agent needs and the least interesting thing to ask for: it decides
which regional marketplace sites exist for this seller and which currency their prices are in.
The machine already knows its timezone, so setup proposes an answer and asks for a yes.

A guess is only ever a default. Everything here is pure and every answer is overridable, because
a wrong guess accepted in silence is worse than a question: it would price in the wrong currency
and compose URLs for a site the seller does not use.
"""

from __future__ import annotations

import os

# Currency per region we can name one for. Wrong currency means wrong prices, so an unlisted
# region is asked about rather than defaulted.
CURRENCIES = {
    "SG": "SGD",
    "MY": "MYR",
    "HK": "HKD",
    "TW": "TWD",
    "PH": "PHP",
    "ID": "IDR",
    "AU": "AUD",
    "US": "USD",
    "CA": "CAD",
    "GB": "GBP",
    "NZ": "NZD",
    "JP": "JPY",
}

# IANA zone → region, for the zones our marketplaces actually serve. Exact names first; anything
# else falls back to the continent-level prefixes below.
_ZONE_REGIONS = {
    "Asia/Singapore": "SG",
    "Asia/Kuala_Lumpur": "MY",
    "Asia/Kuching": "MY",
    "Asia/Hong_Kong": "HK",
    "Asia/Taipei": "TW",
    "Asia/Manila": "PH",
    "Asia/Jakarta": "ID",
    "Asia/Pontianak": "ID",
    "Asia/Makassar": "ID",
    "Asia/Jayapura": "ID",
    "Asia/Tokyo": "JP",
    "Pacific/Auckland": "NZ",
}

_ZONE_PREFIX_REGIONS = (
    ("Australia/", "AU"),
    ("America/Toronto", "CA"),
    ("America/Vancouver", "CA"),
    ("America/Edmonton", "CA"),
    ("America/Winnipeg", "CA"),
    ("America/Halifax", "CA"),
    ("Europe/London", "GB"),
    ("America/", "US"),
    ("US/", "US"),
)


def region_for_zone(zone: str):
    """The region a timezone implies, or None when it implies nothing we can name."""
    if not zone:
        return None
    if zone in _ZONE_REGIONS:
        return _ZONE_REGIONS[zone]
    for prefix, region in _ZONE_PREFIX_REGIONS:
        if zone.startswith(prefix):
            return region
    return None


def system_timezone() -> str:
    """The machine's IANA zone name, or "" when it cannot be read.

    Read from where /etc/localtime points rather than from `time.tzname`, which gives an
    abbreviation ("+08") that names no zone and cannot be stored or looked up.
    """
    try:
        resolved = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    marker = "/zoneinfo/"
    index = resolved.find(marker)
    return resolved[index + len(marker) :] if index >= 0 else ""


def guess(zone: str | None = None):
    """A complete {region, currency, timezone} proposal, or None when the machine gives no hint."""
    zone = system_timezone() if zone is None else zone
    region = region_for_zone(zone)
    if region is None:
        return None
    currency = CURRENCIES.get(region)
    if currency is None:
        return None
    return {"region": region, "currency": currency, "timezone": zone}


def render(basics: dict) -> str:
    """How a proposal is put to the seller: `SG · SGD · Asia/Singapore`."""
    return " · ".join(
        str(basics.get(key, "")) for key in ("region", "currency", "timezone") if basics.get(key)
    )
