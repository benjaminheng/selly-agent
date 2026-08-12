# Craigslist marketplace adapter — design

Status: approved (scope + city-handling confirmed with user 2026-08-12)

## Goal

Add Craigslist as a second browser-driven marketplace, following the extension point this
codebase already documents and partially seeds: `data/marketplaces.json` carries a `craigslist`
stub (`connector.type: "browser"`, `status: "active"`) and `docs/browser-layer.md`'s own "Adding a
marketplace" section names Craigslist explicitly as a market with a registry entry but no adapter.
Today only `carousell` has a real adapter, so `supported_markets()` / `publishable_markets()`
return only `carousell` regardless of what the registry lists.

This is a feature addition, not a bug fix — motivated by wanting sellers to have another
marketplace option besides Carousell, and picked because Craigslist is the best-fit next
candidate among the pre-stubbed markets (fb, ebay, mercari, offerup, poshmark, craigslist):
its posting pages are old-school server-rendered HTML rather than a hashed-class SPA, so its
*stable* facts (permalink shape, page structure, lack of in-page chat) can be established with
high confidence without a live authenticated session — unlike Mercari/OfferUp/Poshmark, which
would need the same DOM-verification research Carousell's adapter clearly had (a live logged-in
browser), not available in this environment.

## Non-goals

- Buyer-chat automation on Craigslist (see "Scope" below).
- A new seller-configurable "city" setting (see "City handling" below).
- Touching any other marketplace's registry entry or adapter.
- Touching the generic layer (`inbox.py`, `reconcile.py`, `sink.py`, `settings.py`,
  `setup_cli.py`, `passes.py`, `cli.py`, `http_server.py`, `healthcheck.py`) — the whole point of
  the adapter seam is that none of these need to change.

## Two structural tensions, and how this design resolves them

**1. Craigslist has no in-page buyer inbox.** The `MarketAdapter` contract
(`conversations_list_js`, `conversation_tail_js`, `chat_message_submit_js`) assumes a
marketplace with an addressable conversation list and message thread, the way Carousell has.
Craigslist doesn't: a buyer's "reply" to a posting is relayed through an anonymized email
address, landing in the seller's own email inbox — a channel this codebase doesn't touch at
all. There is no page to scrape a thread from.

**Resolution: ship listing/publish only.** `conversations_list_js` permanently returns
`{conversations: []}` — a true, static fact about the platform ("no on-platform threads exist"),
not an error state and not a placeholder for future work. `conversation_tail_js` is correspondingly
dead code (never reached, since the conversation list is always empty) and returns `null`
(abstain) for completeness. The inbox lane reads this every tick and finds nothing to do — cheap,
harmless, honest. The upstream issue and PR both say plainly that Craigslist buyer-reply
automation is out of scope because the platform has no on-platform channel for it, not because of
missing engineering effort.

**2. Craigslist is per-city, not per-country.** Every other registry entry's `domains` map is one
host per ISO region (`SG → www.carousell.sg`). Craigslist runs one host per city
(`sfbay.craigslist.org`, `newyork.craigslist.org`, `singapore.craigslist.org`, ...) — hundreds of
them, with no country-level grouping. This codebase's seller-region setting is country-granular
(`seller_region()` → `"SG"`, `"US"`, ...), too coarse to pick a Craigslist city.

