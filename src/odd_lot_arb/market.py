"""Market data helpers — current price via yfinance.

We use Yahoo Finance (via yfinance) to get the current stock price so we can
compute the spread between "what you'd pay today" and "what the company will
pay you". If the spread is positive and there's an odd-lot provision, that's
free money*

* not financial advice. seriously. don't sue us. we're just reading filings.
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ticker -> (price, monotonic timestamp)
# Simple dict cache beats lru_cache here because we need time-based expiry
_price_cache: dict[str, tuple[float, float]] = {}
_PRICE_TTL = 300  # 5 minutes — stale enough to not get banned, fresh enough to matter


def get_current_price(ticker: str) -> Optional[float]:
    """Return the most recent trading price for *ticker*.

    Caches for 5 minutes. Returns None if Yahoo Finance is having a bad day
    (which happens more than you'd think).
    """
    if not ticker:
        return None

    now = time.monotonic()
    cached = _price_cache.get(ticker)
    if cached and (now - cached[1]) < _PRICE_TTL:
        return cached[0]

    try:
        import yfinance as yf  # deferred import so startup is fast

        tkr = yf.Ticker(ticker)
        hist = tkr.history(period="1d", interval="1m")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            _price_cache[ticker] = (price, now)
            return price

        # Fallback: fast_info
        info = tkr.fast_info
        price = float(info.get("lastPrice") or info.get("regularMarketPrice") or 0)
        if price > 0:
            _price_cache[ticker] = (price, now)
            return price

    except Exception as exc:
        logger.debug("yfinance error for %s: %s", ticker, exc)

    return None


def enrich_with_price(opp) -> None:
    """Mutate *opp* by attaching the current market price."""
    if opp.ticker:
        price = get_current_price(opp.ticker)
        if price:
            from datetime import datetime
            opp.current_price = price
            opp.last_price_update = datetime.now()
