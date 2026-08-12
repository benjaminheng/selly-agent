# Discord as a second channel provider

Status: approved for planning
Date: 2026-08-12
Target: upstream `benjaminheng/selly-agent` (this repo's `upstream` remote), via one GitHub issue
and one PR from `jgyy/selly-agent` (`origin`)

## Why

Telegram is the only channel provider today. `docs/channels.md` already names the extension seam
("a sibling package... rather than a rewrite") and lists Slack/iMessage as hypothetical examples.
This adds Discord as a real second provider, exercising that seam for the first time and proving
it holds.

## Scope

Full behavioral parity with the Telegram provider, reusing the provider-agnostic core
(`channel/fastpaths.py`, `channel/outbound.py`, `channel/routing.py`, `channel/prompt.py`)
**unchanged**:

- bind (connect a bot, authenticate by nonce)
- free-text send/receive
- the deterministic fast paths (`/pause /resume /status /catchup /selly`)
- native buttons for the control row (pause/resume, what-needs-me, settings approve/cancel/undo,
  the first-listing CTA's Skip)
- photo/image receiving
- typing indicator
- notice delivery (the drain lane) and catchup

**Non-goals**, called out explicitly in the issue as possible follow-ups: slash-command
registration (plain text works fine over DM, same as Telegram), voice, reactions, threads,
binding more than one channel at a time (matches the existing singleton schema), sharding
(unnecessary at one-bot-one-seller scale).

## The platform constraint that shapes everything

Selly runs locally with no public endpoint (`docs/channels.md`, README: "It runs locally on your
machine"), and `tests/guard/test_stdlib_only.py` allows only `psutil`, `segno`, `pillow`,
`pillow_heif` as runtime dependencies — no HTTP/WebSocket client library.

Telegram's long-poll (`getUpdates`) has no Discord equivalent for receiving messages. A bot can
only learn about new DMs over Discord's **Gateway**, a persistent outbound-initiated WebSocket
connection (works fine from behind NAT — the client always dials out). Discord's HTTP
Interactions endpoint (webhook-style) is not usable here: it requires a publicly reachable URL,
which a local install does not have, and it only covers slash-command/component interactions
anyway, not arbitrary DM text.

Consequence: this PR's one genuinely new primitive is a **hand-rolled RFC 6455 WebSocket client**
in pure stdlib (`socket` + `ssl`), the same spirit as `channel/telegram/transport.py` hand-rolling
HTTP over `urllib` rather than pulling in `requests`.

## Bind flow

Two Discord platform facts drive the shape here:

1. A bot can only DM a user once bot and user share a server — there is no way for a user to
   "just message a bot" cold.
2. There is no Telegram-style deep link that opens a prefilled chat with arbitrary text
   (`t.me/bot?start=<payload>` has no Discord analog for DMs).

So bind becomes two steps instead of Telegram's one:

1. `selly-agent connect discord` reads a bot token from the Discord Developer Portal — same
   interactive-getpass / piped-stdin shape as `connect telegram` — and POSTs it to
   `/control/connect-discord`. The daemon validates it with `GET /users/@me` (Discord's `getMe`),
   writes it 0600, mints a one-time nonce, arms the channel row (`adapter='discord'`).
2. The daemon returns an OAuth2 invite URL requesting **zero permissions**
   (`https://discord.com/oauth2/authorize?client_id=<id>&scope=bot&permissions=0`) — the bot only
   ever DMs, never touches a guild channel, so it needs no guild grant at all. This is a smaller
   trust footprint than a typical Discord bot invite and is worth calling out in the PR
   description.
3. The CLI renders that URL as a terminal QR (reusing `qr.render_terminal` — no new dependency)
   plus the link, and the nonce itself, with phone-and-desktop-agnostic guidance: *"Add the bot to
   any server — even a private one just for you — then send it a direct message containing
   exactly this code: `<nonce>`."*
4. The Gateway connection is already live in an `awaiting-bind` state (mirroring the poller's
   off/awaiting-bind/bound states) and watches for a DM `MESSAGE_CREATE` whose content exactly
   equals the nonce. Same "possession of the nonce authorizes bind, first match wins, a stranger
   mid-bind can never be adopted" property as Telegram's `/start <nonce>` — just against a plain
   DM instead of a `/start` payload.
5. The CLI polls `/control/channel-status`, same timeouts/exit codes as Telegram
   (300s interactive / 120s piped). `connect_cli.bind_flow`'s polling/timeout/printing logic is
   ~90% identical between providers; factor that shared part into one parametrized function rather
   than duplicate it (real duplication being consolidated, not a speculative abstraction).

## New files

Mirrors `channel/telegram/` file-for-file:

- **`channel/discord/transport.py`** — REST client: send message, create/open a DM channel,
  `GET /users/@me`, `GET /oauth2/applications/@me`, trigger typing, download an attachment URL.
  Plus a pure `_normalize` (Gateway dispatch payload → the shared event shape). Network-allowlisted
  (added to `NETWORK_ALLOWLIST`), same as Telegram's transport.
- **`channel/discord/ws_client.py`** — the RFC 6455 client: TLS handshake (`Upgrade: websocket`),
  masked frame encode / frame decode (text, ping/pong, close, continuation), a
  `socket.socketpair()`-testable frame codec kept separate from the socket I/O so it is unit
  testable with no network at all. Deliberately provider-scoped — no speculative shared "core WS"
  module until a second WS-based provider actually needs one, matching `docs/channels.md`'s own
  stance that "the receive model is deliberately not abstracted."
- **`channel/discord/gateway.py`** — the session loop, this PR's analog of `poller.py`: IDENTIFY →
  heartbeat-with-ack-tracking → RESUME-on-drop, reusing the same 5s→60s backoff-on-error pattern
  the Telegram poller already uses for transport failures. Dispatches `MESSAGE_CREATE` (and
  `INTERACTION_CREATE` for button taps — these arrive over the *same* Gateway connection, so
  buttons never need a separate HTTP interactions endpoint) into the existing
  ingest → fast-path-dispatch → routing pipeline, unchanged.
- **`channel/discord/bind.py`** — mirrors `telegram/bind.py`: validate + prove the token, persist
  it, arm the nonce, return `{bot_username, application_id, invite_url}`.
- **`channel/discord/commands.py`** — renders the core's `(label, token)` control spec as Discord
  message `components` (buttons), mirroring `telegram/commands.py`'s inline-keyboard builder. No
  `BOT_COMMANDS` slash-command list (out of scope).
- **`channel/discord/outbound.py`** — `make_deliver(config)` / `make_typing(config)` callables the
  core outbound policy calls, mirroring `telegram/outbound.py`.
- **`channel/discord/provider.py`** — `is_configured()` / `start()` / `handle.shutdown()`,
  mirroring `telegram/provider.py`; registered in `daemon.py`'s `providers={...}` map.

## Two small, real core touch-ups (discovered while reading, not speculative)

- `channel/fastpaths.py` hardcodes `source="telegram"` on pause/resume — a provider name leaking
  into the provider-agnostic core. Generalize to `source="channel"`.
- `store.arm_bind` hardcodes `'telegram'` in its INSERT. Add an `adapter` parameter defaulted to
  `"telegram"` so the existing Telegram call site is untouched; Discord's `bind.py` passes
  `adapter="discord"`.

## Data model

One additive migration, `0011_discord_channel.sql` (migrations are never edited once shipped):
widen `channel.adapter CHECK (adapter IN ('telegram'))` to
`CHECK (adapter IN ('telegram', 'discord'))`. SQLite has no `ALTER TABLE ... DROP/ADD CONSTRAINT`,
so this is the standard create-new-table-with-the-new-CHECK / copy / drop-old / rename dance. No
new columns: `chat_id` (a Discord DM channel snowflake fits SQLite `INTEGER`/int64 fine) and
`bot_username` are generic enough to reuse as-is; `commands_hash` stays unused for Discord (no
slash-command registration in scope) and that's fine — it's nullable already.

## Wiring (small, additive edits to existing files)

- `daemon.py` — `providers={"telegram": telegram_provider, "discord": discord_provider}`
- `config.py` — `discord_api_base: str = "https://discord.com/api/v10"` (and the Gateway URL,
  resolved via `GET /gateway/bot` at connect time so the session-start-limit info is available too)
- `secrets.py` — `read_discord_bot_token` / `write_discord_bot_token`
- `connect_cli.py` — `connect discord` subcommand sharing the refactored `bind_flow`
- `cli.py` — the `discord` connect subparser
- `setup_cli.py` — `_offer_discord`, mirroring `_offer_telegram`
- `healthcheck.py` — the Discord-equivalent guidance line
- `tests/guard/test_stdlib_only.py` — add `channel/discord/transport.py` and
  `channel/discord/ws_client.py` to `NETWORK_ALLOWLIST`

## Testing

Mirrors the existing suite's philosophy: real code driven against an in-process fake, not mocks.

- `tests/fake_discord_gateway.py` — a fake REST server (stdlib `ThreadingHTTPServer`, like
  `fake_telegram_api.py`) **plus** a minimal real server-side WebSocket endpoint, so
  `gateway.py`/`ws_client.py` are exercised exactly as they run in production.
- A standalone frame-codec test using `socket.socketpair()` (a real, local, TLS-free byte pipe) —
  round-trips encode/decode against itself and against RFC 6455's published masking test vector.
  This is the highest-risk new code (a subtly wrong mask/length calculation would misbehave against
  real Discord in ways hard to diagnose from outside), so it gets the most direct test.
- `test_channel_discord_bind.py`, `test_channel_discord_transport.py`,
  `test_channel_discord_gateway.py` mirroring the shape of the existing Telegram test files.

## Docs

- `docs/channels.md` — the "a sibling package" line becomes real; add a `## Discord` section next
  to `## Bind (Telegram)`.
- `README.md` — "Interact with Selly using: Telegram, Claude Code" gains Discord.
- `docs/ARCHITECTURE.md` — two mentions of Telegram as *the* channel generalize to "a channel
  provider."
- The GitHub issue and PR bodies carry a **Mermaid** sequence diagram of the bind flow (GitHub
  renders Mermaid inline in issue/PR markdown with no build step — unlike the repo's own
  `docs/*.pikchr` diagrams, which need local rendering, so Mermaid belongs in the PR body, not
  grafted into `docs/`).
- A terminal screenshot of the `connect discord` bind prompt (QR + guidance), analogous to what the
  Telegram QR PR (#28) would have shown.

## Live verification — explicitly deferred

Getting a real Discord bot token requires creating an application in Discord's Developer Portal,
which is account-bound and not something to do without a human's Discord account. Per the user's
choice, this PR ships **without** a live Discord round-trip: correctness is proven by the fake
REST/Gateway test doubles plus the frame-codec unit tests, and the PR description says so
explicitly rather than implying it was verified against real Discord. A live check is left to the
user or the upstream maintainer post-PR.

## Rollout: issue + PR, no merge conflicts

1. One GitHub issue on `benjaminheng/selly-agent` proposing the feature (checked: no existing
   Discord/Slack/channel issue or PR there today) — states the design summary above, the
   WebSocket/stdlib-only constraint, and the non-goals, so a maintainer can weigh in on scope
   before or independent of the PR.
2. One branch off the latest `upstream/master`, implemented as a sequence of logical, independently
   buildable-and-passing commits (per `AGENTS.md`'s version-control convention) — e.g. the migration
   + core touch-ups, then the WS client + its tests, then the Discord provider package + its tests,
   then the CLI/config/secrets/daemon wiring, then docs.
3. Immediately before opening the PR: rebase onto `upstream/master` again to catch any drift.
4. All edits to shared files (`daemon.py`, `config.py`, `secrets.py`, `connect_cli.py`, `cli.py`,
   `setup_cli.py`, `healthcheck.py`, `tests/guard/test_stdlib_only.py`) are additive/surgical
   (one new dict entry, one new function, one new allowlist line) rather than restructuring, which
   keeps conflict risk low even if upstream moves before merge.
5. PR opened from `jgyy/selly-agent` (`origin`) against `benjaminheng/selly-agent` (`upstream`),
   referencing the issue (`Closes #N`), with the Mermaid diagram and the terminal screenshot in the
   description.

## Risks / open technical details to verify during implementation (not guessed here)

- Exact Discord Gateway v10 opcode numbers, the IDENTIFY payload shape, and the current intents
  bitmask (`DIRECT_MESSAGES`, and whether the privileged `MESSAGE_CONTENT` intent is truly exempt
  for DMs under current Discord policy) will be confirmed against Discord's live developer
  documentation while writing `gateway.py`, not assumed from memory — a wrong bitmask or opcode
  fails closed (Discord disconnects the client) rather than silently, but it should still be gotten
  right the first time.
- Discord bot tokens embed the bot's user ID in their first segment (base64), which may be a
  simpler source of the application/client ID than an extra `/oauth2/applications/@me` call — a
  small implementation-time choice, not a design fork.
