"""
News Filter — TharEX Bot
Blocks new L1 entries within +/- 30 minutes of a high-impact USD/XAU/EUR news
event (Trading-Rules #3: FOMC, NFP, CPI, ECB, ...).
Recovery levels are NOT blocked — only new L1 scalp entries.
Uses ForexFactory public calendar API, cached locally for 6 hours.
"""

import requests
import logging
import json
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CACHE_FILE  = "data/news_cache.json"
CACHE_HOURS = 6
FF_API_URL  = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Currencies whose high-impact events affect XAUUSD (USD/gold directly; EUR via
# ECB rate decisions per Trading-Rules #3).
AFFECTED_CURRENCIES = {"USD", "XAU", "EUR"}


def _load_cache() -> list:
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now(tz=timezone.utc) - cached_at.replace(tzinfo=timezone.utc) < timedelta(hours=CACHE_HOURS):
            return data.get("events", [])
    except Exception:
        pass
    return []


def _save_cache(events: list):
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump({
            "cached_at": datetime.now(tz=timezone.utc).isoformat(),
            "events":    events,
        }, f, indent=2)


def fetch_news() -> list:
    cached = _load_cache()
    if cached:
        return cached
    try:
        resp = requests.get(FF_API_URL, timeout=10)
        resp.raise_for_status()
        events = []
        for e in resp.json():
            if e.get("impact", "").lower() != "high":
                continue
            try:
                dt = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            except Exception:
                continue
            events.append({
                "title":    e.get("title", ""),
                "currency": e.get("country", "").upper(),
                "time":     dt.isoformat(),
            })
        _save_cache(events)
        logger.info(f"News fetched: {len(events)} high-impact events this week")
        return events
    except Exception as ex:
        logger.warning(f"News fetch failed: {ex} — trading without news filter this cycle")
        return []


def is_news_blackout(blackout_minutes: int = 30) -> tuple:
    """
    Returns (blocked: bool, reason: str).
    Checks all USD / XAU / EUR high-impact events within the blackout window.
    Window is symmetric: from blackout_minutes BEFORE the event until
    blackout_minutes AFTER it (Trading-Rules #3 = +/- 30 min).
    """
    now    = datetime.now(tz=timezone.utc)
    events = fetch_news()

    for event in events:
        if event["currency"] not in AFFECTED_CURRENCIES:
            continue
        try:
            event_time = datetime.fromisoformat(event["time"])
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        minutes_to_event = (event_time - now).total_seconds() / 60
        if -blackout_minutes <= minutes_to_event <= blackout_minutes:
            reason = (
                f"NEWS BLACKOUT: '{event['title']}' ({event['currency']}) "
                f"at {event_time.strftime('%H:%M UTC')} "
                f"({int(minutes_to_event)}min away)"
            )
            return True, reason

    return False, ""
