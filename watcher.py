#!/usr/bin/env python3
"""
apple-refurb-watcher: watch Apple refurbished-store filter URLs and email on changes.

Pipeline (top-to-bottom in main()):
  1. Resolve the list of watched targets (label + tag + URL), default: 14" and 16" MacBook Pro.
  2. For each target, fetch the listing page and extract the embedded
     `window.REFURB_GRID_BOOTSTRAP` JSON, then filter its `tiles` to that target's model.
  3. Build a snapshot keyed by partNumber: {partNumber: {title, price, url}}.
  4. Diff each target's snapshot against the previous run (STATE_FILE) — added / removed /
     price change.
  5. If anything changed, email a plain-text, per-model report via Gmail SMTP (new/changed
     items link to their product page, removed items don't since the link is dead), then
     persist the snapshots.

Email is sent only when GMAIL_USER, GMAIL_APP_PASSWORD and MAIL_TO are all set;
otherwise the script still scrapes and updates state, and just logs that email is disabled.

Watchlist keywords: newly-added items are tagged (and exempted from MAX_PRICE) when they
match a config worth flagging regardless of the usual price cap - see watchlist_tags():
  NANOTEXTURE           title mentions the nano-texture glass display option
  HIGH-SPEC-14-CORE     a 14" model with >15 CPU cores and >15 GPU cores, priced ~2500 EUR
                        (a Pro/Max chip at a price it rarely reaches)

Configuration (environment variables):
  REFURB_URLS         comma-separated listing URLs to watch (default: 14" and 16" MacBook Pro,
                       Italian store); label/tag are derived from each URL's slug
  REFURB_URL          legacy single-URL override (used only if REFURB_URLS is unset)
  GMAIL_USER          sending Gmail address      (required to send email)
  GMAIL_APP_PASSWORD  16-char Gmail app password (required to send email)
  MAIL_TO             recipient address          (required to send email)
  MAX_PRICE           EUR cap for newly-added items to notify on (default: 2600)
  STATE_FILE          snapshot path (default: state.json next to this script)

Pure standard library: no pip install needed in CI.
"""

import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urljoin

# Default targets: 14" and 16" MacBook Pro filters on the Italian refurb store.
DEFAULT_TARGETS = [
    {
        "label": '16" MacBook Pro',
        "tag": "16-MBP",
        "url": "https://www.apple.com/it/shop/refurbished/mac/16-macbook-pro",
    },
    {
        "label": '14" MacBook Pro',
        "tag": "14-MBP",
        "url": "https://www.apple.com/it/shop/refurbished/mac/14-macbook-pro",
    },
]
# Legacy single-target label, used only to migrate a pre-multi-model state.json.
LEGACY_LABEL = '16" MacBook Pro'
# Newly-added items priced above this (EUR) are not notified; override via MAX_PRICE env.
DEFAULT_MAX_PRICE = 2600.0
# Apple blocks the urllib default UA; present a normal browser UA so we get the grid HTML.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
# Marker that precedes the JSON object holding the product grid on the page.
BOOTSTRAP_MARKER = "window.REFURB_GRID_BOOTSTRAP"


def log(msg):
    """Emit a timestamped, single-line debug log (no emojis, no verbosity)."""
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}] {msg}", flush=True)


def fetch_html(url):
    """Download the listing page and return its HTML as text."""
    # Build the request with a browser UA + Italian language so Apple serves the IT grid.
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it;q=0.9"}
    )
    # 30s timeout: fail fast rather than hang a CI run if Apple is unreachable.
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_bootstrap(html):
    """Return the REFURB_GRID_BOOTSTRAP JSON object parsed from the page HTML.

    Uses brace-counting (string-aware) instead of a regex so JSON containing
    literal '};' inside string values does not truncate the match.
    """
    # Locate the marker, then the first '{' that opens the JSON object.
    marker_at = html.index(BOOTSTRAP_MARKER)
    start = html.index("{", marker_at)
    depth = 0          # nesting level of '{...}'
    in_str = False     # whether the cursor is inside a JSON string literal
    esc = False        # whether the previous char was an unescaped backslash
    # Walk forward until the matching closing brace returns depth to zero.
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            # Inside a string: only track escapes and the closing quote.
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            # Outside a string: track quotes and brace depth.
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start : j + 1])
    raise ValueError("REFURB_GRID_BOOTSTRAP JSON not found or unbalanced")


def clean(text):
    """Normalize tile text: collapse non-breaking spaces and trim."""
    return (text or "").replace("\xa0", " ").strip()


