"""The shipped marketplace registry: region→host resolution and the pruned-stub guard."""

from __future__ import annotations

import re

from selly_agent import marketplaces
from selly_agent.browser import markets as market_adapters
from selly_agent.browser.markets.craigslist import LISTING_ID_PATTERN
from selly_agent.engines import hosts


def test_resolve_regional_host_exact() -> None:
    assert marketplaces.resolve_domain("carousell", "SG") == "www.carousell.sg"
    assert marketplaces.resolve_domain("carousell", "MY") == "www.carousell.com.my"


def test_resolve_falls_back_to_star_default() -> None:
    # fb has only a "*" domain; ebay has regional hosts plus a "*" default for unknown regions
    assert marketplaces.resolve_domain("fb", "SG") == "www.facebook.com"
    assert marketplaces.resolve_domain("ebay", "ZZ") == "www.ebay.com"
    assert marketplaces.resolve_domain("fb", None) == "www.facebook.com"


def test_resolve_falls_back_to_listing_url_host() -> None:
    # craigslist has no domains map and a real host, so the listing_url host is the answer
    assert marketplaces.resolve_domain("craigslist", "US") == "craigslist.org"


def test_resolve_unknown_market_is_none() -> None:
    assert marketplaces.resolve_domain("nope", "SG") is None


def test_display_name_known_and_fallback() -> None:
    assert marketplaces.display_name("carousell-ai") == "Carousell.ai"
    assert marketplaces.display_name("nope") == "nope"  # fail-open to the id


def test_carousell_ai_entry_shape() -> None:
    entry = marketplaces.get_marketplace("carousell-ai")
    assert entry["listing_url"]["host"] == "www.carousell.ai"
    assert entry["connector"]["type"] == "mcp"


def test_recipe_less_stubs_are_pruned() -> None:
    ids = {e["id"] for e in marketplaces.all_marketplaces()}
    assert {"depop", "thredup", "nextdoor"}.isdisjoint(ids)
    # the kept first-port markets are present
    assert {"fb", "carousell", "carousell-ai"}.issubset(ids)


def test_registry_carries_no_unread_fields() -> None:
    """The registry is the data no code can derive: hosts, URL templates, display names. A field
    nothing reads is a fact free to drift, so it does not live here."""
    unread = {"regions", "categories", "fulfillment", "default_enabled"}
    for entry in marketplaces.all_marketplaces():
        assert unread.isdisjoint(entry), entry["id"]
        assert set(entry.get("connector") or {}) <= {"type"}, entry["id"]


def test_allowlist_covers_markets_without_adapters() -> None:
    """Why the registry cannot shrink to the markets we drive: entries with no adapter still
    contribute the hosts that keep the scam scanner from flagging legitimate marketplace links."""
    allowlist = hosts.build_allowlist(marketplaces.all_marketplaces())
    assert {"ebay.com", "mercari.com", "poshmark.com"} <= allowlist


def test_supported_markets_is_the_adapter_registry() -> None:
    """Carousell and Craigslist today — every other browser entry is a host the scanner needs, not
    a market anything can drive."""
    assert market_adapters.supported_markets() == ["carousell", "craigslist"]


def test_supported_market_needs_both_an_adapter_and_a_recipe(monkeypatch) -> None:
    monkeypatch.setattr(marketplaces, "listing_flow", lambda market: "")
    assert market_adapters.supported_markets() == []

    monkeypatch.undo()
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {})
    assert market_adapters.supported_markets() == []


def test_publishable_markets_follow_the_seller_region() -> None:
    """Carousell runs no US site, so a US seller has nowhere to be listed there. Craigslist has no
    domains map at all (see test_resolve_falls_back_to_listing_url_host), so it resolves for every
    region alike -- including no region at all."""
    assert market_adapters.publishable_markets("SG") == ["carousell", "craigslist"]
    assert market_adapters.publishable_markets("US") == ["craigslist"]
    assert market_adapters.publishable_markets(None) == ["craigslist"]


# --- region resolution: a domains map is exhaustive --------------------------------------------


def test_a_region_absent_from_the_map_has_no_site() -> None:
    assert marketplaces.resolve_domain("carousell", "US") is None
    assert marketplaces.resolve_domain("carousell", None) is None


def test_carousell_ai_serves_us_and_sg_only() -> None:
    assert marketplaces.resolve_domain("carousell-ai", "US") == "www.carousell.ai"
    assert marketplaces.resolve_domain("carousell-ai", "SG") == "www.carousell.ai"
    assert marketplaces.resolve_domain("carousell-ai", "MY") is None


def test_no_entry_ever_resolves_to_a_bare_host_suffix() -> None:
    """A suffix like "carousell." is the verifier's host pattern. Handed out as a site it composes
    URLs that cannot resolve and region checks that compare against nonsense."""
    for entry in marketplaces.all_marketplaces():
        for region in ("SG", "US", "MY", "ZZ", None):
            host = marketplaces.resolve_domain(entry["id"], region)
            assert host is None or not host.endswith("."), (entry["id"], region, host)


# --- craigslist: registry entry -----------------------------------------------------------------


def test_craigslist_registry_entry_is_complete() -> None:
    assert marketplaces.listing_flow("craigslist") == "listing-flow-craigslist"
    assert marketplaces.market_url("craigslist", "sell", "US") == "https://craigslist.org/"
    assert marketplaces.market_url("craigslist", "sell", None) == "https://craigslist.org/"


def test_craigslist_has_no_recorded_inbox_url() -> None:
    """Craigslist has no in-page buyer inbox, so the read lane must never be pointed at one —
    covered end to end in test_browser_inbox.py."""
    assert marketplaces.urls("craigslist").get("inbox") is None


def test_craigslist_adapter_is_listing_only() -> None:
    """The inbox-related artifacts still exist to satisfy MarketAdapter's required shape, but
    there is never a composer or a submit mechanism because there is never a chat to reply in."""
    assert market_adapters.CRAIGSLIST.chat_message_submit_js == ""
    assert market_adapters.CRAIGSLIST.composer == ()
    assert market_adapters.CRAIGSLIST.system_handles == frozenset()


def test_craigslist_listing_id_pattern_matches_a_real_shaped_permalink() -> None:
    url = "https://sfbay.craigslist.org/sfc/mob/d/san-francisco-iphone-13/7654321098.html"
    match = re.search(LISTING_ID_PATTERN, url)
    assert match is not None
    assert match.group(1) == "7654321098"


def test_craigslist_listing_id_pattern_rejects_a_non_listing_page() -> None:
    assert re.search(LISTING_ID_PATTERN, "https://sfbay.craigslist.org/search/sss") is None
