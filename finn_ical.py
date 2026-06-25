#!/usr/bin/env python3
"""
finn_ical.py
============

Fetches the busy dates from a public FINN cabin-rental listing and writes them to
an .ics file with timed events ("Opptatt: Saltholmen 7"), covering a rolling
window of the next 12 months from today.

Strategy (verified against the real DOM, see spec):
  1. Load the listing page in a Windows browser context (anti-detection).
  2. Wait for `button.CalendarDay`.
  3. Read every day carrying the `eventBooking` class, take its `time[datetime]`
     -> date, and which markers the day has (Start / Between / End).
  4. Page ~12 months forward (click the right arrow), re-read after each click,
     and merge into {date: set(markers)} (deduplicated per date).
  5. Reconstruct bookings from Start/End (keeps back-to-back stays separate).
  6. Write the .ics manually: DTSTART = check-in, DTEND = check-out (exclusive
     end, same convention as Airbnb's iCal).

Run locally:
    pip install -r requirements.txt
    playwright install chromium        # or use real Chrome (channel)
    python finn_ical.py

Environment variables (all optional):
    FINN_CODE         FINN code (default 249098396)
    FINN_PROXY        Proxy URL, e.g. http://user:pass@host:port
                      Used ONLY if set. The only real fix for datacenter-IP blocks.
    FINN_CHANNEL      Playwright channel: "chrome" (real Chrome) or empty (chromium)
    FINN_HEADFUL      Set to "1" to show the browser (debugging)
    FINN_STORAGE      Path to the storage_state file (default storage_state.json)
    FINN_MONTHS       Number of months to page forward (default 12)
    OUT_ICS           Output file (default hytte.ics)
"""

import os
import re
import sys
import time
import random
from datetime import date, datetime, timedelta, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# playwright-stealth is optional, and its API differs across versions.
# Old (<=1.0.x): stealth_sync(page). New (>=1.1.0): Stealth().apply_stealth_sync(page).
# We support both and fall back silently to the manual webdriver patch otherwise.
def _make_stealth():
    try:
        from playwright_stealth import stealth_sync  # old API
        return stealth_sync
    except Exception:
        pass
    try:
        from playwright_stealth import Stealth  # new API
        return lambda page: Stealth().apply_stealth_sync(page)
    except Exception:
        return None

_STEALTH = _make_stealth()
_HAS_STEALTH = _STEALTH is not None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

FINN_CODE = os.environ.get("FINN_CODE", "249098396")
LISTING_URL = (
    "https://www.finn.no/reise/feriehus-hytteutleie/ad.html?finnkode=" + FINN_CODE
)
OUT_ICS = os.environ.get("OUT_ICS", "hytte.ics")
STORAGE_PATH = os.environ.get("FINN_STORAGE", "storage_state.json")
MONTHS_AHEAD = int(os.environ.get("FINN_MONTHS", "12"))
# FINN_DEBUG=1 -> dump every CalendarDay button (datetime + full class) to
# calendar_debug.txt so we can calibrate marker detection against the real DOM.
DEBUG = os.environ.get("FINN_DEBUG", "") == "1"
DEBUG_ROWS: list[str] = []