def selected_filter_map(data):
    """Group the URL's selected filters into {dimensionKey: {allowed values}}.

    Apple ships the whole catalog in `tiles` and applies the URL filter
    (`selectedGridFilters`) client-side. The same key can appear twice (e.g.
    24gb and 36gb), which means OR within a key; different keys mean AND.
    """
    groups = {}
    # Apple's bootstrap JSON sometimes carries this key as an explicit `null`
    # (not just absent), which bypasses dict.get's default -> always fall back to [].
    for entry in data.get("selectedGridFilters") or []:
        for key, value in entry.items():
            groups.setdefault(key, set()).add(value)
    return groups


def tile_matches(tile, groups):
    """True if the tile satisfies every selected dimension (AND across keys, OR within)."""
    # Each tile describes itself under filters.dimensions, e.g. {"dimensionScreensize": "16inch"}.
    dims = tile.get("filters", {}).get("dimensions", {})
    return all(dims.get(key) in values for key, values in groups.items())


def build_snapshot(data, base_url):
    """Reduce the bootstrap JSON to {partNumber: {title, price, url}} for the selected filter."""
    # Replicate the page's client-side filtering so we only track the requested model.
    groups = selected_filter_map(data)
    snapshot = {}
    # Each tile is one refurbished product offer in the grid.
    for tile in data.get("tiles", []):
        part = tile.get("partNumber")
        if not part:
            # Skip non-product tiles (e.g. promo cells) that lack a part number.
            continue
        # Keep only tiles matching the URL filter (when the URL carries one).
        if groups and not tile_matches(tile, groups):
            continue
        # currentPrice.raw_amount is the machine-readable price, e.g. "679.00".
        price = tile.get("price", {}).get("currentPrice", {}).get("raw_amount")
        snapshot[part] = {
            "title": clean(tile.get("title", "")),
            "price": price,
            # productDetailsUrl is relative; resolve it against the site root.
            "url": urljoin(base_url, tile.get("productDetailsUrl", "")),
        }
    return snapshot


def label_from_url(url):
    """Derive a human label and a filter-friendly tag from a refurb listing URL's slug.

    E.g. ".../mac/14-macbook-pro" -> ('14" Macbook Pro', '14-MACBOOK-PRO').
    Only used for custom targets supplied via REFURB_URLS/REFURB_URL.
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    tag = slug.upper()
    match = re.match(r"(\d+)-(.+)", slug)
    if match:
        size, rest = match.groups()
        label = f'{size}" ' + rest.replace("-", " ").title()
    else:
        label = slug.replace("-", " ").title()
    return label, tag


def resolve_targets():
    """Return the list of {label, tag, url} targets to watch, from env or defaults."""
    urls_env = (os.environ.get("REFURB_URLS") or "").strip()
    if urls_env:
        urls = [u.strip() for u in urls_env.split(",") if u.strip()]
    else:
        single = (os.environ.get("REFURB_URL") or "").strip()
        urls = [single] if single else None
    if not urls:
        return DEFAULT_TARGETS
    targets = []
    for url in urls:
        label, tag = label_from_url(url)
        targets.append({"label": label, "tag": tag, "url": url})
    return targets


def load_state(path):
    """Load {"targets": {label: {part: item}}} from disk, or None if this is the first run.

    Transparently migrates a pre-multi-model state.json (a flat {part: item} dict)
    into the nested format, filed under LEGACY_LABEL, so existing baselines aren't
    treated as new listings just because the file shape changed.
    """
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "targets" in data:
        return data
    return {"targets": {LEGACY_LABEL: data}}


def save_state(path, state):
    """Persist the current {"targets": ...} state for the next run to diff against."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def diff_snapshots(old, new):
    """Return (added, removed, changed) part-number lists between two snapshots."""
    added = [p for p in new if p not in old]                                  # newly listed
    removed = [p for p in old if p not in new]                                # gone from store
    changed = [p for p in new if p in old and old[p]["price"] != new[p]["price"]]  # price moved
    return added, removed, changed


def within_cap(item, max_price):
    """True if the item's price is at or below max_price; unknown prices pass through."""
    # Suppress only confidently-over-cap items; if the price can't be parsed, don't hide it.
    try:
        return float(item.get("price")) <= max_price
    except (TypeError, ValueError):
        return True


# --- Watchlist keyword rules -------------------------------------------------
# Beyond the plain added/removed/changed diff, flag specific configs worth surfacing
# regardless of MAX_PRICE. Matches get a bracketed keyword tag in the email (subject
# and body) so a Gmail filter can search for them across runs, e.g. "NANOTEXTURE".

# Matches "CPU 18-core" / "CPU 18‑Core" (Apple mixes a plain hyphen and U+2011).
CORE_COUNT_RE = {
    "cpu": re.compile(r"CPU\s*(\d+)[^0-9A-Za-z]{1,2}core", re.IGNORECASE),
    "gpu": re.compile(r"GPU\s*(\d+)[^0-9A-Za-z]{1,2}core", re.IGNORECASE),
}
# "around 2500 EUR" for the 14" high-core-count watch below.
HIGH_SPEC_14_PRICE_BAND = (2300.0, 2700.0)


