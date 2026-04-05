"""Snazzy curses terminal UI for the Odd Lot Arb Finder.

Yes, we built a Bloomberg Terminal knockoff in Python curses.
Yes, it uses box-drawing characters and a pyfiglet ASCII logo.
No, we are not sorry.

Minimum terminal size: 60 columns × 15 rows.
Recommended: 100+ × 30+ for the full experience. Go full-screen. You deserve it.

On Windows: make sure `windows-curses` is installed (it's in pyproject.toml,
so `uv sync` handles it). On Mac/Linux: curses ships with Python, you're fine.
"""
from __future__ import annotations

import curses
import logging
import threading
import time
from datetime import datetime
from typing import Optional

import pyfiglet

from .finder import OpportunityFinder
from .models import OddLotOpportunity

logger = logging.getLogger(__name__)

# ── Colour pair IDs ───────────────────────────────────────────────────────────
_C_DEFAULT  = 1
_C_CYAN     = 2
_C_SEL      = 3
_C_GREEN    = 4
_C_YELLOW   = 5
_C_RED      = 6
_C_MAGENTA  = 7
_C_BLUE     = 8
_C_STATUS   = 9
_C_WARN     = 10
_C_HOT      = 11   # bright green for HOT spread
_C_DIM      = 12

# ── Box-drawing glyphs ────────────────────────────────────────────────────────
_H  = "─"
_V  = "│"
_TL = "┌"; _TR = "┐"; _BL = "└"; _BR = "┘"
_LT = "├"; _RT = "┤"; _TT = "┬"; _BT = "┴"; _XX = "┼"
_DH = "═"   # double horizontal
_DV = "║"   # double vertical

_SORT_KEYS = [
    ("spread_pct",        "Spread %"),
    ("annualized_return", "Ann. Ret."),
    ("days_to_expiry",    "Days Left"),
    ("offer_price",       "Offer $"),
    ("filing_date",       "Filed"),
    ("company_name",      "Company"),
]

