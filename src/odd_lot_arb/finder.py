"""Orchestrates EDGAR scraping, market pricing, and caching.

This is the conductor of our little orchestra:
  🎺 EdgarScraper plays the "find filings" part
  💰 enrich_with_price adds the current market price
  📦 The cache means we don't re-download everything every 30 seconds
     (the SEC would like a word if we did)

Results are sorted: actionable → unexpired → highest spread first.
Because life is short and so is the tender offer window.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .edgar import EdgarScraper
from .market import enrich_with_price
from .models import OddLotOpportunity

logger = logging.getLogger(__name__)

_CACHE_PATH  = Path.home() / ".oddlotarb" / "cache.json"
_CACHE_TTL   = 30 * 60   # 30 minutes — fresh enough to be useful, stale enough to be polite


class OpportunityFinder:
    """High-level orchestrator: fetch → parse → price → sort → cache."""

    def __init__(self, days_back: int = 90, use_cache: bool = True) -> None:
        self.days_back  = days_back
        self.use_cache  = use_cache

        self._opps: list[OddLotOpportunity] = []
        self._last_refresh: Optional[datetime] = None
        self._is_refreshing: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def opportunities(self) -> list[OddLotOpportunity]:
        return self._opps

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    @property
    def is_refreshing(self) -> bool:
        return self._is_refreshing

    def try_load_cache(self) -> bool:
        """Warm up from disk cache (fast). Returns True if cache is still fresh."""
        if not self.use_cache:
            return False
        try:
            data = json.loads(_CACHE_PATH.read_text())
            refreshed_at = datetime.fromisoformat(data["refreshed_at"])
            if (datetime.now() - refreshed_at).total_seconds() > _CACHE_TTL:
                return False
            self._opps = [
                OddLotOpportunity(**_deserialise(o))
                for o in data.get("opportunities", [])
            ]
            self._last_refresh = refreshed_at
            logger.debug("Loaded %d opportunities from cache", len(self._opps))
            return True
        except Exception:
            return False

    def refresh(
        self,
        status_cb: Optional[Callable[[str], None]] = None,
        force: bool = False,
    ) -> list[OddLotOpportunity]:
        """Fetch fresh data from EDGAR. Thread-safe enough for a single bg thread."""
        if self._is_refreshing:
            return self._opps

        self._is_refreshing = True
        try:
            opps = self._fetch(status_cb)
            self._opps = opps
            self._last_refresh = datetime.now()
            if self.use_cache:
                self._save_cache()
        except Exception as exc:
            logger.error("Refresh failed: %s", exc)
        finally:
            self._is_refreshing = False

        return self._opps

    def sorted_by(
        self,
        key: str = "spread_pct",
        reverse: bool = True,
        active_only: bool = False,
    ) -> list[OddLotOpportunity]:
        opps = self._opps
        if active_only:
            opps = [o for o in opps if o.is_actionable]
        sort_fns: dict[str, Callable] = {
            "spread_pct":        lambda o: o.spread_pct or -9999,
            "annualized_return": lambda o: o.annualized_return or -9999,
            "days_to_expiry":    lambda o: o.days_to_expiry if o.days_to_expiry is not None else 9999,
            "offer_price":       lambda o: o.offer_price or 0,
            "company_name":      lambda o: o.company_name.lower(),
            "filing_date":       lambda o: o.filing_date.isoformat(),
        }
        fn = sort_fns.get(key, sort_fns["spread_pct"])
        return sorted(opps, key=fn, reverse=reverse)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _fetch(
        self, status_cb: Optional[Callable[[str], None]]
    ) -> list[OddLotOpportunity]:
        def cb(msg: str) -> None:
            if status_cb:
                status_cb(msg)

        cb("Fetching recent SC TO filings from EDGAR…")
        with EdgarScraper() as scraper:
            hits = scraper.search_odd_lot_filings(days_back=self.days_back)
            total = len(hits)
            cb(f"Found {total} recent SC TO filing(s). Checking for odd-lot provisions…")

            opps: list[OddLotOpportunity] = []
            seen_accessions: set[str] = set()

            for idx, hit in enumerate(hits, 1):
                company = hit.get("company_name", "?")
                cb(f"[{idx}/{total}] Checking {company}…")
                try:
                    opp_dict = scraper.parse_opportunity(hit)
                    if not opp_dict:
                        continue
                    acc = opp_dict.get("accession_number") or hit.get("accession", "")
                    if acc in seen_accessions:
                        continue
                    seen_accessions.add(acc)

                    opp = OddLotOpportunity(**opp_dict)
                    enrich_with_price(opp)
                    opps.append(opp)
                except Exception as exc:
                    logger.warning("Skipping filing: %s", exc)

        cb(f"Done — {len(opps)} opportunity/-ies processed.")
        # Sort: actionable first, then by spread %
        opps.sort(
            key=lambda o: (
                -int(o.is_actionable),
                -int(not o.is_expired),
                -(o.spread_pct or -9999),
            )
        )
        return opps

    def _save_cache(self) -> None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "refreshed_at": self._last_refresh.isoformat(),  # type: ignore[union-attr]
                "opportunities": [_serialise(o) for o in self._opps],
            }
            _CACHE_PATH.write_text(json.dumps(payload, indent=2, default=str))
        except Exception as exc:
            logger.warning("Cache save failed: %s", exc)


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _serialise(opp: OddLotOpportunity) -> dict:
    d = opp.model_dump()
    for k in ("filing_date", "expiration_date", "last_price_update"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def _deserialise(d: dict) -> dict:
    for k in ("filing_date", "expiration_date"):
        if d.get(k):
            try:
                d[k] = date.fromisoformat(d[k])
            except (ValueError, TypeError):
                d[k] = None
    if d.get("last_price_update"):
        try:
            d["last_price_update"] = datetime.fromisoformat(d["last_price_update"])
        except (ValueError, TypeError):
            d["last_price_update"] = None
    return d