def watchlist_tags(item, label):
    """Return keyword tags for item configs worth flagging beyond the plain diff."""
    tags = []
    title = item.get("title", "")
    if "nanotexture" in title.lower():
        tags.append("NANOTEXTURE")
    if '14"' in label:
        cpu_match = CORE_COUNT_RE["cpu"].search(title)
        gpu_match = CORE_COUNT_RE["gpu"].search(title)
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            price = None
        if (
            cpu_match and gpu_match and price is not None
            and int(cpu_match.group(1)) > 15 and int(gpu_match.group(1)) > 15
            and HIGH_SPEC_14_PRICE_BAND[0] <= price <= HIGH_SPEC_14_PRICE_BAND[1]
        ):
            tags.append("HIGH-SPEC-14-CORE ~2500")
    return tags


# --- Email formatting (plain text only) --------------------------------------
# New/price-changed items link to their product page; removed items don't get a
# link since the page is gone once Apple delists it.


def format_added_line(part, item, tags):
    """Render one NEW-item line: title, price, part number, watchlist tags, product link."""
    price = item.get("price") or "n/a"
    tag_str = "".join(f" [{t}]" for t in tags)
    return f"  - {item['title']} - EUR {price} [{part}]{tag_str}\n    {item['url']}"


def format_changed_line(part, old_item, new_item, tags):
    """Render one PRICE CHANGED line: old -> new price and the product link."""
    tag_str = "".join(f" [{t}]" for t in tags)
    return (
        f"  - {new_item['title']} [{part}]{tag_str}\n"
        f"    EUR {old_item.get('price')} -> EUR {new_item.get('price')}\n"
        f"    {new_item['url']}"
    )


def format_removed_line(part, item):
    """Render one REMOVED-item line: title and price only, no link (page is gone)."""
    price = item.get("price") or "n/a"
    return f"  - {item['title']} - EUR {price} [{part}]"


def build_section_text(section):
    """Plain-text block reporting one target's added/changed/removed items."""
    added, removed, changed, old, new = (
        section["added"], section["removed"], section["changed"], section["old"], section["new"]
    )
    lines = [f"\n[{section['tag']}] {section['label']}", f"Listing page: {section['url']}"]
    if added:
        lines.append(f"\nNEW ({len(added)}):")
        lines += [format_added_line(p, new[p], watchlist_tags(new[p], section["label"])) for p in added]
    if changed:
        lines.append(f"\nPRICE CHANGED ({len(changed)}):")
        lines += [
            format_changed_line(p, old[p], new[p], watchlist_tags(new[p], section["label"]))
            for p in changed
        ]
    if removed:
        lines.append(f"\nREMOVED ({len(removed)}):")
        lines += [format_removed_line(p, old[p]) for p in removed]
    return "\n".join(lines)


def build_change_email(sections, new_target_intros):
    """Compose the (subject, body) plain-text email for a run that found changes."""
    run_label = f"Run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    subject_bits = []
    all_tags = set()
    for s in sections:
        bits = []
        if s["added"]:
            bits.append(f"+{len(s['added'])} new")
        if s["changed"]:
            bits.append(f"~{len(s['changed'])} price")
        if s["removed"]:
            bits.append(f"-{len(s['removed'])} gone")
        subject_bits.append(f"[{s['tag']}] {', '.join(bits)}")
        for p in s["added"] + s["changed"]:
            all_tags.update(watchlist_tags(s["new"][p], s["label"]))
    for label, tag, count in new_target_intros:
        subject_bits.append(f"[{tag}] now watching")
    subject = "Apple Refurb: " + " | ".join(subject_bits)
    if all_tags:
        subject += " | " + " ".join(f"[{t}]" for t in sorted(all_tags))

    body_parts = [f"Apple refurbished watcher - change report\n{run_label}\n"]
    for label, tag, count in new_target_intros:
        body_parts.append(f"\n[{tag}] {label}: now watching ({count} item(s) currently listed)")
    for section in sections:
        body_parts.append(build_section_text(section))
    return subject, "\n".join(body_parts)


def build_startup_email(targets, new_targets):
    """Compose the (subject, body) plain-text email for the very first run ever."""
    tags = ", ".join(f"[{t['tag']}]" for t in targets)
    subject = f"Apple Refurb watcher started: {tags}"
    lines = [f"Monitoring started for {len(targets)} target(s):\n"]
    for t in targets:
        count = len(new_targets.get(t["label"], {}))
        lines.append(f"\n[{t['tag']}] {t['label']} - {count} item(s) currently listed")
        lines.append(f"Listing page: {t['url']}")
    lines.append("\nYou will be emailed only when listings change.")
    return subject, "\n".join(lines)