**Resolution: no fabricated mapping; discover the city live.** The registry entry carries no
`domains` map (matching its current stub shape and the existing test
`test_resolve_falls_back_to_listing_url_host`, which already documents and asserts this fallback:
"craigslist has no domains map and a real host, so the listing_url host is the answer"). `urls.sell`
and `urls.inbox` both resolve against the bare `craigslist.org` host — a real, always-reachable
page (the site's own country/city directory), not a fabricated deep link. The `listing-flow-craigslist.md`
skill's first step instructs the agent to log in and follow the seller's own account/redirect to
their real city site, rather than the code guessing a subdomain — consistent with this codebase's
existing rule (`marketplaces.market_url`'s own docstring) that navigation targets come from the
registry, a stored URL, or a link read off a live page, never a guess.

## What's solid vs. what needs live verification

No live browser is available in this environment (the Chrome extension isn't connected here), so
this adapter cannot be built from an inspected live session the way Carousell's evidently was.
Craigslist's public pages are stable, server-rendered HTML — not an SPA — so URL/permalink shape
is high confidence from long-standing, well-documented structure. Exact DOM selectors for
login-state and the posting wizard are lower confidence and flagged as such in code comments and
in the upstream issue, for a maintainer with a live account to confirm or correct — the same
courtesy this codebase's own selector-healing cache exists to make cheap to fix later.

| Fact | Confidence | Where it's used |
|---|---|---|
| Permalink shape `.../d/<slug>/<digits>.html` | High (stable for years) | `LISTING_ID_PATTERN` |
| No in-page buyer inbox; contact is by email | High (documented platform behavior) | scope decision above |
| One host per city, no country grouping | High | no `domains` map |
| `craigslist.org` root is a real, reachable directory page | Medium-high | `urls.sell` / `urls.inbox` fallback host |
| Account login exists and gates posting/managing listings | Medium-high (long-standing feature) | `LOGIN_JS` |
| Exact login-state DOM markers | Low — needs live verification | `LOGIN_JS` body, flagged in comments |
| Exact posting-wizard steps/fields | Medium (well-known general shape; exact field names unverified) | `listing-flow-craigslist.md` |

## Changes

### 1. `data/marketplaces.json` — complete the `craigslist` entry

Add `urls` (`sell`, `inbox` — both `"/"`, the bare root) and `listing_flow:
"listing-flow-craigslist"`. No `domains` map (see above). `connector.type: "browser"` and
`status: "active"` are already present.

### 2. `browser/markets/craigslist.py` — new adapter module

- `SYSTEM_HANDLES = frozenset()` — no bot/assistant accounts concept on Craigslist.
- `LISTING_ID_PATTERN` — the numeric id before `.html` in a posting permalink.
- `CONVERSATIONS_LIST_JS` — synchronous, always `{conversations: []}`, with a doc comment
  explaining this is permanent, not provisional.
- `CONVERSATION_TAIL_JS` — always `null`; doc comment notes it is unreachable given the above,
  kept only to satisfy the adapter's required-field shape.
- `LOGIN_JS` — three-state probe (`logged_in` / `logged_out` / `unknown`), same fail-closed shape
  as Carousell's (never guesses `logged_out` on thin evidence), against the best-known stable
  markers for Craigslist's account nav; comment flags it as needing live confirmation.
- `CHAT_MESSAGE_SUBMIT_JS` — omitted (default `""`); there is never a composer to submit into.
- `composer` — left empty (`()`); no chat composer exists to define defaults for.

### 3. `browser/markets/__init__.py` — register it

One `CRAIGSLIST = MarketAdapter(...)` literal and one entry in `_ADAPTERS`, mirroring `CAROUSELL`.

### 4. `skills/listing-flow-craigslist.md` — new skill

Structured like `listing-flow-carousell.md`: read the item, find the seller's real city (live,
per above), walk the posting wizard (category → title/price/location/description → photos →
review → publish), record the published permalink via `record_published_listing_url`, and
explicit guardrails — never pay for a bump/feature, stop and escalate on a phone/email
verification gate or CAPTCHA rather than retry, and state plainly that Selly cannot see or answer
replies to this listing (they arrive by email, outside this session).

### 5. Docs

- `docs/browser-layer.md`: update the "Registry vs. adapter" paragraph (Craigslist moves from
  "registry entry, no adapter" to having one) and add a `### Adapter: Craigslist` subsection
  mirroring `### Adapter: Carousell`, documenting the two resolved tensions above.
- `README.md`: update "Supported marketplaces: Carousell" to include Craigslist, noting
  listing-only.

### 6. Tests

- `tests/test_marketplaces.py`: update the two hardcoded assertions
  (`test_supported_markets_is_the_adapter_registry`,
  `test_publishable_markets_follow_the_seller_region`) to include `craigslist` — an expected,
  intentional change, not a regression, since both now reflect a second real adapter.
- `tests/test_browser_inbox.py`: extend `StubClient.evaluate` to recognize Craigslist's JS
  artifacts (currently raises `AssertionError` on anything not Carousell's, which the real
  registry's now-adapter-having `craigslist` entry would otherwise trip on every existing test).
  Add one test asserting a Craigslist tick reads as empty and quiet — no blind count, no thread
  created, no error — confirming the always-empty contract is actually harmless in the lane.
- New `tests/test_browser_markets_craigslist.py` (or folded into `test_marketplaces.py`): shape
  tests for the adapter — `LISTING_ID_PATTERN` matches real-shaped permalinks and rejects
  non-matches, `CRAIGSLIST` is registered and satisfies the same structural checks
  `test_browser_sink.py` already runs against `CAROUSELL`.

No test executes the JS artifacts themselves (the suite only compares them by identity via the
`StubClient` pattern) — so this is scoped to Python-level shape/wiring, matching the existing
test suite's own approach to Carousell.

## PR assets

- **Mermaid diagram**: the registry+adapter+skill extension seam, and where the two tensions
  (no inbox, no domains map) are resolved, in the PR/issue body.
- **Screenshot**: following this repo's own precedent (issue #1 / PR #2), a terminal screenshot of
  the green `make lint && make typecheck && uv run pytest` run, committed to an orphan
  `assets/craigslist-adapter-screenshot` branch and linked via a `raw.githubusercontent.com` URL —
  there's no live Craigslist account to screenshot an actual posting.

## Issue + PR pair

Mirrors this repo's existing issue #1 → PR #2 convention:

- **Issue**: proposes the feature, states the two structural tensions and their resolutions
  up front, and lists exactly which facts need a maintainer's live-account confirmation.
- **PR**: implements the design above, references "Addresses #N", includes the confidence table,
  test plan, and screenshot. Branched from `upstream/master`, touching only new files plus the
  additive registry/docs/test edits — should apply cleanly.
