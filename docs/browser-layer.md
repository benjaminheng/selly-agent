# The browser layer

Most marketplaces have no API for a third party, so the agent works them the way
a person would: in the seller's own logged-in Chrome. This is the layer that
does it — read buyers' messages into durable rows, send replies back, and fill a
listing form — without the LLM ever touching a page for the ordinary cases.

It is **optional in effect**: a machine with no Node, or no Chrome running, keeps
the daemon and the carousell.ai rail working, with the browser lanes reporting
themselves unavailable rather than reporting empty marketplaces.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for where this sits in the whole, and
[`tool-surface-and-passes.md`](tool-surface-and-passes.md) for the *other* browser
seam — the Playwright server a publish pass drives itself, which is separate from
everything below.

## The warm Chrome

One dedicated Chrome profile, one browser, CDP listening on the loopback
interface. The profile is the point: the seller's everyday Chrome is never
driven, and the marketplace sessions the agent uses persist across restarts.

**The daemon never launches it.** One profile admits exactly one Chrome — a
second launch on a live profile either hangs or opens read-only — so supervision
belongs to launchd (and in dev, to the person at the keyboard). `browser/chrome.py`
holds only the parts around that:

- `is_ready(port)` — an HTTP GET of Chrome's own `/json/version`. The **only
  network I/O in the layer**.
- `launch_command(port)` — the argv. Includes
  `--disable-backgrounding-occluded-windows` (a window the seller has covered
  otherwise counts as hidden, and every send would raise it again) and a pinned
  window position and size.
- `clear_stale_locks()` — removes the `Singleton*` files a SIGKILLed Chrome
  leaves behind, which would hang the next launch. Only safe once the probe says
  nothing is answering.
- `bring_up_hint(port)` — the dev-mode instruction, printing the full command.

These are the bring-up helpers. The lanes themselves do **not** probe CDP: an
absent or logged-out Chrome surfaces through the read path below, as a failed read
or a `logged_out` verdict.

## The client

`browser/client.py` is the daemon's own Playwright MCP client: JSON-RPC over a
**stdio subprocess**. No socket, no port, nothing to authenticate, and nothing
else on the machine that could connect to it. The shape follows `rail/client.py`
— typed errors, timeouts as named constants, and **no internal retry** (a lane
backs off; a hot retry against a marketplace is the anti-automation tell).

Errors are three kinds because the responses differ:

| error | means | response |
| --- | --- | --- |
| `BrowserUnavailable` | the layer cannot run at all — no `node`/`npx`, or the server died at startup | skip the browser lanes, one needs-me notice, daemon runs on |
| `BrowserTransportError` | the server was there and the exchange failed — exited mid-call, timed out, unparseable frame | counted as a failed read / a send that did not happen |
| `BrowserToolError` | the tool ran and failed — a selector matched nothing, a navigation was refused | the browser is healthy; the action is not |

`ensure_available(command)` is a `shutil.which` check the factory runs *before*
spawning, so absence is reported as absence with an install hint, rather than
surfacing later as a failed read or a send that reserved pacing for nothing.

Three client behaviours are load-bearing:

- **One tab, held by identity, not index.** `ensure_tab` opens the client's own
  tab once; because the daemon owns the server process exclusively, that tab stays
  the current one. Nothing ever selects a tab by index or guesses one by host —
  indices renumber whenever any tab opens or closes.
- **`ensure_frontmost` before typing.** Chrome routes key events only to a
  visible renderer, and a tab is visible only when it is its window's active tab.
  Filling a text box still works on a background tab, which is what makes the
  failure quiet: the text lands, the key that would commit it never arrives, and
  nothing errors. Only sends that need a real key press pay this — reads run fine
  in the background, which is what keeps the read lane out of the seller's way.
  Selecting is by index, so the page is read back afterwards; a tab that came
  forward and is not the page we navigated to belongs to something else, and the
  client abandons its handle and raises rather than typing into it.
- **`evaluate` never sets a value.** With a target it is a locate-and-read, plus
  the one market-supplied submit. The text a buyer sees is always typed as real
  input.

## One Chrome, three actors

The read lane, the reply sink, and a browser-driving pass all reach the same tab,
and the first two genuinely overlap (a reply can be sent while the lane is
mid-read). Two mechanisms keep that safe:

- **A re-entrant mutex on the client**, taken for whole *operations* rather than
  single calls: `with client.exclusive()` wraps navigate-then-read and
  locate-then-type-then-verify, so no other actor can navigate away mid-sequence.
- **The lane yields entirely** while a browser-touching pass is queued or
  running (`browser_pass_running`). A rail publish is not a reason to yield; a
  browser publish holds the tab for minutes where the lane holds it for seconds.
  Worst case the lane notices a buyer message one tick late.