_HELP_KEYS = "[↑↓/jk] Nav  [PgUp/Dn] Page  [Enter] Detail  [s] Sort  [f] Filter  [r] Refresh  [q] Quit"


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write *text* to *win* at (y, x), silently ignoring out-of-bounds errors."""
    try:
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x < 0:
            return
        avail = w - x - 1
        if avail <= 0:
            return
        win.addstr(y, x, text[:avail], attr)
    except curses.error:
        pass


def _hline(win, y: int, x: int, width: int, attr: int = 0) -> None:
    _safe_addstr(win, y, x, _H * width, attr)


def _box_top(win, y: int, x: int, width: int, attr: int = 0) -> None:
    _safe_addstr(win, y, x, _TL + _H * (width - 2) + _TR, attr)


def _box_bot(win, y: int, x: int, width: int, attr: int = 0) -> None:
    _safe_addstr(win, y, x, _BL + _H * (width - 2) + _BR, attr)


def _spread_bar(spread_pct: Optional[float], width: int = 10) -> str:
    """Render a tiny ASCII bar for spread %."""
    if spread_pct is None:
        return "·" * width
    filled = min(width, max(0, int(spread_pct / 5.0 * width)))
    return "█" * filled + "░" * (width - filled)


def _rating_attr(opp: OddLotOpportunity, selected: bool) -> int:
    if selected:
        return curses.color_pair(_C_SEL) | curses.A_BOLD
    if opp.is_expired:
        return curses.color_pair(_C_RED) | curses.A_DIM
    if not opp.has_odd_lot_provision:
        return curses.color_pair(_C_DIM)
    sp = opp.spread_pct
    if sp is None:
        return curses.color_pair(_C_DEFAULT)
    if sp > 3:
        return curses.color_pair(_C_HOT) | curses.A_BOLD
    if sp > 1:
        return curses.color_pair(_C_GREEN)
    if sp > 0:
        return curses.color_pair(_C_YELLOW)
    return curses.color_pair(_C_RED)


# ─────────────────────────────────────────────────────────────────────────────

class OddLotApp:
    """Full-screen curses application."""

    _LOGO_FONT = "small"
    _LOGO_TEXT = "ODD  LOT  ARB"

    def __init__(self, stdscr, finder: OpportunityFinder) -> None:
        self.scr    = stdscr
        self.finder = finder

        self._opps:         list[OddLotOpportunity] = []
        self._sel:          int   = 0          # selected row index
        self._scroll:       int   = 0          # top visible row index
        self._sort_idx:     int   = 0          # index into _SORT_KEYS
        self._sort_rev:     bool  = True
        self._active_only:  bool  = False
        self._detail_mode:  bool  = False

        self._status:       str   = "  Press r to refresh"
        self._refreshing:   bool  = False
        self._logo_lines:   list[str] = []
        self._last_err:     str   = ""

        self._init_colors()
        self._build_logo()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_colors(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(_C_DEFAULT,  curses.COLOR_WHITE,   -1)
        curses.init_pair(_C_CYAN,     curses.COLOR_CYAN,    -1)
        curses.init_pair(_C_SEL,      curses.COLOR_BLACK,   curses.COLOR_CYAN)
        curses.init_pair(_C_GREEN,    curses.COLOR_GREEN,   -1)
        curses.init_pair(_C_YELLOW,   curses.COLOR_YELLOW,  -1)
        curses.init_pair(_C_RED,      curses.COLOR_RED,     -1)
        curses.init_pair(_C_MAGENTA,  curses.COLOR_MAGENTA, -1)
        curses.init_pair(_C_BLUE,     curses.COLOR_BLUE,    -1)
        curses.init_pair(_C_STATUS,   curses.COLOR_BLACK,   curses.COLOR_WHITE)
        curses.init_pair(_C_WARN,     curses.COLOR_BLACK,   curses.COLOR_YELLOW)
        curses.init_pair(_C_HOT,      curses.COLOR_GREEN,   -1)
        curses.init_pair(_C_DIM,      curses.COLOR_WHITE,   -1)

    def _build_logo(self) -> None:
        raw = pyfiglet.figlet_format(self._LOGO_TEXT, font=self._LOGO_FONT)
        self._logo_lines = [l for l in raw.splitlines() if l.strip()][:5]

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        curses.curs_set(0)
        self.scr.nodelay(True)
        self.scr.timeout(100)

        # Try loading from cache immediately
        if self.finder.try_load_cache():
            self._reload_opps()
            self._status = f"  Loaded {len(self._opps)} from cache — press r to refresh"
        else:
            self._start_refresh()

        while True:
            try:
                self._draw()
                key = self.scr.getch()
                if not self._handle_key(key):
                    break
            except KeyboardInterrupt:
                break
            except Exception as exc:
                self._last_err = str(exc)

    # ── Input handling ────────────────────────────────────────────────────────

    def _handle_key(self, key: int) -> bool:
        """Return False to quit."""
        if key in (ord("q"), ord("Q")):
            return False

        h, w = self.scr.getmaxyx()
        vis = self._visible_rows(h)

        if key in (ord("r"), ord("R")):
            if not self._refreshing:
                self._start_refresh()

        elif key in (curses.KEY_UP, ord("k")) :
            self._move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._move(1)
        elif key == curses.KEY_PPAGE:
            self._move(-vis)
        elif key == curses.KEY_NPAGE:
            self._move(vis)
        elif key in (curses.KEY_HOME, ord("g")):
            self._sel = 0; self._scroll = 0
        elif key in (curses.KEY_END, ord("G")):
            self._sel = max(0, len(self._opps) - 1)
            self._scroll = max(0, self._sel - vis + 1)

        elif key in (ord("\n"), curses.KEY_ENTER, ord(" ")):
            self._detail_mode = not self._detail_mode

        elif key in (ord("s"), ord("S")):
            # Cycle sort key; if same key clicked again, flip direction
            new_idx = (self._sort_idx + 1) % len(_SORT_KEYS)
            if new_idx == self._sort_idx:
                self._sort_rev = not self._sort_rev
            else:
                self._sort_idx = new_idx
                self._sort_rev = True
            self._reload_opps()

        elif key in (ord("f"), ord("F")):
            self._active_only = not self._active_only
            self._reload_opps()

        elif key == curses.KEY_RESIZE:
            self.scr.clear()

        return True

    def _move(self, delta: int) -> None:
        h, _ = self.scr.getmaxyx()
        vis   = self._visible_rows(h)
        n     = len(self._opps)
        if n == 0:
            return
        self._sel = max(0, min(n - 1, self._sel + delta))
        if self._sel < self._scroll:
            self._scroll = self._sel
        elif self._sel >= self._scroll + vis:
            self._scroll = self._sel - vis + 1

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _draw(self) -> None:
        h, w = self.scr.getmaxyx()
        if h < 15 or w < 60:
            self.scr.erase()
            _safe_addstr(self.scr, 0, 0,
                         "Terminal too small — please resize (min 60×15).",
                         curses.A_BOLD)
            self.scr.refresh()
            return

        self.scr.erase()
        row = self._draw_header(h, w)
        row = self._draw_stats_bar(row, h, w)
        row = self._draw_col_headers(row, w)
        row = self._draw_rows(row, h, w)

        if self._detail_mode and self._opps:
            self._draw_detail_overlay(h, w)

        self._draw_status_bar(h, w)
        self.scr.refresh()

    # ── Header ────────────────────────────────────────────────────────────────

    def _draw_header(self, h: int, w: int) -> int:
        logo_attr = curses.color_pair(_C_MAGENTA) | curses.A_BOLD
        row = 0
        for line in self._logo_lines:
            centered = line.center(w - 1)
            _safe_addstr(self.scr, row, 0, centered, logo_attr)
            row += 1
            if row >= h - 8:
                break

        # Tagline
        tagline = "◆  EDGAR Tender Offer Scanner  ◆  Small Investor Arbitrage  ◆"
        _safe_addstr(self.scr, row, max(0, (w - len(tagline)) // 2),
                     tagline, curses.color_pair(_C_CYAN))
        row += 1

        # Double-line separator
        _safe_addstr(self.scr, row, 0, _DH * (w - 1),
                     curses.color_pair(_C_CYAN) | curses.A_BOLD)
        row += 1
        return row

    # ── Stats bar ─────────────────────────────────────────────────────────────

    def _draw_stats_bar(self, row: int, h: int, w: int) -> int:
        if row >= h - 5:
            return row
        n       = len(self._opps)
        active  = sum(1 for o in self._opps if o.is_actionable)
        best    = max((o.spread_pct for o in self._opps if o.spread_pct is not None),
                      default=None)
        best_s  = f"{best:+.2f}%" if best is not None else "n/a"
        sk_name = _SORT_KEYS[self._sort_idx][1]
        filt    = " [ACTIVE ONLY]" if self._active_only else ""
        stats   = (f"  {n} filings  │  {active} actionable  │  "
                   f"Best spread: {best_s}  │  Sort: {sk_name}{filt}")
        _safe_addstr(self.scr, row, 0, stats,
                     curses.color_pair(_C_YELLOW) | curses.A_BOLD)
        row += 1
        _safe_addstr(self.scr, row, 0, _H * (w - 1),
                     curses.color_pair(_C_BLUE))
        row += 1
        return row

    # ── Column headers ────────────────────────────────────────────────────────

    _COL_WIDTHS = (8, 26, 9, 9, 9, 8, 8)  # TICKER COMPANY OFFER CURR SPREAD EXP STATUS
    _COL_HEADS  = ("TICKER", "COMPANY", "OFFER $", "CURR $", "SPREAD", "EXP", "RATING")

    def _draw_col_headers(self, row: int, w: int) -> int:
        attr = curses.color_pair(_C_CYAN) | curses.A_BOLD | curses.A_UNDERLINE
        x = 1
        for head, cw in zip(self._COL_HEADS, self._COL_WIDTHS):
            _safe_addstr(self.scr, row, x, head.ljust(cw), attr)
            x += cw + 1
        row += 1
        _safe_addstr(self.scr, row, 0, _H * (w - 1),
                     curses.color_pair(_C_BLUE))
        row += 1
        return row

    # ── Table rows ────────────────────────────────────────────────────────────

    def _visible_rows(self, h: int) -> int:
        # header ~9 rows + status 2 rows + detail maybe 5 rows
        reserved = 11 + (5 if self._detail_mode else 0)
        return max(1, h - reserved)

    def _draw_rows(self, row: int, h: int, w: int) -> int:
        vis    = self._visible_rows(h)
        opps   = self._opps
        end_r  = row + vis

        if not opps:
            msg = "  No opportunities found — press r to search EDGAR."
            _safe_addstr(self.scr, row, 0, msg,
                         curses.color_pair(_C_YELLOW) | curses.A_BOLD)
            return row + 1

        for i in range(self._scroll, min(self._scroll + vis, len(opps))):
            if row >= end_r or row >= h - 3:
                break
            opp  = opps[i]
            sel  = (i == self._sel)
            attr = _rating_attr(opp, sel)
            self._draw_opp_row(row, w, opp, attr, sel)
            row += 1

        # Scrollbar indicator
        if len(opps) > vis:
            pct  = self._scroll / max(1, len(opps) - vis)
            sbar_h = max(1, vis * vis // len(opps))
            sbar_y = row - vis + int(pct * (vis - sbar_h))
            for sy in range(sbar_y, min(sbar_y + sbar_h, h - 3)):
                _safe_addstr(self.scr, sy, w - 2, "▐",
                             curses.color_pair(_C_BLUE) | curses.A_BOLD)

        return row

    def _draw_opp_row(
        self, row: int, w: int, opp: OddLotOpportunity, attr: int, selected: bool
    ) -> None:
        ticker  = (opp.ticker or "—").ljust(self._COL_WIDTHS[0])
        company = opp.company_name[:self._COL_WIDTHS[1]].ljust(self._COL_WIDTHS[1])
        offer   = (f"${opp.offer_price:.2f}" if opp.offer_price else "—").ljust(self._COL_WIDTHS[2])
        curr    = (f"${opp.current_price:.2f}" if opp.current_price else "—").ljust(self._COL_WIDTHS[3])
        sp      = opp.spread_pct
        spread  = (f"{sp:+.2f}%" if sp is not None else "—").ljust(self._COL_WIDTHS[4])
        dte     = opp.days_to_expiry
        exp     = (f"{dte}d" if dte is not None else "—").ljust(self._COL_WIDTHS[5])
        rating  = opp.risk_rating.ljust(self._COL_WIDTHS[6])

        sel_prefix = "▶ " if selected else "  "
        line = f"{sel_prefix}{ticker} {company} {offer} {curr} {spread} {exp} {rating}"

        if selected:
            _safe_addstr(self.scr, row, 0, " " * (w - 1), attr)
        _safe_addstr(self.scr, row, 0, line, attr)

    # ── Detail overlay ────────────────────────────────────────────────────────

    def _draw_detail_overlay(self, h: int, w: int) -> None:
        if self._sel >= len(self._opps):
            return
        opp   = self._opps[self._sel]
        lines = self._detail_lines(opp, w)
        bh    = len(lines) + 2
        by    = h - bh - 2  # 2 for status bar
        if by < 4:
            by = 4

        # Separator
        _safe_addstr(self.scr, by - 1, 0, _DH * (w - 1),
                     curses.color_pair(_C_CYAN) | curses.A_BOLD)

        box_attr = curses.color_pair(_C_CYAN)
        _box_top(self.scr, by, 0, w - 1, box_attr)
        for i, dl in enumerate(lines):
            _safe_addstr(self.scr, by + 1 + i, 0, _V, box_attr)
            _safe_addstr(self.scr, by + 1 + i, 1, dl.ljust(w - 3),
                         curses.color_pair(_C_DEFAULT))
            _safe_addstr(self.scr, by + 1 + i, w - 2, _V, box_attr)
        _box_bot(self.scr, by + bh - 1, 0, w - 1, box_attr)

    def _detail_lines(self, opp: OddLotOpportunity, w: int) -> list[str]:
        sp  = f"{opp.spread_pct:+.2f}%" if opp.spread_pct is not None else "n/a"
        ar  = f"{opp.annualized_return:.1f}%" if opp.annualized_return is not None else "n/a"
        dte = f"{opp.days_to_expiry}d" if opp.days_to_expiry is not None else "n/a"
        exp = str(opp.expiration_date) if opp.expiration_date else "unknown"
        odd = "✓  " + (opp.odd_lot_text[:w - 10] if opp.odd_lot_text else "—") \
              if opp.has_odd_lot_provision else "✗ No odd-lot provision detected"

        lines = [
            f" ▸ {opp.company_name}  ({opp.ticker or 'no ticker'})  [{opp.form_type}]",
            f"   Offer: {'$'+str(opp.offer_price) if opp.offer_price else 'n/a':>10}  "
            f"Current: {'$'+f'{opp.current_price:.2f}' if opp.current_price else 'n/a':>10}  "
            f"Spread: {sp:>8}  Ann.Ret: {ar:>8}",
            f"   Expires: {exp} ({dte})   "
            f"Threshold: <{opp.odd_lot_threshold} shares   "
            f"Filed: {opp.filing_date}",
            f"   {odd[:w-4]}",
            f"   URL: {opp.filing_url[:w-8]}",
        ]
        return lines

    # ── Status bar ────────────────────────────────────────────────────────────

    def _draw_status_bar(self, h: int, w: int) -> None:
        # Help line
        help_attr = curses.color_pair(_C_STATUS)
        help_line = _HELP_KEYS[:w - 1].ljust(w - 1)
        _safe_addstr(self.scr, h - 2, 0, help_line, help_attr)

        # Status line
        if self._refreshing:
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            ch   = spin[int(time.time() * 8) % len(spin)]
            left = f" {ch} {self._status}"
            attr = curses.color_pair(_C_WARN) | curses.A_BOLD
        else:
            ts   = self.finder.last_refresh
            ts_s = ts.strftime("%-I:%M:%S %p") if ts else "never"
            live_dot = "● LIVE" if ts else "○ —"
            left = f"  {self._status}"
            right = f" Last refresh: {ts_s}  {live_dot} "
            _safe_addstr(self.scr, h - 1, w - len(right) - 1, right,
                         curses.color_pair(_C_CYAN))
            attr = curses.color_pair(_C_DEFAULT)

        _safe_addstr(self.scr, h - 1, 0, left[:w - 1], attr)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _reload_opps(self) -> None:
        key  = _SORT_KEYS[self._sort_idx][0]
        rev  = self._sort_rev
        self._opps = self.finder.sorted_by(key, rev, self._active_only)
        self._sel  = min(self._sel, max(0, len(self._opps) - 1))

    def _start_refresh(self) -> None:
        self._refreshing = True
        self._status     = "  Fetching from EDGAR…"

        def _worker() -> None:
            def cb(msg: str) -> None:
                self._status = f"  {msg}"

            self.finder.refresh(status_cb=cb)
            self._reload_opps()
            self._refreshing = False
            n = len(self._opps)
            self._status = f"  {n} filing(s) loaded"

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_ui(days_back: int = 90, use_cache: bool = True) -> None:
    finder = OpportunityFinder(days_back=days_back, use_cache=use_cache)

    def _main(stdscr) -> None:
        app = OddLotApp(stdscr, finder)
        app.run()

    curses.wrapper(_main)
