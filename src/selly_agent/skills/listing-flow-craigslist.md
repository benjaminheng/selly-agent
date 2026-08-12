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