## Market adapters

`browser/markets/` is the per-market seam. Everything above it — client, read
lane, reconcile, sink — depends only on the `MarketAdapter` protocol, so a new
marketplace is a new module plus a registry entry, not edits threaded through the
layer. The same split `channel/` uses for providers.

| field | what it carries |
| --- | --- |
| `conversations_list_js` | which conversations exist → `{conversations: [...]}` or `{error: …}` |
| `conversation_tail_js` | the open conversation's trailing bubbles, or `null` to abstain |
| `login_js` | `{state: logged_in \| logged_out \| unknown}` |
| `chat_message_submit_js` | **how a message is committed** — empty means a real key press |
| `listing_id_pattern` | where a listing's id sits in a permalink, one regex group |
| `composer` | shipped selector defaults, by step |
| `publish_skill` | the skill holding this market's publish recipe |
| `system_handles` | rows an inbox read must never treat as a buyer |

`chat_message_submit_js` is the one genuinely per-market *decision* rather than a
per-market fact. Empty is the safe default: submitting falls back to a real key
event, indistinguishable from a person, at the cost of pulling the seller's
window forward on every reply. Supplying it trades that cost for a keystroke
dispatched from the page, which carries `isTrusted: false` — a signal on the
seller's own account, so it belongs to a market someone has decided that for.

The JS artifacts are the layer's only DOM knowledge, and they are written
**class-agnostically** — marketplaces ship hashed CSS classes that churn every
deploy — locating by role, by href shape, and (for message direction) by geometry.

### Registry vs. adapter

`data/marketplaces.json` says a market is `connector.type == "browser"`;
`browser_markets()` returns the active ones in registry order. Several entries
qualify today (Facebook, Mercari, OfferUp, Poshmark, Craigslist) and **only
Carousell has an adapter** — the lane skips a registry entry with no adapter, so
listing a market in the registry does not make it read. Page URLs come from the
registry's `urls` templates and the region→host `domains` map, never from a guess:
an unrecorded template resolves to `None` and the caller reports that.

### Carousell, specifically

- **The conversation list is Carousell's own JSON API**, fetched from the page so
  the session cookie rides along. The inbox DOM cannot supply it: its rows are
  `div[role="button"]` with hashed classes and carry no link, id or data
  attribute, so a conversation there has no addressable identity at all. The API
  also *fails with a status code*, where a DOM read that finds nothing looks
  exactly like an empty inbox. Fetching it marks nothing read.
- **The thread id is `legacy_offer_id`, not `id`.** `id` is a 32-bit integer
  server-side and has wrapped, so a new conversation reports a negative one —
  which in the chat URL is a different conversation.
- **Message history is DOM work**, because chat lives in a separate service. The
  reader scopes to the single scrollable pane, then keeps only *inline rounded*
  containers: the header's "Online 11 days ago", profile cards, system notices,
  and Carousell's quick-reply suggestion chips are all block/flex with square
  corners. A chip is indistinguishable from a buyer message by text alone, and
  recording one would have the agent answering itself.
- **Direction is geometry.** An outbound bubble hugs the right edge, inbound the
  left; anything roughly centred is reported `center` so the caller ignores it.
- **The login probe is three-state and never guesses `logged_out`** — a false
  `logged_out` tells a signed-in seller to re-authenticate and stops their market.
  An auth-gated control (inbox / sell) proves `logged_in`; a login control with no
  such marker proves `logged_out`; anything else is `unknown`.
- **There is no send-button selector**, because there is nothing addressable to
  click: the send icon's ancestors are undecorated elements with no role, no
  label and no cursor change, while the message box handles Enter itself.

## The read lane

`browser/inbox.py`, on the `inbox_read` scheduler task. One tick asks each
browser market which conversations exist, opens the ones that look like they
moved, and reconciles each tail against the rows already stored — a navigate and
one JS evaluate per thread, **no model turns at all**. That is what lets the reply
pass above it stay browser-free: by the time it runs, what the buyer said is
already state.

Per market, per tick: navigate the inbox → login probe → conversation list →
for each row, adopt or match a thread, skip or open it, reconcile → one
`browser.read` event.

**Three rules keep it honest:**

1. *A market that cannot be seen must never look like a market with no news.*
   Failed reads are counted per market; `browser_blind_after` consecutive
   failures raise one needs-me notice. A conversation whose message list could
   not be found counts as blindness too — it would otherwise pass for "this buyer
   said nothing new", which is how a buyer gets stranded.
