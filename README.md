# apple-refurb-watcher

Watches Apple's refurbished store for the 14" and 16" MacBook Pro (Italian store,
apple.com/it) and emails on any change: new listing, price change, or removal. Runs as
a GitHub Actions job, not on a local machine.

## Targets

Default targets:

- 16" MacBook Pro: `https://www.apple.com/it/shop/refurbished/mac/16-macbook-pro`
- 14" MacBook Pro: `https://www.apple.com/it/shop/refurbished/mac/14-macbook-pro`

Each target is fetched, parsed, and diffed independently. Override with `REFURB_URLS`
(comma-separated URLs) or the legacy single-URL `REFURB_URL`. Label and tag are derived
from the URL slug for custom targets.

## Pipeline

1. Fetch the listing page HTML with a browser user agent (Apple blocks the default
   urllib user agent).
2. Extract the `window.REFURB_GRID_BOOTSTRAP` JSON embedded in the page.
3. Filter `tiles` to the target's model using `selectedGridFilters`. Apple ships the
   full catalog in the page and filters it client side; this replicates that filter.
4. Build a snapshot keyed by part number: `{part: {title, price, url}}`.
5. Diff against the previous snapshot in `state.json`.
6. On any change, send one email covering all targets, then persist the new snapshot.

No third-party dependencies. Standard library only.

## Email format

Plain text, not HTML. One section per target:

```
[16-MBP] 16" MacBook Pro
Listing page: https://www.apple.com/it/shop/refurbished/mac/16-macbook-pro

NEW (1):
  - <title> - EUR <price> [<part number>] [<tag>]
    <product url>

PRICE CHANGED (1):
  - <title> [<part number>]
    EUR <old price> -> EUR <new price>
    <product url>

REMOVED (1):
  - <title> - EUR <price> [<part number>]
```

New and price-changed items link to the product page. Removed items do not: the page
is gone once Apple delists it. The subject line carries the same `[16-MBP]` / `[14-MBP]`
tags plus counts, for example `Apple Refurb: [16-MBP] +1 new | [14-MBP] ~1 price`.

## Watchlist keywords

Three configurations are flagged and exempted from the price cap (`MAX_PRICE`) regardless
of price. Defined in `watchlist_tags()` in `watcher.py`:

- `NANOTEXTURE`: title contains "nanotexture" (the nano-texture glass display option).
- `HIGH-SPEC-14-CORE ~2500`: a 14" model with CPU cores > 15 and GPU cores > 15, priced
  between EUR 2300 and 2700.
- `ALERT-TARGET-14-M5PRO-15-16`: the target config - 14" MacBook Pro, Apple M5 Pro chip,
  CPU 15-core, GPU 16-core, in **any color** and **with or without nanotexture** (matched on
  chip/cores text, so every color and any reissued SKU is caught; e.g. `FGDR4T/A` Nero
  siderale, `FGDN4T/A` Argento, `G1ML0T/A` nanotexture). Not the higher 18-core/20-core M5
  Pro tier already covered by `HIGH-SPEC-14-CORE`. When this fires, the email subject is also
  prefixed with `TARGET CONFIG IN STOCK -` so it's visible in a notification preview.

A match adds the tag in brackets next to the listing in the email, even when the price
exceeds `MAX_PRICE`.

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `REFURB_URLS` | comma-separated listing URLs | 14" and 16" MacBook Pro, IT store |
| `REFURB_URL` | legacy single-URL override, used only if `REFURB_URLS` is unset | none |
| `GMAIL_USER` | sending Gmail address | required for email |
| `GMAIL_APP_PASSWORD` | 16-character Gmail app password | required for email |
| `MAIL_TO` | recipient address | required for email |
| `MAX_PRICE` | EUR cap for newly-added items, bypassed by watchlist matches | 2600 |
| `STATE_FILE` | snapshot path | `state.json` next to `watcher.py` |

Without all three of `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `MAIL_TO` set, the job still
scrapes and updates state. It skips sending email and logs that email is disabled.

## Scheduling

GitHub's native `schedule:` trigger is best-effort. Observed behavior on this repo:
with a 15-minute cron, actual gaps between runs ranged from 55 to 207 minutes, not 15.
This is GitHub throttling scheduled triggers, not a defect in this workflow.

To get a real 5-minute cadence, the job re-triggers itself: after committing the
snapshot, it sleeps 300 seconds, then calls `gh workflow run watch.yml` using the
built-in `GITHUB_TOKEN`. The hourly `schedule:` entry in the workflow exists only as a
backup, to restart the chain if it breaks (a run fails, gets cancelled, and so on).

This keeps the job running close to continuously. GitHub Actions minutes are
unrestricted for public repositories on GitHub-hosted runners, which is why this repo
is public. On a private repo, continuous 5-minute runs would exceed the 2,000
minute/month free quota (estimate: about 8,600 minutes/month at one billed minute per
run).

## Run-health check

After `watcher.py` runs, a "Verify run health" step inspects its log:

- Fails the job if the watcher crashed or every target failed to fetch or parse.
- Warns, without failing, if some targets had a transient error but others succeeded.
- Fails the job if no email was sent and the log does not explicitly confirm why
  ("No changes since last check" or "Email disabled").

This distinguishes "nothing changed" from "a check silently broke" in the Actions run
history.

## One-time setup (email)

1. Enable 2-Step Verification on the sending Gmail account (myaccount.google.com,
   Security).
2. Create an app password (Security, App passwords): a 16-character code.
3. Set three repository secrets:

```bash
gh secret set GMAIL_USER
gh secret set GMAIL_APP_PASSWORD
gh secret set MAIL_TO
```

## Run manually

```bash
gh workflow run watch.yml
```

Or: Actions tab, apple-refurb-watch, Run workflow.

## Run locally

```bash
GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=xxxx MAIL_TO=dest@gmail.com python3 watcher.py
```
