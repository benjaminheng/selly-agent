# Craigslist Marketplace Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Craigslist as a second browser-driven marketplace (listing/publish only), following this codebase's existing adapter extension point, with zero changes to the generic browser/publish/settings layer.

**Architecture:** Complete the pre-existing `craigslist` stub in `data/marketplaces.json` (a `urls.sell` entry point and a `listing_flow` pointer, deliberately no `domains` map and no `urls.inbox`), add a new `browser/markets/craigslist.py` adapter module registered in `browser/markets/__init__.py`, and a `listing-flow-craigslist.md` skill. Every consumer (settings, onboarding, the publish tool, `selly-agent connect`, healthcheck) picks this up automatically through the existing registry/adapter seam — see `docs/superpowers/specs/2026-08-12-craigslist-marketplace-adapter-design.md` for the full design rationale, in particular why Craigslist ships listing-only (no in-page buyer inbox — contact is by email) and why there's no per-country `domains` map (Craigslist is per-city).

**Tech Stack:** Python 3 (stdlib only, per this repo's convention), pytest, ruff, pyright.

## Global Constraints

- No changes to any file under `src/selly_agent/browser/inbox.py`, `reconcile.py`, `sink.py`, `settings.py`, `setup_cli.py`, `passes.py`, `cli.py`, `http_server.py`, `healthcheck.py` — the adapter seam means none of these need touching. If any task appears to require touching one of them, stop and re-check the design doc rather than improvising.
- The `craigslist` registry entry gets **no `domains` map** and **no `urls.inbox`** — both are deliberate (see design doc "Two structural tensions"). Do not add either.
- `conversations_list_js` always returns `{conversations: []}`; `conversation_tail_js` always returns `null`. Both are permanent, honest facts about the platform, not placeholders — never add scraping logic to them.
- `chat_message_submit_js` stays at its dataclass default (omit it from the `MarketAdapter(...)` call entirely); `composer` stays `()`; `system_handles` stays `frozenset()`. There is never a chat composer on this market.
- Every new skill file must start with `---\ndescription: ...\n---` frontmatter (checked generically by `tests/test_skills.py`) and must not contain the literal substrings `"TODO("` or `"plan-0"` anywhere in its body.
- Run `make lint && make typecheck` before every commit in this plan; all three of `make lint`, `make typecheck`, `uv run pytest` must be clean before the final task closes.
- Work happens on the `feat/craigslist-marketplace-adapter` branch (already created off `master`, which is even with `upstream/master`). Do not touch `fix/rollback-cleanup-on-provision-failure` (a different, already-open PR's branch).

---

### Task 1: Complete the `craigslist` registry entry

**Files:**
- Modify: `src/selly_agent/data/marketplaces.json` (the `craigslist` entry, currently the last entry in the `marketplaces` array)
- Test: `tests/test_marketplaces.py`

**Interfaces:**
- Consumes: nothing new — `marketplaces.listing_flow()`, `marketplaces.market_url()`, `marketplaces.urls()` already exist in `src/selly_agent/marketplaces.py`.
- Produces: `marketplaces.listing_flow("craigslist") == "listing-flow-craigslist"`; `marketplaces.market_url("craigslist", "sell", <any region or None>) == "https://craigslist.org/"`; `marketplaces.urls("craigslist").get("inbox") is None`. Task 2 relies on `listing_flow` being set here (it's one of the two conditions `supported_markets()` checks).

- [ ] **Step 1: Write the failing tests**

Add this new section at the end of `tests/test_marketplaces.py`:

```python
# --- craigslist: registry entry -----------------------------------------------------------------


def test_craigslist_registry_entry_is_complete() -> None:
    assert marketplaces.listing_flow("craigslist") == "listing-flow-craigslist"
    assert marketplaces.market_url("craigslist", "sell", "US") == "https://craigslist.org/"
    assert marketplaces.market_url("craigslist", "sell", None) == "https://craigslist.org/"


def test_craigslist_has_no_recorded_inbox_url() -> None:
    """Craigslist has no in-page buyer inbox, so the read lane must never be pointed at one —
    covered end to end in test_browser_inbox.py."""
    assert marketplaces.urls("craigslist").get("inbox") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_marketplaces.py -k craigslist_registry_entry_is_complete -v`
Expected: FAIL — `listing_flow("craigslist")` is `""` and `market_url(...)` is `None` (no `urls` key yet on the entry).

The second new test (`test_craigslist_has_no_recorded_inbox_url`) will already pass (there's no `urls` key at all yet, so `.get("inbox")` is `None`) — that's fine, it's a guard for later, not something this step is driving.

- [ ] **Step 3: Edit the registry entry**

In `src/selly_agent/data/marketplaces.json`, find the `craigslist` entry (the last one in the `marketplaces` array):

```json
    {
      "id": "craigslist",
      "display_name": "Craigslist",
      "listing_url": {
        "host": "craigslist.org",
        "path": "/d/"
      },
      "connector": {
        "type": "browser"
      },
      "status": "active"
    }
```

Replace it with:

```json
    {
      "id": "craigslist",
      "display_name": "Craigslist",
      "listing_url": {
        "host": "craigslist.org",
        "path": "/d/"
      },
      "connector": {
        "type": "browser"
      },
      "status": "active",
      "urls": {
        "sell": "/"
      },
      "listing_flow": "listing-flow-craigslist"
    }
```

Note there is still no `domains` key (Craigslist has no per-country site — see the design doc) and no `inbox` key under `urls` (Craigslist has no in-page buyer inbox to read).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_marketplaces.py -v`
Expected: PASS — all tests in this file, including the two new ones and the pre-existing `test_resolve_falls_back_to_listing_url_host`, which is unaffected by this change.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
make lint
make typecheck
git add src/selly_agent/data/marketplaces.json tests/test_marketplaces.py
git commit -m "$(cat <<'EOF'
data/marketplaces: complete the craigslist registry entry

Adds urls.sell (the publish composer's entry point) and listing_flow,
completing the pre-existing stub. Deliberately no domains map (Craigslist is
per-city, not per-country) and no urls.inbox (no in-page buyer inbox to
read) -- see docs/superpowers/specs/2026-08-12-craigslist-marketplace-adapter-design.md.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Create the Craigslist adapter module and register it

**Files:**
- Create: `src/selly_agent/browser/markets/craigslist.py`
- Modify: `src/selly_agent/browser/markets/__init__.py`
- Test: `tests/test_marketplaces.py`

**Interfaces:**
- Consumes: `MarketAdapter` dataclass and `_ADAPTERS` dict from `src/selly_agent/browser/markets/__init__.py` (already defined, used the same way `CAROUSELL` is).
- Produces: `market_adapters.CRAIGSLIST` (a `MarketAdapter` instance), registered in `market_adapters._ADAPTERS["craigslist"]`. `market_adapters.supported_markets()` now returns `["carousell", "craigslist"]`. `market_adapters.publishable_markets(region)` now includes `"craigslist"` for every region (including `None`), since it has no `domains` map. `craigslist.LISTING_ID_PATTERN` (a regex string with one capture group) — used by Task 4's guard test only by cross-reference, not consumed elsewhere in this plan.

- [ ] **Step 1: Write the failing tests**

First, add these two imports to the top of `tests/test_marketplaces.py`, alongside the existing imports:

```python
import re

from selly_agent import marketplaces
from selly_agent.browser import markets as market_adapters
from selly_agent.browser.markets.craigslist import LISTING_ID_PATTERN
from selly_agent.engines import hosts
```

(`import re` and the `LISTING_ID_PATTERN` import are new; the other two lines already exist — just add the new ones in that position, keeping the existing two.)

Then update the two existing hardcoded assertions:

```python
def test_supported_markets_is_the_adapter_registry() -> None:
    """Carousell and Craigslist today — every other browser entry is a host the scanner needs, not
    a market anything can drive."""
    assert market_adapters.supported_markets() == ["carousell", "craigslist"]
```

```python
def test_publishable_markets_follow_the_seller_region() -> None:
    """Carousell runs no US site, so a US seller has nowhere to be listed there. Craigslist has no
    domains map at all (see test_resolve_falls_back_to_listing_url_host), so it resolves for every
    region alike -- including no region at all."""
    assert market_adapters.publishable_markets("SG") == ["carousell", "craigslist"]
    assert market_adapters.publishable_markets("US") == ["craigslist"]
    assert market_adapters.publishable_markets(None) == ["craigslist"]
```

(These replace the existing `test_supported_markets_is_the_adapter_registry` and `test_publishable_markets_follow_the_seller_region` — same names, same position in the file, new bodies and docstrings.)

Then add this new section at the end of the file (after the `test_craigslist_has_no_recorded_inbox_url` test added in Task 1):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_marketplaces.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'selly_agent.browser.markets.craigslist'` (the import at the top of the file fails, since the module doesn't exist yet), so every test in the file errors at collection.

- [ ] **Step 3: Write the adapter module**

Create `src/selly_agent/browser/markets/craigslist.py`:

```python
"""Craigslist's browser contract: listing/publish only — there is no in-page buyer inbox to read.

A buyer's "reply" to a Craigslist posting is relayed through an anonymized email address and lands
in the seller's own email inbox, a channel this layer never touches. `conversations_list_js` and
`conversation_tail_js` are therefore permanent, honest stubs — not scrapers with nothing yet to
scrape — and the registry records no `urls.inbox` for this market (`data/marketplaces.json`), so
`browser/inbox.py`'s read lane skips it via its existing "no recorded inbox URL" path before ever
calling them.

`login_js` is not dead code, though: `selly-agent connect craigslist` and the healthcheck's
per-market login line navigate to the market's home page directly (`marketplaces.market_home`,
independent of `urls.inbox`) and evaluate it, for whichever markets the seller has enabled.

Craigslist's pages are old-school server-rendered HTML, not a hashed-class SPA, so URL shapes here
(the permalink pattern below) are stable and long-standing. The login-state markers in `LOGIN_JS`
are a best effort — this module was written without a live, logged-in session to inspect — and are
worth a maintainer's confirmation against a real account.
"""

from __future__ import annotations

# No bot/assistant-account concept on Craigslist — every conversation this layer could ever see
# would be a buyer's, if it could see any at all.
SYSTEM_HANDLES = frozenset()

# A posting's permalink is .../<region>/<category>/d/<title-slug>/<digits>.html — the numeric id
# is the last path segment before the extension. Stable for years, unlike a hashed-class SPA.
LISTING_ID_PATTERN = r"/(\d+)\.html$"

# Permanent, honest fact: Craigslist has no on-platform conversation list, on any city site, ever.
# Not reachable from the read lane in practice (the registry defines no `urls.inbox` for this
# market, so `browser/inbox.py` skips it before this would be called) — kept only to satisfy
# `MarketAdapter`'s required shape truthfully.
CONVERSATIONS_LIST_JS = """() => {
  return { conversations: [] };
}"""

# Unreachable in practice: conversations_list_js never reports a conversation to open, so nothing
# ever calls this either. Kept, and kept honest (an abstain, not a fabricated empty tail), only to
# satisfy the adapter's required shape.
CONVERSATION_TAIL_JS = """() => {
  return null;
}"""

# Is the seller logged in? Three-state, and it must never answer logged_out on thin evidence — see
# carousell.py's LOGIN_JS for why. Best effort: Craigslist's login lives at
# accounts.craigslist.org; a signed-in page links to the post-login account home or a "log out"
# action, a signed-out one only ever offers to log in. Needs confirming against a live account.
LOGIN_JS = """() => {
  try {
    const loggedIn = !!document.querySelector(
      'a[href*="accounts.craigslist.org/login/home"], a[href*="/login/home"], a[href*="logout"]'
    );
    if (loggedIn) return { state: 'logged_in' };
    const loginLink = !!document.querySelector('a[href*="accounts.craigslist.org/login"]');
    const text = (document.body && document.body.innerText) || '';
    if (loginLink || /\\blog\\s?in\\b/i.test(text)) return { state: 'logged_out' };
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""
```

- [ ] **Step 4: Register the adapter**

In `src/selly_agent/browser/markets/__init__.py`, add the import (alongside the existing `carousell` import):

```python
from selly_agent.browser.markets import carousell
from selly_agent.browser.markets import craigslist
```

Then, immediately after the existing `CAROUSELL = MarketAdapter(...)` block, add:

```python
CRAIGSLIST = MarketAdapter(
    market="craigslist",
    conversations_list_js=craigslist.CONVERSATIONS_LIST_JS,
    conversation_tail_js=craigslist.CONVERSATION_TAIL_JS,
    login_js=craigslist.LOGIN_JS,
    listing_id_pattern=craigslist.LISTING_ID_PATTERN,
    system_handles=craigslist.SYSTEM_HANDLES,
)
```

And change the `_ADAPTERS` line from:

```python
_ADAPTERS = {CAROUSELL.market: CAROUSELL}
```

to:

```python
_ADAPTERS = {CAROUSELL.market: CAROUSELL, CRAIGSLIST.market: CRAIGSLIST}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_marketplaces.py -v`
Expected: PASS — all tests, including the two updated ones and the four new ones.

- [ ] **Step 6: Run the full suite once to check for unexpected ripple**

Run: `uv run pytest -q`
Expected: PASS, with the same pre-existing unrelated failure noted in PR #2 (`tests/test_docker_assets.py::test_the_container_refuses_to_start_without_a_timezone_or_a_token`, present on unmodified `upstream/master` too) and nothing else new. If anything else fails, stop and diagnose before continuing — the design doc's "What's solid vs. what needs live verification" table and the non-goals list are the reference for what should and shouldn't be touched.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
make lint
make typecheck
git add src/selly_agent/browser/markets/craigslist.py src/selly_agent/browser/markets/__init__.py tests/test_marketplaces.py
git commit -m "$(cat <<'EOF'
browser/markets: add the Craigslist adapter

Listing-only: conversations_list_js/conversation_tail_js are permanent,
honest stubs (Craigslist has no in-page buyer inbox -- contact is by
email), login_js is a real best-effort probe used by `selly-agent connect`
and the healthcheck's per-market login line. supported_markets() and
publishable_markets() now include craigslist.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Write the Craigslist listing-flow skill

**Files:**
- Create: `src/selly_agent/skills/listing-flow-craigslist.md`
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: `passes.PASS_TYPES["publish"].skills_for({"market": "craigslist"})`, which reads `marketplaces.listing_flow("craigslist")` (set in Task 1) to resolve the skill name.
- Produces: a loadable skill named `listing-flow-craigslist`, picked up automatically by `skills.available()`.

- [ ] **Step 1: Write the failing test**

In `tests/test_skills.py`, add a new line to `EXPECTED_SKILL_SETS`, immediately after the existing `("publish", (("market", "carousell"),))` line:

```python
EXPECTED_SKILL_SETS = {
    ("publish", ()): ("selly-conventions", "listing-flow"),
    ("publish", (("market", "carousell-ai"),)): ("selly-conventions", "listing-flow"),
    ("publish", (("market", "carousell"),)): ("selly-conventions", "listing-flow-carousell"),
    ("publish", (("market", "craigslist"),)): ("selly-conventions", "listing-flow-craigslist"),
    ("channel", ()): ("selly-conventions", "voice-and-style", "seller-comms", "listing-flow"),
    ("reply", ()): (
        "selly-conventions",
        "voice-and-style",
        "buyer-conversation",
        "scam-guard",
    ),
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skills.py -k test_every_pass_type_composes_resolvable_skills -v`
Expected: FAIL — `skills.skill_path("listing-flow-craigslist").exists()` is `False`.

- [ ] **Step 3: Write the skill file**

Create `src/selly_agent/skills/listing-flow-craigslist.md`:

```markdown
---
description: Publishing an item to Craigslist in the browser — finding the seller's city, then the composer, step by step
---

# Listing flow — Craigslist (browser)

Publishing one already-confirmed item to Craigslist by filling its real posting form in the
seller's own logged-in Chrome. The numbers were agreed with the seller before this pass started:
publish what the item record says, and change nothing.

Read the item with `get_item` for its title, price, description and condition. Its photos are in
your working directory, named in your prompt — upload those, not the paths `get_item` reports.

**Craigslist has no in-app inbox.** A buyer's reply lands in the seller's own email, outside this
session entirely — nothing you do here creates an inbox thread, and there is nothing more to check
after publishing. Say so plainly in your report once the listing is live.

## Before you touch the page

**Open your own tab first** (`browser_tabs`, action `new`) and work only in it. Other tabs may be
mid-flow for something else; never switch to one.

Craigslist runs one site per city, not one per country — there is no single global posting page.
**Never construct or guess a city subdomain.** Navigate to the composer URL your prompt gives you,
and if it does not already land you on your own city's site while logged in, follow the page's own
link to your account or your city — read it off the page, the same way you would read any other
navigation target.

## Steps

1. **Go to the composer.** `browser_navigate` to the composer URL your prompt gives you. If it
   lands on a country/city picker rather than a posting form, follow the seller's own account link
   (or the page's own "post to classifieds" control) to reach their real city site — never type a
   city subdomain from memory.
2. **Post type and category.** Choose "for sale by owner" (never "for sale by dealer" unless the
   seller's item record says this is a dealer account), then the closest matching category. Accept
   Craigslist's own category suggestion when it fits the item; otherwise pick the closest.
3. **Fill title, price, location and description in ONE `browser_fill_form`.** That call is real
   typed input and is the right way to fill these fields. Never set a field's value through
   `browser_evaluate`: that is synthetic input with no focus or keystroke cadence behind it, which
   is exactly the automation signature this whole approach exists to avoid. Use the seller's own
   neighborhood/zip for location — never a location Craigslist has not offered on the page.
4. **Verify every field you filled, in ONE `browser_evaluate`** that returns all the values you
   set — never one read per field. Confirm each is what you sent (compare price on its digits —
   the page may reformat it). A field that did not take gets re-filled individually.
5. **Condition.** Set it from the item's condition, mapped to Craigslist's own condition options
   (new / like new / excellent / good / fair / salvage) — pick the closest when there is no exact
   match, and report which one you picked.
6. **All photos in one upload.** One `browser_file_upload` with every file from your working
   directory — never one file per call, which is the slowest thing this flow can do.
7. **Read any suggested or comparable price** the page shows, and report it — it is a signal of
   what the item actually sells for. Read it, never remember it: it only exists mid-flow.
8. **Publish.** Read the preview back, then submit as an ordinary click. If a phone-number or
   email verification step appears, or a CAPTCHA, **stop and escalate to the seller** — this is not
   something to retry past, and retrying against a verification wall is the clearest automation
   signal there is.
9. **Get the live URL from the page, then record it.** The permalink ends `/<digits>.html`. **Only
   ever report a URL you read off the page** — never one you assembled. Then call
   `record_published_listing_url`: until you do, the listing is not recorded as live. No readable
   permalink means the publish failed: say so, rather than reporting a listing as live.
10. **Close your tab.** `browser_tabs`, action `close`, once the URL is recorded — including when
    the publish failed. A tab left behind outlives this pass.

## Never spend money

Never click anything that costs money: no "Post to more categories", no bump, no featured-ad
upsell. Before clicking any control that might, classify it: free, paid, or unclear. **Unclear
means click nothing.** If a payment screen appears at any point, stop, dismiss it, and report that
the step needs money — do not confirm a purchase.

## When it goes wrong

- **Logged out, a verification wall, or a captcha:** stop this marketplace, escalate to the seller
  to re-authenticate or verify by hand, and do not retry. Repeated attempts against a verification
  wall are the clearest automation signal there is.
- **A field needs re-finding more than three times in one pass:** stop and report it. Something has
  changed structurally.
- **Anything you cannot verify:** report it as failed. The draft and its photos survive, so a retry
  costs nothing — reporting a listing as live when it is not costs the seller a sale.
- **No buyer-reply coverage.** Once published, tell the seller plainly that Selly cannot see or
  answer replies to this listing — they arrive by email, a channel this session does not read.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_skills.py -v`
Expected: PASS — including the generic loops (`test_every_installed_skill_loads`,
`test_no_loaded_skill_still_carries_frontmatter`, `test_the_drafts_ship_without_their_authoring_notes`), which pick up the new file automatically via `skills.available()`.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
make lint
make typecheck
git add src/selly_agent/skills/listing-flow-craigslist.md tests/test_skills.py
git commit -m "$(cat <<'EOF'
skills: add the Craigslist listing-flow recipe

Mirrors listing-flow-carousell.md's structure: find the composer, fill it
with real typed input, verify every field, escalate rather than retry past
a verification wall or captcha, and state plainly that Selly has no
buyer-reply coverage for this market (Craigslist has no in-app inbox).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Add the inbox-lane regression guard

**Files:**
- Modify: `tests/test_browser_inbox.py`

**Interfaces:**
- Consumes: `inbox.inbox_lane`, the existing `StubClient`, `_deps`, `seeded`, `_thread`, `_conv`, `_bubble` fixtures/helpers already defined in this file (unchanged).
- Produces: nothing new consumed elsewhere — this is a standalone regression guard proving Task 1+2's "no `urls.inbox`" decision keeps the read lane inert for Craigslist.

- [ ] **Step 1: Write the test**

In `tests/test_browser_inbox.py`, add this test immediately after `test_an_unknown_login_state_still_reads` and its two-line body ending `assert store.count_queued_notices() == 0` — right before `def test_no_browser_degrades_with_one_notice_and_no_crash`, which currently follows it directly with no section comment in between:

```python
def test_a_market_with_no_recorded_inbox_url_is_never_navigated_to(store, bus, seeded) -> None:
    """Craigslist has no in-page inbox and the registry records no `urls.inbox` for it — the lane
    must skip it via the existing "no recorded inbox URL" path, never touching the browser for it,
    and Carousell's own read must be completely unaffected."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client))
    assert all("craigslist" not in url for url in client.navigations)
    assert store.get_thread("carousell:99")["message_count"] == 1
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_browser_inbox.py -k test_a_market_with_no_recorded_inbox_url_is_never_navigated_to -v`
Expected: PASS immediately — Tasks 1 and 2 already made this true; this step is confirming it, not driving new behavior.

- [ ] **Step 3: Run the whole file to confirm zero ripple**

Run: `uv run pytest tests/test_browser_inbox.py -v`
Expected: PASS — every pre-existing test in this file (all ~35 of them, including the exact-list navigation assertions like `test_the_read_navigates_only_recorded_urls`) passes completely unchanged, because Craigslist's `_read_market` call returns before touching `client.navigate` or `client.evaluate` at all (no `urls.inbox` means `inbox_url is None` at the top of `_read_market`). If any pre-existing assertion in this file fails, stop — that means Task 1 or 2 accidentally gave Craigslist an `inbox` URL or otherwise changed its registry/adapter shape from what this plan specifies; re-check those tasks' diffs before touching this file further.

- [ ] **Step 4: Lint, typecheck, commit**

```bash
make lint
make typecheck
git add tests/test_browser_inbox.py
git commit -m "$(cat <<'EOF'
tests: guard that a market with no urls.inbox stays inert in the read lane

Regression guard for the Craigslist adapter's design: registering an
adapter with no urls.inbox must never cause the lane to navigate or
evaluate anything for that market, and must never affect any other
market's read.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Update documentation

**Files:**
- Modify: `docs/browser-layer.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing (prose only).
- Produces: nothing consumed by other tasks — this is the last content task before final verification.

- [ ] **Step 1: Update `docs/browser-layer.md`'s "Registry vs. adapter" section**

Find this paragraph (under `### Registry vs. adapter`):

```markdown
`data/marketplaces.json` says a market is `connector.type == "browser"`;
`browser_markets()` returns the active ones in registry order. Several entries
qualify today (Facebook, Mercari, OfferUp, Poshmark, Craigslist) and **only
Carousell has an adapter** — the lane skips a registry entry with no adapter, so
listing a market in the registry does not make it read. Page URLs come from the
registry's `urls` templates and the region→host `domains` map, never from a guess:
an unrecorded template resolves to `None` and the caller reports that.
```

Replace it with:

```markdown
`data/marketplaces.json` says a market is `connector.type == "browser"`;
`browser_markets()` returns the active ones in registry order. Several entries
still qualify with no adapter at all (Facebook, Mercari, OfferUp, Poshmark) —
the lane skips a registry entry with no adapter, so listing a market in the
registry does not make it read. Carousell and Craigslist both have adapters;
Craigslist's is listing-only (see below) and its registry entry records no
`urls.inbox`, so it is skipped by the same "no recorded inbox URL" path rather
than actually being read. Page URLs come from the registry's `urls` templates
and the region→host `domains` map, never from a guess: an unrecorded template
resolves to `None` and the caller reports that.
```

- [ ] **Step 2: Add a "Adapter: Craigslist" subsection**

Find the end of the `### Adapter: Carousell` section — it ends with this bullet, immediately before the `## The read lane` heading:

```markdown
- **There is no send-button selector**, because there is nothing addressable to
  click: the send icon's ancestors are undecorated elements with no role, no
  label and no cursor change, while the message box handles Enter itself.

## The read lane
```

Insert a new subsection between them, so it reads:

```markdown
- **There is no send-button selector**, because there is nothing addressable to
  click: the send icon's ancestors are undecorated elements with no role, no
  label and no cursor change, while the message box handles Enter itself.

### Adapter: Craigslist

- **Listing/publish only — there is no in-page buyer inbox to read.** A buyer's
  reply to a Craigslist posting is relayed through an anonymized email address
  and lands in the seller's own email, a channel this layer never touches.
  `conversations_list_js` and `conversation_tail_js` are permanent, honest stubs
  (an always-empty list, an always-`null` tail) rather than DOM scrapers with
  nothing yet to scrape.
- **The registry records no `urls.inbox` for this market**, so the read lane's
  own "no recorded inbox URL — skip" path keeps it fully inert in the
  background lane: no per-tick navigation, and no change to how any other
  market's read behaves.
- **`login_js` is still real and still used** — `selly-agent connect craigslist`
  and the healthcheck's per-market login line navigate to
  `marketplaces.market_home()` directly, independent of `urls.inbox`, and
  evaluate it for whichever markets the seller has enabled.
- **No `domains` map.** Craigslist runs one host per city, not one per country —
  a country-level map would silently misroute most sellers. The registry falls
  back to the bare `craigslist.org` host; the publish skill finds the seller's
  actual city live, off the page, rather than the code guessing a subdomain.
- **The permalink id is the last numeric path segment before `.html`** —
  Craigslist's listing pages are plain server-rendered HTML, stable for years,
  unlike a hashed-class SPA.

## The read lane
```

- [ ] **Step 3: Note the exception in "Adding a marketplace"**

Find this text near the end of the file:

```markdown
4. **A publish recipe skill**, if the market should be publishable, pointed at by
   the registry's `listing_flow`. Steps 2 and 4 together are what make the market
   selectable in `crosslist_markets` — there is no separate switch to remember.

Nothing else in the layer changes: the read lane, reconcile, the sink and the
selector cache are all written against the protocol.
```

Replace it with:

```markdown
4. **A publish recipe skill**, if the market should be publishable, pointed at by
   the registry's `listing_flow`. Steps 2 and 4 together are what make the market
   selectable in `crosslist_markets` — there is no separate switch to remember.

A market need not fit every part of this shape. Craigslist (`### Adapter:
Craigslist` above) has no in-page buyer inbox at all, so it ships with no
`domains` map and no `urls.inbox` — the two required-looking pieces in step 1
are a norm most markets follow, not a hard requirement of the protocol itself.

Nothing else in the layer changes: the read lane, reconcile, the sink and the
selector cache are all written against the protocol.
```

- [ ] **Step 4: Update `README.md`**

Find:

```markdown
- **Supported marketplaces**: Carousell
```

Replace it with:

```markdown
- **Supported marketplaces**: Carousell, Craigslist (listing only — Craigslist has no in-app buyer inbox, so replies arrive by email, outside Selly)
```

- [ ] **Step 5: Run the full suite once more (docs-only change, but confirm nothing broke)**

Run: `uv run pytest -q`
Expected: PASS, same as Task 2's Step 6.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
make lint
make typecheck
git add docs/browser-layer.md README.md
git commit -m "$(cat <<'EOF'
docs: document the Craigslist adapter

Updates the "Registry vs. adapter" summary, adds an "Adapter: Craigslist"
subsection mirroring the Carousell one, notes that Craigslist is an
intentional exception to the "Adding a marketplace" recipe's domains/inbox
expectations, and lists it as supported in the README.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Final verification and screenshot

**Files:** none (verification only).

**Interfaces:** none.

- [ ] **Step 1: Run the full check suite**

```bash
make lint
make typecheck
uv run pytest
```

Expected: `make lint` and `make typecheck` clean; `uv run pytest` green except the one pre-existing, unrelated failure already noted in PR #2 (`tests/test_docker_assets.py::test_the_container_refuses_to_start_without_a_timezone_or_a_token`) — confirm that failure (and only that one) is present on unmodified `master` too, by stashing and re-running if there's any doubt:

```bash
git stash
uv run pytest tests/test_docker_assets.py -k test_the_container_refuses_to_start_without_a_timezone_or_a_token -v
git stash pop
```

- [ ] **Step 2: Capture a terminal screenshot of the green run**

Re-run the three commands in one visible terminal pass and capture a screenshot of the output (following PR #2's precedent exactly — a screenshot of `make lint && make typecheck && uv run pytest` all green). Save it for the PR body; it gets committed to an orphan `assets/craigslist-adapter-screenshot` branch (outside the source tree) in the "raise the PR" step, not into this feature branch.

- [ ] **Step 3: Review the full diff against `master` one more time**

```bash
git diff master...feat/craigslist-marketplace-adapter --stat
git log --oneline master..feat/craigslist-marketplace-adapter
```

Confirm the diff touches only: `src/selly_agent/data/marketplaces.json`, `src/selly_agent/browser/markets/craigslist.py` (new), `src/selly_agent/browser/markets/__init__.py`, `src/selly_agent/skills/listing-flow-craigslist.md` (new), `tests/test_marketplaces.py`, `tests/test_skills.py`, `tests/test_browser_inbox.py`, `docs/browser-layer.md`, `README.md`, plus the two design-doc commits from earlier in this session (`docs/superpowers/specs/2026-08-12-craigslist-marketplace-adapter-design.md`) and this plan file itself once committed. No file under the "Global Constraints" no-touch list should appear.

This task has no commit of its own — it's the readiness gate before raising the issue and PR (a separate step, outside this plan, per the design doc's "Issue + PR pair" section).
