"""The fan-out lane: which items it queues a publish for, what it refuses to try twice, when it
holds off, and how each outcome reaches the seller.

Chrome and the Playwright command are stubbed at the module boundary — these tests are about the
lane's decisions, and the bring-up itself is covered in test_browser_client.py.
"""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from selly_agent import crosslist, settings
from selly_agent.browser import chrome
from selly_agent.browser.client import BrowserUnavailable
from selly_agent.config import Config

_RAIL_URL = "https://www.carousell.ai/listing/abc123"
_CAROUSELL_URL = "https://www.carousell.sg/p/teak-lamp-1328307791/"


@pytest.fixture(autouse=True)
def _browser_is_available(monkeypatch):
    """The default posture for these tests: Node present, Chrome already up."""
    monkeypatch.setattr(crosslist.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(crosslist.chrome, "ensure_running", lambda port, **kw: chrome.READY)


def _deps(store, bus, **overrides):
    return crosslist.CrosslistDeps(
        store=store,
        bus=bus,
        config=Config(**overrides) if overrides else Config(),
        # Midday, so the default quiet window (23:00–08:00) is not in force.
        now=lambda: _noon(),
    )


def _noon() -> float:
    import time as _time

    stamp = _time.localtime()
    return _time.mktime(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 12, 0, 0, stamp.tm_wday, stamp.tm_yday, -1)
    )


def _midnight() -> float:
    import time as _time

    stamp = _time.localtime()
    return _time.mktime(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 2, 0, 0, stamp.tm_wday, stamp.tm_yday, -1)
    )


@pytest.fixture
def enabled(store):
    """A seller in SG who has asked for Carousell, with one item live on the rail."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "crosslist_markets", ["carousell"])
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)
    return store.get_item(item["id"])


def _queued(store):
    return [row for row in store.publish_pass_index() if row["status"] == "queued"]


def _notices(store):
    return [n["text"] for n in store.claim_queued_notices(10)]


# --- what gets queued -------------------------------------------------------------------------


def test_a_rail_listed_item_is_queued_for_the_enabled_market(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))

    assert pass_id
    assert _queued(store) == [
        {
            "market": "carousell",
            "item_id": enabled["id"],
            "origin": "crosslist",
            "status": "queued",
        }
    ]


def test_an_item_not_on_the_rail_yet_is_not_queued(store, bus) -> None:
    """Rail-first is a precondition, not an instruction: with no carousell.ai listing there is
    nothing to fan out from."""
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "crosslist_markets", ["carousell"])
    store.create_item(title="Teak lamp", list_price=80.0)

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_an_item_already_on_the_market_is_not_queued(store, bus, enabled) -> None:
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_nothing_is_queued_with_the_setting_at_its_default(store, bus) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    item = store.create_item(title="Teak lamp", list_price=80.0)
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_a_sold_item_is_not_queued(store, bus, enabled) -> None:
    """Its rail listing is about to be archived and its other listings are take-down work, so
    starting a new one would create something to immediately close."""
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="buyer",
        item_id=enabled["id"],
    )
    store.negotiate_confirm_sold(enabled["id"], "carousell:1")

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_a_market_the_sellers_region_lost_is_not_queued(store, bus, enabled) -> None:
    """The stored value still names Carousell, but a US account has nowhere to be listed there."""
    store.set_seller_config_section("basics", {"region": "US"})
    assert crosslist.enqueue_next(_deps(store, bus)) is None


# --- one shot ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status,cls", [("done", "ok"), ("error", "error"), ("error", "timeout")])
def test_a_settled_attempt_is_never_retried(store, bus, enabled, status, cls) -> None:
    """Every attempt is minutes of browser work and a vision-priced token bill, so a failure is
    reported to the seller rather than repeated behind their back."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.finish_pass(pass_id, status=status, rc=1, cls=cls, summary=cls)

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_an_attempt_by_hand_also_spends_the_shot(store, bus, enabled) -> None:
    """Same pair, same outcome: the lane's memory is the pass history, not a counter of its own."""
    manual = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell"})
    store.finish_pass(manual, status="error", rc=1, cls="error", summary="error")

    assert crosslist.enqueue_next(_deps(store, bus)) is None