def send_email(subject, body, user, password, recipient):
    """Send a plain-text email via Gmail SMTP over SSL."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(body)
    # Port 465 = implicit TLS; authenticate with the Gmail app password.
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.send_message(msg)


def main():
    # --- Read configuration from the environment ---
    targets = resolve_targets()
    state_file = os.environ.get(
        "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
    )
    gmail_user = (os.environ.get("GMAIL_USER") or "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD") or ""
    # Gmail shows app passwords as "abcd efgh ijkl mnop"; SMTP needs the bare 16 chars.
    gmail_pwd = gmail_pwd.replace(" ", "").strip()
    mail_to = (os.environ.get("MAIL_TO") or "").strip()
    email_enabled = bool(gmail_user and gmail_pwd and mail_to)
    # Newly-added items priced above this cap (EUR) are not notified, unless they
    # match a watchlist keyword (see watchlist_tags), which is always surfaced.
    max_price = float(os.environ.get("MAX_PRICE", str(DEFAULT_MAX_PRICE)))

    state = load_state(state_file)
    is_first_run = state is None
    old_targets = (state or {}).get("targets", {})

    new_targets = {}
    sections = []            # per-target change reports for targets seen before
    new_target_intros = []   # (label, tag, count) for targets not in old state
    fetch_failures = 0

    for target in targets:
        label, tag, url = target["label"], target["tag"], target["url"]
        try:
            log(f"Fetching [{tag}] {url}")
            html = fetch_html(url)
            data = extract_bootstrap(html)
            snap = build_snapshot(data, url)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            log(f"ERROR [{tag}]: {type(exc).__name__}: {exc}")
            fetch_failures += 1
            if label in old_targets:
                # Keep the previous snapshot so a transient failure doesn't look like
                # every item in this target was removed.
                new_targets[label] = old_targets[label]
            continue

        log(f"[{tag}] matched {len(snap)} products (grid has {len(data.get('tiles', []))})")
        if not snap:
            # An empty grid usually means the page layout changed or we were blocked.
            log(f"ERROR [{tag}]: zero products parsed; keeping previous snapshot")
            fetch_failures += 1
            if label in old_targets:
                new_targets[label] = old_targets[label]
            continue

        new_targets[label] = snap

        if label not in old_targets:
            # Newly added target (e.g. just enabled watching this model): seed it
            # silently instead of reporting its whole current inventory as "new".
            new_target_intros.append((label, tag, len(snap)))
            continue

        added, removed, changed = diff_snapshots(old_targets[label], snap)
        tags_by_part = {p: watchlist_tags(snap[p], label) for p in added}
        suppressed = [p for p in added if not within_cap(snap[p], max_price) and not tags_by_part[p]]
        added = [p for p in added if within_cap(snap[p], max_price) or tags_by_part[p]]
        if suppressed:
            log(f"[{tag}] suppressed {len(suppressed)} added item(s) above EUR {max_price:.0f}")
        if added or removed or changed:
            sections.append({
                "label": label, "tag": tag, "url": url,
                "added": added, "removed": removed, "changed": changed,
                "old": old_targets[label], "new": snap,
            })

    if fetch_failures == len(targets):
        log("ERROR: every target failed to parse; aborting without overwriting state")
        return 1

    should_save = is_first_run or bool(new_target_intros) or bool(sections)
    if should_save:
        save_state(state_file, {"targets": new_targets})

    if is_first_run:
        log("First run: seeding state for all targets, no diff to report")
        if email_enabled:
            subject, body = build_startup_email(targets, new_targets)
            send_email(subject, body, gmail_user, gmail_pwd, mail_to)
            log("Sent startup confirmation email")
        else:
            log("Email disabled (set GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO to enable)")
        return 0

    if not sections and not new_target_intros:
        log("No changes since last check")
        return 0

    total_added = sum(len(s["added"]) for s in sections)
    total_removed = sum(len(s["removed"]) for s in sections)
    total_changed = sum(len(s["changed"]) for s in sections)
    log(
        f"Changes to notify: +{total_added} -{total_removed} ~{total_changed} (price), "
        f"{len(new_target_intros)} new target(s)"
    )

    if not email_enabled:
        log("Email disabled (set GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO to enable)")
        return 0

    subject, body = build_change_email(sections, new_target_intros)
    send_email(subject, body, gmail_user, gmail_pwd, mail_to)
    log(f"Sent change email to {mail_to}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, ValueError, OSError, smtplib.SMTPException) as exc:
        # Surface hard failures as a non-zero exit so the CI run (and its notice) flags them.
        log(f"FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
