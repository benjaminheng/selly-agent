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
# carousell.py's LOGIN_JS for why. logged_out requires a login CONTROL carrying login-labeled text,
# never bare body text (a stray "log in" mention elsewhere on the page must not count) — best
# effort: Craigslist's login lives at accounts.craigslist.org; a signed-in page links to the
# post-login account home or a "log out" action, a signed-out one offers a labeled login link.
# Needs confirming against a live account.
LOGIN_JS = """() => {
  try {
    const loggedIn = !!document.querySelector(
      'a[href*="accounts.craigslist.org/login/home"], a[href*="/login/home"], a[href*="logout"]'
    );
    if (loggedIn) return { state: 'logged_in' };
    const loginControl = document.querySelector('a[href*="accounts.craigslist.org/login"]');
    const labeled = loginControl && /\\blog\\s?in\\b/i.test(loginControl.textContent || '');
    if (labeled) return { state: 'logged_out' };
    return { state: 'unknown' };
  } catch (e) {
    return { state: 'unknown' };
  }
}"""