def test_a_rail_publish_never_counts_as_an_attempt(store, bus, enabled) -> None:
    rail = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell-ai"})
    store.finish_pass(rail, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.enqueue_next(_deps(store, bus))


def test_only_one_publish_is_in_flight_at_a_time(store, bus, enabled) -> None:
    other = store.create_item(title="Brass lamp", list_price=40.0)
    store.record_listing_url(other["id"], "carousell-ai", _RAIL_URL + "2")

    assert crosslist.enqueue_next(_deps(store, bus))
    assert crosslist.enqueue_next(_deps(store, bus)) is None  # one is already queued
    assert len(_queued(store)) == 1


# --- when it holds off ------------------------------------------------------------------------


def test_a_paused_agent_queues_nothing(store, bus, enabled) -> None:
    store.set_paused(True)
    crosslist.crosslist_lane(_deps(store, bus))
    assert _queued(store) == []


def test_quiet_hours_hold_the_start_of_new_work(store, bus, enabled) -> None:
    # The store fixture seeds quiet hours off; this is the one test that needs a real window.
    seed_setting(store, "quiet_hours", [2300, 800])

    deps = crosslist.CrosslistDeps(store=store, bus=bus, config=Config(), now=_midnight)
    crosslist.crosslist_lane(deps)
    assert _queued(store) == []

    deps = crosslist.CrosslistDeps(store=store, bus=bus, config=Config(), now=_noon)
    crosslist.crosslist_lane(deps)
    assert len(_queued(store)) == 1


# --- the browser has to be there --------------------------------------------------------------


def test_no_node_means_no_attempt_spent(store, bus, enabled, monkeypatch) -> None:
    def _absent(command):
        raise BrowserUnavailable("'npx' is not installed")

    monkeypatch.setattr(crosslist.browser_client, "ensure_available", _absent)
    assert crosslist.enqueue_next(_deps(store, bus)) is None
    assert any("can't drive a browser" in text for text in _notices(store))

    # Eligibility survives, so installing Node is all it takes.
    monkeypatch.setattr(crosslist.browser_client, "ensure_available", lambda command: None)
    assert crosslist.enqueue_next(_deps(store, bus))


def test_chrome_that_will_not_start_means_no_attempt_spent(
    store, bus, enabled, monkeypatch
) -> None:
    monkeypatch.setattr(crosslist.chrome, "ensure_running", lambda port, **kw: chrome.UNAVAILABLE)
    assert crosslist.enqueue_next(_deps(store, bus)) is None
    notices = _notices(store)
    assert any("couldn't start the agent's Chrome" in text for text in notices)
    assert any("--remote-debugging-port" in text for text in notices)  # the by-hand command

    monkeypatch.setattr(crosslist.chrome, "ensure_running", lambda port, **kw: chrome.READY)
    assert crosslist.enqueue_next(_deps(store, bus))


def test_a_started_chrome_is_announced_before_the_publish(store, bus, enabled, monkeypatch) -> None:
    """A window appearing on its own is alarming; the seller should already know why."""
    monkeypatch.setattr(crosslist.chrome, "ensure_running", lambda port, **kw: chrome.LAUNCHED)
    crosslist.enqueue_next(_deps(store, bus))

    assert any("Opening the agent's Chrome" in text for text in _notices(store))


def test_an_already_running_chrome_is_not_announced(store, bus, enabled) -> None:
    crosslist.enqueue_next(_deps(store, bus))
    assert _notices(store) == []


def test_a_repeated_failure_tells_the_seller_once(store, bus, enabled, monkeypatch) -> None:
    monkeypatch.setattr(crosslist.chrome, "ensure_running", lambda port, **kw: chrome.UNAVAILABLE)
    deps = _deps(store, bus)
    for _ in range(3):
        crosslist.enqueue_next(deps)

    assert len(_notices(store)) == 1


# --- reporting the outcome --------------------------------------------------------------------


def test_a_recorded_url_is_reported_as_a_live_listing(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.report_settled(_deps(store, bus)) == 1
    notices = _notices(store)
    assert notices == [f"Teak lamp is now listed on Carousell: {_CAROUSELL_URL}"]


def test_a_clean_exit_without_a_url_is_reported_as_a_failure(store, bus, enabled) -> None:
    """The 07 shape: the pass said it was done and recorded nothing, so no listing exists that
    anyone can find. The row is the fact, not the exit code."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.report_settled(_deps(store, bus)) == 1
    text = _notices(store)[0]
    assert "couldn't list Teak lamp on Carousell" in text
    assert f"--item {enabled['id']} --market carousell" in text  # the retry, spelled out


def test_a_failure_names_the_retry_and_reassures_about_the_rail(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.finish_pass(pass_id, status="error", rc=1, cls="error", summary="error")

    crosslist.report_settled(_deps(store, bus))
    assert "carousell.ai listing" in _notices(store)[0]


def test_a_running_publish_is_not_reported_yet(store, bus, enabled) -> None:
    crosslist.enqueue_next(_deps(store, bus))
    assert crosslist.report_settled(_deps(store, bus)) == 0
    assert _notices(store) == []


def test_an_outcome_is_reported_exactly_once(store, bus, enabled) -> None:
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")

    deps = _deps(store, bus)
    assert crosslist.report_settled(deps) == 1
    assert crosslist.report_settled(deps) == 0
    assert len(_notices(store)) == 1


def test_a_publish_run_by_hand_is_not_reported(store, bus, enabled) -> None:
    """Whoever ran it is watching it; a notice would be the daemon narrating their own command."""
    manual = store.enqueue_pass("publish", {"item_id": enabled["id"], "market": "carousell"})
    store.finish_pass(manual, status="done", rc=0, cls="ok", summary="ok")

    assert crosslist.report_settled(_deps(store, bus)) == 0
    assert _notices(store) == []


def test_reporting_runs_even_while_paused(store, bus, enabled) -> None:
    """A pause stops the agent acting, not telling the seller what already happened."""
    pass_id = crosslist.enqueue_next(_deps(store, bus))
    store.record_listing_url(enabled["id"], "carousell", _CAROUSELL_URL)
    store.finish_pass(pass_id, status="done", rc=0, cls="ok", summary="ok")
    store.set_paused(True)

    crosslist.crosslist_lane(_deps(store, bus))
    assert len(_notices(store)) == 1


# --- backfill ---------------------------------------------------------------------------------


def test_enabling_a_market_picks_up_items_listed_before(store, bus) -> None:
    """Nothing special-cases the backlog: eligibility is a query, so items published long before the
    setting existed qualify the moment it is turned on."""
    store.set_seller_config_section("basics", {"region": "SG"})
    old = store.create_item(title="Teak lamp", list_price=80.0)
    store.record_listing_url(old["id"], "carousell-ai", _RAIL_URL)
    assert crosslist.enqueue_next(_deps(store, bus)) is None

    seed_setting(store, "crosslist_markets", ["carousell"])
    assert crosslist.enqueue_next(_deps(store, bus))


# --- what the seller conversation says about it ------------------------------------------------


def test_the_listing_flow_names_the_destinations_and_claims_no_tool() -> None:
    """The one thing the recipe is asked to do here is set expectations — the fan-out itself is the
    daemon's, and a recipe that thought it had to trigger it would look for a tool that isn't there.
    """
    from selly_agent import skills

    recipe = skills.load("listing-flow")
    assert "crosslist_markets" in recipe
    assert "not your job" in recipe
    assert "background" in recipe


def test_the_channel_pass_can_see_the_setting_it_is_told_to_name(store, bus, enabled) -> None:
    """Naming the destinations requires knowing them: the settings block carries the value."""
    block = settings.prompt_block(store)
    assert "crosslist_markets" in block
    assert "Carousell" in block


def test_settings_read_filters_to_publishable_markets(store, bus, enabled) -> None:
    """A stale id in the stored value is not an eligible publish."""
    seed_setting(store, "crosslist_markets", ["carousell", "fb"])
    assert settings.crosslist_markets(store) == ["carousell"]
    assert [market for _, market in crosslist.pending_pairs(_deps(store, bus))] == ["carousell"]