2. *The skip gate is an optimization, never a correctness input.* A thread whose
   last stored message still matches the list's preview is left closed — but
   every `inbox_full_sweep_every`-th tick opens every active thread regardless, so
   a wrong preview match costs one sweep interval of latency and nothing more.
   Anything unread, anything whose last message we don't hold, and anything never
   read is always opened.
3. *Reading never advances the reply cursor.* Only a committed reply does, so a
   crash between seeing a message and answering it leaves the buyer eligible
   rather than silently handled.

**Adoption.** A buyer writing about one of our listings for the first time gets a
thread only if three things hold: they approached us (`offer_type == received`,
not an offer we made), the conversation names a listing we recognise by id, and we
know their handle. Anything less is left alone and emits `browser.unmatched` with
*which* check failed (`we_offered`, `no_handle`, `unknown_listing`, `two_items`) —
that event is what answers "why is nobody answering this buyer", and one label for
all of them would make the ordinary case (a listing the seller made outside the
agent) read like the alarming one (two items claiming the same listing).

**Scam pre-scan.** Every inbound row is scanned by the deterministic offline
engine as it is written, so the verdict is on the row before any model sees the
text.

Three notices, each queued at most once per condition and cleared on recovery:

| condition | notice |
| --- | --- |
| `browser_blind_after` failed reads on a market | can't read your `<market>` inbox — check Chrome is running and logged in |
| the login probe says `logged_out` | that market's session is logged out; reading stopped until you log back in |
| `BrowserUnavailable` | the browser can't be driven at all; browser markets paused, the rail unaffected |

The reply lane (`reply_lane`, every 10s) is a sibling: it claims every waiting
thread into **one** coalesced reply pass, refuses to enqueue a second while one is
in flight, and auto-refires nothing — eligibility comes from the rows, so a failed
pass's threads are simply picked up next tick.

## Reconcile

`browser/reconcile.py` — pure functions, no I/O. **The rule is reconcile, not
infer**: the tail is read as ground truth and compared against the stored rows,
and whatever is not stored is new. No memo of what a previous read rendered is
kept anywhere; the state that decides is the state that persists.

- **Message ids are derived from content**, because the chat DOM exposes none:
  `<direction>|<sha256 prefix>|<occurrence>`. Occurrence numbering, counted from
  the stored copies, keeps ids unique across repeats and stable across reads.
- **The tail is aligned as a trailing window.** The longest suffix of the stored
  rows that matches the tail's opening is the region both sides agree on; only
  what follows is new. Counting copies of each text instead would swallow a
  repeat whose earlier copy has scrolled out of the window — the stored count
  exceeds anything an 8-bubble tail can still show, so a buyer's new "ok" would
  read as already handled.
- **Alignment is truncation-tolerant.** A reader may cap how much of a bubble it
  returns, so a long stored reply must still match its cut-short read-back;
  otherwise the bubble records as an outbound message we never wrote — a phantom
  manual seller reply, which silences the thread. Texts under 200 normalized
  characters must match exactly: "ok" opening "ok, deal" is a coincidence, not a
  truncation.
- **Alignment is agnostic about who wrote a row**, so our own committed replies
  and the seller's manual ones reconcile the same way — already-recorded outbound
  text, matched and not recorded twice.
- **`classify_tail` drops what nobody said**: separator rows (times, "Yesterday")
  and anything centred, which is a system banner or an offer widget. Keeping one
  would record it as a message and, worse, let it stand as "someone answered".
- **`preview_matches`** decides only whether to *skip* opening a thread, matching
  an inbox row's truncated preview against a stored message. Its wrong answers
  cost latency, never a stranded buyer, because the full sweep backstops it.

## The send

`browser/sink.py` fills the `ReplySink` seam `send_reply` sends through. One call
does the whole bracket: navigate the recorded thread URL → (bring the tab forward,
if this market sends with a real key) → locate the composer → fill it in one go
→ commit → stamp the intent → confirm by reading our own words back off the page.

The text is filled whole rather than typed character by character, so a reply
containing a newline cannot commit part-way through itself and send half a
message. Verification is strict: "no error from the key press" is not success — a
refused validation, a composer that silently cleared, or a chat that ignored the
key because it thought the box was empty all look like success from outside. Only
our own words in an outbound bubble count.

**The two failure shapes are treated oppositely, because the safe response to
each is the opposite:**

| | nothing was sent | sent, unconfirmed |
| --- | --- | --- |
| how it happens | composer not located, page refused the commit, browser error before the commit | the commit was accepted and the read-back failed or found nothing |
| intent status | stays `pending` | `sent_unverified` |
| the sink raises | `SendNotAttempted` | `SendUnverified` |
| `send_reply` returns | `send_failed` | `send_unverified` |
| what happens next | safe to retry | **never re-driven** |

