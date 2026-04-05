"""Entry point — ``oddlot`` CLI command."""
from __future__ import annotations

import argparse
import sys

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="oddlot",
        description="Odd Lot Arbitrage Finder — EDGAR Tender Offer Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  oddlot                    launch interactive TUI (default)
  oddlot --days 180         search last 180 days of filings
  oddlot --no-cache         skip cached results, force fresh fetch
  oddlot --list             print table to stdout (no TUI)
  oddlot --list --days 30   last 30 days, plain text output
        """,
    )
    parser.add_argument("--version", action="version", version=f"oddlot {__version__}")
    parser.add_argument(
        "--days", type=int, default=90,
        metavar="N",
        help="how many days back to search EDGAR (default: 90)",
    )
    parser.add_argument(
        "--no-cache", dest="no_cache", action="store_true",
        help="ignore cached results and fetch fresh from EDGAR",
    )
    parser.add_argument(
        "--list", dest="list_mode", action="store_true",
        help="print opportunities to stdout instead of launching TUI",
    )
    parser.add_argument(
        "--active-only", dest="active_only", action="store_true",
        help="show only actionable opportunities (has odd-lot provision, not expired)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="enable debug logging to stderr",
    )

    args = parser.parse_args()

    if args.debug:
        import logging
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

    use_cache = not args.no_cache

    if args.list_mode:
        _run_list(days_back=args.days, use_cache=use_cache, active_only=args.active_only)
    else:
        from .ui import run_ui
        run_ui(days_back=args.days, use_cache=use_cache)


def _run_list(days_back: int, use_cache: bool, active_only: bool) -> None:
    from .finder import OpportunityFinder

    finder = OpportunityFinder(days_back=days_back, use_cache=use_cache)

    def cb(msg: str) -> None:
        print(f"\r\033[K[*] {msg}", end="", flush=True)

    opps = finder.refresh(status_cb=cb)
    print()  # newline after spinner

    if active_only:
        opps = [o for o in opps if o.is_actionable]

    if not opps:
        print("No odd-lot arbitrage opportunities found.")
        return

    # ── Pretty table ──────────────────────────────────────────────────────────
    SEP  = "─" * 100
    HEAD = (
        f"{'TICKER':<8}  {'COMPANY':<28}  {'OFFER':>8}  "
        f"{'CURR':>8}  {'SPREAD':>8}  {'ANN.RET':>8}  {'EXPIRES':<12}  RATING"
    )
    print(f"\n{'ODD LOT ARB — EDGAR FINDER':^100}")
    print(SEP)
    print(HEAD)
    print(SEP)

    for opp in opps:
        ticker  = (opp.ticker or "—")[:8]
        company = opp.company_name[:28]
        offer   = f"${opp.offer_price:.2f}" if opp.offer_price else "—"
        curr    = f"${opp.current_price:.2f}" if opp.current_price else "—"
        sp      = f"{opp.spread_pct:+.2f}%" if opp.spread_pct is not None else "—"
        ar      = f"{opp.annualized_return:.1f}%" if opp.annualized_return is not None else "—"
        exp     = str(opp.expiration_date) if opp.expiration_date else "unknown"
        rating  = opp.risk_rating
        print(
            f"{ticker:<8}  {company:<28}  {offer:>8}  "
            f"{curr:>8}  {sp:>8}  {ar:>8}  {exp:<12}  {rating}"
        )

    print(SEP)
    print(f"  {len(opps)} opportunities  |  days searched: {days_back}")
    print()

    # Detail for actionable opps
    actionable = [o for o in opps if o.is_actionable]
    if actionable:
        print(f"{'ACTIONABLE DETAIL':^100}")
        print(SEP)
        for opp in actionable:
            print(f"\n  {opp.company_name}  ({opp.ticker or 'no ticker'})  [{opp.form_type}]")
            offer_s  = f"${opp.offer_price:.2f}" if opp.offer_price else "n/a"
            curr_s   = f"${opp.current_price:.2f}" if opp.current_price else "n/a"
            spread_s = f"{opp.spread_pct:+.2f}%" if opp.spread_pct is not None else "n/a"
            ann_s    = f"{opp.annualized_return:.1f}%" if opp.annualized_return is not None else "n/a"
            print(f"  Offer: {offer_s}  │  Current: {curr_s}  │  Spread: {spread_s}  │  Ann.Ret: {ann_s}")
            exp_s = f"{opp.expiration_date} ({opp.days_to_expiry}d)" if opp.expiration_date else "unknown"
            print(f"  Threshold: <{opp.odd_lot_threshold} shares  │  Expires: {exp_s}")
            if opp.odd_lot_text:
                print(f"  Odd-lot clause: …{opp.odd_lot_text[:120]}…")
            print(f"  Filing: {opp.filing_url}")
        print(SEP)


if __name__ == "__main__":
    main()