# Windows + Chrome fingerprint. Keep the major version in the UA and sec-ch-ua in sync.
CHROME_MAJOR = "126"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
SEC_CH_UA = (
    f'"Not/A)Brand";v="8", "Chromium";v="{CHROME_MAJOR}", '
    f'"Google Chrome";v="{CHROME_MAJOR}"'
)
EXTRA_HEADERS = {
    "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.5",
    # Credible Referer: as if arriving from FINN's search results
    "Referer": "https://www.finn.no/reise/feriehus-hytteutleie/search.html",
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# English month mapping (the datetime attribute is always English, locale-independent)
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _log(msg: str) -> None:
    print(msg, flush=True)


def parse_datetime_attr(value: str) -> date:
    """Parse "Tue Jun 30 2026" -> date(2026, 6, 30), locale-independent."""
    parts = value.split()
    # Expected: ["Tue", "Jun", "30", "2026"]
    if len(parts) != 4:
        raise ValueError(f"Unexpected datetime format: {value!r}")
    _weekday, mon, day, year = parts
    if mon not in MONTHS:
        raise ValueError(f"Unknown month: {mon!r} in {value!r}")
    return date(int(year), MONTHS[mon], int(day))


def build_proxy() -> dict | None:
    """Build a Playwright proxy dict from FINN_PROXY (only if set)."""
    raw = os.environ.get("FINN_PROXY", "").strip()
    if not raw:
        return None
    # Accept http(s)://[user:pass@]host:port and socks5://...
    m = re.match(r"^(?P<scheme>\w+)://(?:(?P<user>[^:@]+):(?P<pw>[^@]+)@)?(?P<host>.+)$", raw)
    if not m:
        _log(f"[WARN] Could not parse FINN_PROXY ({raw!r}) - ignoring.")
        return None
    proxy = {"server": f"{m.group('scheme')}://{m.group('host')}"}
    if m.group("user"):
        proxy["username"] = m.group("user")
        proxy["password"] = m.group("pw")
    _log("[INFO] Using proxy from FINN_PROXY.")
    return proxy


def human_pause(lo: float = 0.4, hi: float = 1.2) -> None:
    """Random human-like pause between month clicks."""
    time.sleep(random.uniform(lo, hi))


# --------------------------------------------------------------------------- #
# DOM extraction
# --------------------------------------------------------------------------- #

def read_calendar_days(page) -> dict[date, set[str]]:
    """
    Read every `button.CalendarDay` carrying an eventBooking class in the current view.
    Returns {date: set("Start"|"Between"|"End")}.
    """
    result: dict[date, set[str]] = {}
    buttons = page.query_selector_all("button.CalendarDay")
    for btn in buttons:
        cls = btn.get_attribute("class") or ""
        # Exact-token match (NOT substring): "eventBookingStart" is a substring of
        # "eventBookingStartEnd", so substring matching would mis-tag single-night
        # bookings. Split into tokens and compare against full class names.
        tokens = set(cls.split())

        def has(name: str) -> bool:
            return f"CalendarDay--{name}" in tokens

        if not has("eventBooking"):
            # Available day (or disabled past/min-stay) - ignore.
            continue
        time_el = btn.query_selector("time[datetime]")
        if not time_el:
            continue
        dt_attr = time_el.get_attribute("datetime")
        if not dt_attr:
            continue
        try:
            d = parse_datetime_attr(dt_attr)
        except ValueError as e:
            _log(f"[WARN] {e}")
            continue
        markers: set[str] = set()
        if has("eventBookingStartEnd"):
            # Single-night booking: both check-in and last night are this day.
            markers.update({"Start", "End"})
        if has("eventBookingStart"):
            markers.add("Start")
        if has("eventBookingEnd"):
            markers.add("End")
        if has("eventBookingBetween"):
            markers.add("Between")
        if not markers:
            # eventBooking with no known position - treat as "Between" (mid-stay).
            markers.add("Between")
        result[d] = markers
    return result


def dump_view(page, view_idx: int) -> None:
    """Debug: record EVERY button.CalendarDay (datetime + full class) for one view."""
    if not DEBUG:
        return
    header = ""
    for sel in ["[class*='CalendarMonth'] [class*='caption']", "h2", "caption", "strong"]:
        el = page.query_selector(sel)
        if el:
            txt = (el.inner_text() or "").strip()
            if txt:
                header = txt
                break
    DEBUG_ROWS.append(f"### VIEW {view_idx}  header={header!r}")
    for btn in page.query_selector_all("button.CalendarDay"):
        cls = btn.get_attribute("class") or ""
        t = btn.query_selector("time[datetime]")
        dt = t.get_attribute("datetime") if t else None
        label = btn.get_attribute("aria-label") or ""
        DEBUG_ROWS.append(f"{dt!r}\taria={label!r}\tclass={cls!r}")
    # On the first view, also dump every non-CalendarDay button so we can find
    # the exact "next month" selector if the guesses above miss.
    if view_idx == 0:
        DEBUG_ROWS.append("### NON-CalendarDay BUTTONS (nav candidates)")
        for btn in page.query_selector_all("button"):
            cls = btn.get_attribute("class") or ""
            if "CalendarDay" in cls:
                continue
            label = btn.get_attribute("aria-label") or ""
            txt = (btn.inner_text() or "").strip().replace("\n", " ")[:40]
            DEBUG_ROWS.append(f"aria={label!r}\ttext={txt!r}\tclass={cls!r}")


def view_signature(page) -> tuple[str, str]:
    """A cheap fingerprint of the current view: first & last visible day datetimes.
    Used to detect whether a 'next month' click actually advanced the calendar."""
    days = page.query_selector_all("button.CalendarDay time[datetime]")
    if not days:
        return ("", "")
    first = days[0].get_attribute("datetime") or ""
    last = days[-1].get_attribute("datetime") or ""
    return (first, last)


def click_next_month(page) -> bool:
    """
    Click the right arrow (next month). Returns True if the click succeeded.
    FINN uses different aria-labels per language - try several.
    """
    candidates = [
        # FINN's actual next-month button (verified in calendar_debug.txt).
        "button.Calendar-navButton--right",
        ".Calendar-navButton--right",
        "button[aria-label='neste måned']",
        # Fallbacks for other layouts. NOTE: avoid a bare [aria-label*='neste'] -
        # FINN's image carousel has aria-label 'gå til neste bilde' which would
        # match first. Scope strictly to month navigation instead.
        "[class*='Calendar-navButton'][class*='right']",
        "button[aria-label*='neste måned' i]",
    ]
    for sel in candidates:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if el and el.is_visible() and el.is_enabled():
            try:
                el.click()
                return True
            except Exception:
                continue
    return False


# --------------------------------------------------------------------------- #
# Booking reconstruction
# --------------------------------------------------------------------------- #

def parse_bookings(day_markers: dict[date, set[str]]) -> list[tuple[date, date]]:
    """
    Reconstruct bookings from the calendar markers. Returns (check-in, check-out)
    where check-out is EXCLUSIVE (the .ics DTEND), matching Airbnb's iCal.

    Marker semantics (verified against the real FINN DOM):
      - Start       = first occupied night (check-in day)
      - Between     = a middle occupied night
      - End         = LAST occupied night  -> check-out = End + 1 day
      - StartEnd    = a single-night booking (Start and End are the same day)

    Because `End` is the last occupied night (not the check-out day), back-to-back
    bookings stay separate automatically: booking A's End and booking B's Start
    fall on different days.
    """
    bookings: list[tuple[date, date]] = []
    open_start: date | None = None

    for d in sorted(day_markers.keys()):
        markers = day_markers[d]
        is_start = "Start" in markers
        is_end = "End" in markers

        if is_start and is_end:
            # Single-night standalone booking (eventBookingStartEnd).
            if open_start is not None:
                # Defensive: close any stray open booking first.
                bookings.append((open_start, d))
                open_start = None
            bookings.append((d, d + timedelta(days=1)))
            continue

        if is_end:
            # Last occupied night -> check-out is the following day (exclusive).
            start = open_start if open_start is not None else d
            bookings.append((start, d + timedelta(days=1)))
            open_start = None
            continue

        if is_start:
            open_start = d
        # "Between" (or unmarked eventBooking) belongs to the open booking.

    # A booking still open at the end (continues past our 12-month window).
    if open_start is not None:
        last = max(day_markers.keys())
        bookings.append((open_start, last + timedelta(days=1)))

    return bookings


def add_months(d: date, n: int) -> date:
    """Add n calendar months to a date, clamping the day to the month length."""
    from calendar import monthrange
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def clip_to_window(
    bookings: list[tuple[date, date]],
    today: date | None = None,
    months: int = 12,
) -> list[tuple[date, date]]:
    """Keep only the rolling [today, today + `months`] window.

    Drops bookings that start on/after the horizon, and clips the check-out of a
    booking that runs past the horizon so the calendar always shows exactly the
    newest 12 months. Each run recomputes this from the current date, so the
    window rolls forward one day at a time automatically.
    """
    if today is None:
        today = date.today()
    horizon = add_months(today, months)
    out: list[tuple[date, date]] = []
    for start, end in bookings:
        if start >= horizon:
            continue          # entirely beyond the window
        if end > horizon:
            end = horizon     # clip a booking that extends past 12 months
        if end > start:
            out.append((start, end))
    return out


# --------------------------------------------------------------------------- #
# .ics generation
# --------------------------------------------------------------------------- #

# Check-in / check-out clock times (local Europe/Oslo time).
CHECKIN_TIME = (15, 0)    # arrive 15:00 on the check-in date
CHECKOUT_TIME = (12, 0)   # leave 12:00 on the check-out date

# Europe/Oslo VTIMEZONE so check-in/out times are correct year-round (CET/CEST,
# including the daylight-saving switch). Apple Calendar honours this.
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/Oslo",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def _fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fmt_local(d: date, hm: tuple[int, int]) -> str:
    """Local date-time stamp 'YYYYMMDDTHHMMSS' (used with TZID=Europe/Oslo)."""
    return f"{_fmt_date(d)}T{hm[0]:02d}{hm[1]:02d}00"


def build_ics(bookings: list[tuple[date, date]]) -> str:
    """Write an RFC 5545 calendar with timed events (check-in 15:00, check-out
    12:00 Europe/Oslo). CRLF line endings."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hytte-ICS//FINN availability//NO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Hytte (FINN)",
        "X-WR-TIMEZONE:Europe/Oslo",
        *VTIMEZONE,
    ]
    for start, end in bookings:
        uid = f"{_fmt_date(start)}-{_fmt_date(end)}-{FINN_CODE}@hytte-ics"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=Europe/Oslo:{_fmt_local(start, CHECKIN_TIME)}",
            f"DTEND;TZID=Europe/Oslo:{_fmt_local(end, CHECKOUT_TIME)}",
            "SUMMARY:Opptatt: Saltholmen 7",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------- #
# Browser setup
# --------------------------------------------------------------------------- #

def launch_browser(pw):
    """Launch the browser with real Chrome if possible, otherwise chromium."""
    channel = os.environ.get("FINN_CHANNEL", "").strip()
    headless = os.environ.get("FINN_HEADFUL", "") != "1"
    proxy = build_proxy()
    launch_kwargs = {
        "headless": headless,
        # The new headless mode is harder to detect than the old one.
        "args": ["--headless=new"] if headless else [],
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    if channel:
        try:
            _log(f"[INFO] Trying Playwright channel {channel!r}.")
            return pw.chromium.launch(channel=channel, **launch_kwargs)
        except Exception as e:
            _log(f"[INFO] Channel {channel!r} failed ({e}); falling back to chromium.")
    return pw.chromium.launch(**launch_kwargs)


def make_context(browser):
    ctx_kwargs = {
        "user_agent": USER_AGENT,
        "locale": "nb-NO",
        "timezone_id": "Europe/Oslo",
        "viewport": {"width": 1366, "height": 768},
        "extra_http_headers": EXTRA_HEADERS,
    }
    # Returning-visitor effect: reuse stored cookies if the file exists.
    if os.path.exists(STORAGE_PATH):
        ctx_kwargs["storage_state"] = STORAGE_PATH
        _log(f"[INFO] Reusing storage_state from {STORAGE_PATH}.")
    ctx = browser.new_context(**ctx_kwargs)
    # Hide automation flags (fallback even when stealth is on).
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return ctx


def looks_like_block(page) -> bool:
    """Rough check: a challenge/login page instead of the listing."""
    try:
        html = page.content().lower()
    except Exception:
        return False
    needles = ["captcha", "er du et menneske", "verifiser at du", "access denied",
               "unusual traffic", "logg inn for å fortsette"]
    return any(n in html for n in needles)


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def main() -> int:
    _log(f"[INFO] Fetching {LISTING_URL}")
    with sync_playwright() as pw:
        browser = launch_browser(pw)
        ctx = make_context(browser)
        page = ctx.new_page()
        if _STEALTH is not None:
            try:
                _STEALTH(page)
                _log("[INFO] playwright-stealth enabled.")
            except Exception as e:
                _log(f"[INFO] playwright-stealth failed ({e}) - using manual patch.")
        else:
            _log("[INFO] playwright-stealth not installed - using manual patch.")

        page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=45_000)

        # Wait for the calendar. A timeout here likely means a bot block/challenge.
        try:
            page.wait_for_selector("button.CalendarDay", timeout=30_000)
        except PWTimeout:
            if looks_like_block(page):
                _log("[ERROR] Looks like a challenge/login page (bot block).")
            else:
                _log("[ERROR] Never found button.CalendarDay (timeout).")
            _log("[ERROR] Set FINN_PROXY (Norwegian/residential) and try again.")
            browser.close()
            return 1

        if looks_like_block(page):
            _log("[ERROR] Challenge/login page detected. Use the proxy fallback.")
            browser.close()
            return 1

        # Read the current view, then page ~12 months forward.
        all_days: dict[date, set[str]] = {}
        all_days.update(read_calendar_days(page))
        dump_view(page, 0)
        clicks = 0
        for i in range(MONTHS_AHEAD):
            before = view_signature(page)
            if not click_next_month(page):
                _log(f"[INFO] No 'next month' button found after {i} clicks - stopping.")
                break
            human_pause()
            # Wait until the month actually changes. We can't wait_for_selector on
            # visibility here: the first CalendarDay is often a hidden overflow day
            # (isNotDisplayedMonth), so a visibility wait would time out.
            advanced = False
            for _ in range(20):  # up to ~5 s
                sig = view_signature(page)
                if sig != before and sig != ("", ""):
                    advanced = True
                    break
                page.wait_for_timeout(250)
            if not advanced:
                _log(f"[WARN] Calendar did not advance after click #{i + 1} - stopping.")
                break
            clicks += 1
            all_days.update(read_calendar_days(page))  # dedup per date
            dump_view(page, i + 1)
        _log(f"[INFO] Advanced {clicks} month(s); read {len(all_days)} busy days in total.")

        if DEBUG:
            with open("calendar_debug.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(DEBUG_ROWS))
            _log(f"[DEBUG] Wrote calendar_debug.txt ({len(DEBUG_ROWS)} lines).")

        # Save cookies for the next run (returning-visitor effect).
        try:
            ctx.storage_state(path=STORAGE_PATH)
        except Exception as e:
            _log(f"[WARN] Could not save storage_state: {e}")

        browser.close()

    bookings = parse_bookings(all_days)
    bookings = clip_to_window(bookings, months=12)  # rolling: today .. today+12mo
    _log(f"[INFO] Reconstructed {len(bookings)} booking(s):")
    for start, end in bookings:
        _log(f"        {start.isoformat()} -> {end.isoformat()} (check-out/exclusive)")

    ics = build_ics(bookings)
    with open(OUT_ICS, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    _log(f"[INFO] Wrote {OUT_ICS} ({len(bookings)} event(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