Everything before the commit fails closed, so "nothing was sent" is a guarantee
and not a hope. Past the commit nothing may retry and nothing may claim the send
did not happen — because the one thing worse than an unconfirmed message is the
same message twice.

An unconfirmed send is then handed off, not resolved in code:

1. While it is open, `reserve_reply` refuses any fresh send on that thread with
   `unverified_open` — no caller can talk past it, and no second intent or pacing
   row is minted.
2. The `stale_intent_sweep` task folds the intent as `unconfirmed` past its grace
   window (600s, held well above the pacing delay ceiling so a merely-jittered
   send can never look like a stall) and opens an escalation. The thread becomes
   `escalated`, which is the gate from then on.
3. Only the seller can settle it, by looking at the real chat. If the message is
   there: resolve the escalation and reactivate the thread. If it is not: resolve
   and reactivate **first** — sends are refused while a thread is escalated — then
   send again. The framing lives in the `seller-comms` skill.

## Selectors and the heal cache

Selectors ship as code, because a fresh install should not pay a vision
round-trip to find a message box that was known-good at release. The `ui_cache`
table sits **over** those defaults as a heal overlay: when a marketplace moves a
control, whatever re-found it is recorded and used from then on, and a later
release refreshes the defaults underneath. So self-healing never waits on a
release, and a release never overwrites what an install has learned.

- **Order is cache-then-shipped.** A stale row is skipped rather than tried, and
  a cache row that misses is counted *even when the shipped default then works* —
  otherwise a heal gone bad is probed on every send forever. Three failures retire
  it, so the row stops costing anything with nobody invalidating it by hand.
- **Stale is a miss, never "act anyway."** A row is stale when it has failed
  three times, carries no page-URL guard, or has gone 30 days unverified. Every
  stale answer degrades to the slow path exactly as a miss would.
- **Resolving is locate-only and must match exactly one visible element on the
  right page.** None means absent; several means acting would be a guess — made
  once per send, silently, on the account-sensitive path. A page guard is
  mandatory: recording a row without one is refused, since it could never be
  trusted.
- **The table holds locating strings and timestamps only** — never a value, a
  price, or an address.

## Events

| kind | when |
| --- | --- |
| `browser.read` | one market's tick: rows listed, threads opened, rows recorded, unreadable count, whether it was a full sweep |
| `browser.inbound` | one message folded into a durable row, with its scam verdict |
| `browser.thread_new` | a buyer's conversation adopted as a thread |
| `browser.unmatched` | a conversation deliberately not adopted, and which check failed |
| `browser.blind` | a failed read, with the running count and the reason |
| `browser.login` | the login probe answered `logged_out` |
| `browser.unavailable` | the browser cannot be driven at all |
| `browser.send` | a send's outcome: `sent`, `refused`, `unverified`, `browser_error` |
| `browser.heal` | a selector candidate that did not resolve, and where it came from |

All are `info` level — none is demoted to `routine`, so all of them show in a
default `inspect` view. See [`observability.md`](observability.md).

## Configuration

| key | default | what it does |
| --- | --- | --- |
| `chrome_cdp_port` | `9222` | the warm Chrome's CDP port on loopback |
| `playwright_mcp_cmd` | `null` | override the server command; `null` means `npx --yes @playwright/mcp` against the CDP endpoint |
| `inbox_read_interval_sec` | `300.0` | how often the read lane ticks |
| `inbox_full_sweep_every` | `6` | every Nth tick opens every active thread; `1` disables the skip gate |
| `browser_blind_after` | `3` | consecutive failed reads before the needs-me notice |

Lane counters (tick count, consecutive failures, which notices are already
queued) live **in process** on purpose: they are all counters, and a restart
re-arming them errs toward reading more rather than less.

## Adding a marketplace

1. **A registry entry** in `data/marketplaces.json`: `connector.type: "browser"`,
   `status: "active"`, the `domains` map for its regions, and `urls` templates for
   at least `inbox` and `thread`.
2. **An adapter module** under `browser/markets/`, exporting the JS artifacts,
   the composer defaults, the listing-id pattern and its system handles — then one
   line in that package's `_ADAPTERS` registry.
3. **Decide the submit mechanism.** Leave `chat_message_submit_js` empty unless
   someone has decided that market's account can afford a page-dispatched
   keystroke.
4. **A publish recipe skill**, if the market should be publishable, pointed at by
   the registry's `listing_flow`.

Nothing else in the layer changes: the read lane, reconcile, the sink and the
selector cache are all written against the protocol.
