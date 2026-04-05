"""Data models for odd lot arbitrage opportunities.

The core insight: when a company does a tender offer, shareholders holding
FEWER than 100 shares ("odd lots") get priority acceptance — no proration.
Buy <100 shares below the offer price, tender them, pocket the spread.
It's not insider trading, it's not cheating — it's just reading the fine
print that most institutional investors are too big to care about. 🔍
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class OddLotOpportunity(BaseModel):
    """One beautiful little arb trade waiting to happen.

    Or, y'know, an expired one. Life is suffering. Check is_expired first.
    """

    model_config = {"frozen": False}

    # ── Filing metadata ──────────────────────────────────────────────────────
    company_name: str
    cik: str                # Central Index Key — the SEC's way of saying "we numbered everyone"
    accession_number: str   # looks like 0001234567-24-000001, means nothing to humans
    form_type: str          # SC TO-T (third party offer) or SC TO-I (issuer buying itself)
    filing_date: date
    filing_url: str         # proof it's real, not hallucinated

    # ── Offer details ────────────────────────────────────────────────────────
    offer_price: Optional[float] = None        # the golden number — what they'll pay you
    odd_lot_threshold: int = 99                # usually <100 shares; the magic boundary
    expiration_date: Optional[date] = None     # don't miss this — the clock is always ticking
    has_odd_lot_provision: bool = False        # the whole point; False = not worth our time
    odd_lot_text: Optional[str] = None         # the actual clause we found — receipts!

    # ── Market data ──────────────────────────────────────────────────────────
    ticker: Optional[str] = None              # None for non-traded funds (sad trombone)
    current_price: Optional[float] = None     # what the plebs are paying on the open market
    last_price_update: Optional[datetime] = None

    # ── Computed properties ──────────────────────────────────────────────────

    @property
    def spread_dollar(self) -> Optional[float]:
        """Offer minus market price. Positive = profit. Negative = whoops."""
        if self.offer_price is not None and self.current_price is not None:
            return self.offer_price - self.current_price
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        """Spread as a percentage. 2% in 2 weeks beats your savings account by a mile."""
        sd = self.spread_dollar
        if sd is not None and self.current_price and self.current_price > 0:
            return (sd / self.current_price) * 100
        return None

    @property
    def days_to_expiry(self) -> Optional[int]:
        """How many sunrises until this opportunity evaporates. Negative = too late, pal."""
        if self.expiration_date:
            return (self.expiration_date - date.today()).days
        return None

    @property
    def annualized_return(self) -> Optional[float]:
        """Spread % × (365 / days) — because everyone loves big annualized numbers.
        Yes, 2% in 14 days IS 52% annualized. No, you can't do this 26x a year.
        """
        sp = self.spread_pct
        dte = self.days_to_expiry
        if sp is not None and dte and dte > 0:
            return sp * (365.0 / dte)
        return None

    @property
    def is_expired(self) -> bool:
        """True if you've already missed the boat. The boat has sailed. Goodbye boat."""
        dte = self.days_to_expiry
        return dte is not None and dte < 0

    @property
    def is_actionable(self) -> bool:
        """True if you could theoretically put this trade on right now.
        Has odd lot clause + not expired + has a real offer price. The holy trinity.
        """
        return (
            self.has_odd_lot_provision
            and not self.is_expired
            and self.offer_price is not None
        )

    @property
    def risk_rating(self) -> str:
        """One-word verdict. We're not liable for your financial decisions."""
        if self.is_expired:
            return "EXPIRED"       # 💀
        if not self.has_odd_lot_provision:
            return "NO ODD LOT"    # just a regular tender offer, move along
        sp = self.spread_pct
        if sp is None:
            return "NO PRICE"      # non-traded fund, can't mark to market
        if sp > 3:
            return "HOT"           # 🔥 get in there
        if sp > 1:
            return "GOOD"          # solid risk-adjusted return
        if sp > 0:
            return "THIN"          # technically profitable, but so is a lemonade stand
        return "NEGATIVE"          # the market knows something you don't
