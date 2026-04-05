"""EDGAR scraper — finds and parses SC TO-T / SC TO-I tender offer filings.

Search strategy
───────────────
Fun fact: the SEC's fancy full-text-search endpoint (EFTS) completely ignores
its own date filter and instead returns results sorted by "relevance", which
apparently means "vibes from 2016". We discovered this the hard way.

So instead we use the boring-but-reliable current-filings Atom feed:

  1.  Fetch recent filings from the EDGAR Atom feed — always sorted
      newest-first, covers SC TO-I (issuer) and SC TO-T (third-party).

  2.  Download each filing's main document and do a quick "odd lot" keyword
      check. Most SC TO filers are private funds doing routine redemptions —
      boring. We skip them fast and only parse the interesting ones.

  3.  Extract offer price, expiry date, odd-lot threshold, and the clause
      text so you have receipts when your broker asks why you bought 47
      shares of a company you'd never heard of.

SEC rate limit: 10 req/s. We do ~8. Don't be greedy. The SEC notices.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://www.sec.gov"
DATA_BASE  = "https://data.sec.gov"

# The SEC requires a real User-Agent with contact info. Bots without one get
# rate-limited into the shadow realm. Don't be that bot.
_HEADERS = {
    "User-Agent": "OddLotArb/1.0 research@oddlotarb.com",
    "Accept": "application/json, text/html, */*",
    "Accept-Encoding": "gzip, deflate, br",
}

# ── Regex patterns ───────────────────────────────────────────────────────────
# Legal drafters have infinite creativity when describing the same thing.
# "price equal to", "purchase price of", "consideration of" — it's all just
# "here's what we'll pay you", written by lawyers billing by the word.

_MONTHS = (
    r'(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)'
)
_MDY = rf'{_MONTHS}\s+\d{{1,2}},\s+\d{{4}}'

# Higher-specificity first — we use finditer so ALL matches are checked, not
# just the first. (Important because par value "$0.001 per share" appears
# before the real offer price in almost every filing. Thanks, lawyers.)
_OFFER_PRICE_RE = [
    # "at a price equal to $X.XX per share"
    re.compile(r'price\s+equal\s+to\s+\$\s*([\d,]+\.\d{1,4})\s+per\s+share', re.I),
    # "purchase price of $X.XX"
    re.compile(r'purchase\s+price\s+of\s+\$\s*([\d,]+\.\d{1,4})', re.I),
    # "offering price of $X.XX"
    re.compile(r'offer(?:ing)?\s+price\s+of\s+\$\s*([\d,]+\.\d{1,4})', re.I),
    # "consideration of $X.XX per share"
    re.compile(r'consideration\s+of\s+\$\s*([\d,]+\.\d{1,4})\s+per\s+share', re.I),
    # "tender offer price ... $X.XX"
    re.compile(r'tender\s+offer\s+price[^$\n]{0,60}\$\s*([\d,]+\.\d{1,4})', re.I),
    # "purchased at $X.XX per share"
    re.compile(r'purchased\s+at\s+a\s+price\s+(?:equal\s+to\s+)?\$\s*([\d,]+\.\d{1,4})', re.I),
    # "at $X.XX per share" (general, used as fallback)
    re.compile(r'\bat\s+\$\s*([\d,]+\.\d{1,4})\s+per\s+share', re.I),
    # "$X.XX per share" — broadest, catches most formats; skip tiny par values
    re.compile(r'\$\s*([\d,]+\.\d{1,4})\s+per\s+(?:share|common\s+share)', re.I),
]

_EXPIRY_RE = [
    # Anchored: "on <MMMM DD, YYYY>" after any expir* word
    re.compile(rf'expir\w+[^{{}}]{{0,200}}?\bon\s+({_MDY})', re.I | re.S),
    # Straight: first MDY date after any expir* word
    re.compile(rf'expir\w+.*?({_MDY})', re.I | re.S),
    # Numeric date mm/dd/yyyy
    re.compile(r'expir\w+.*?(\d{1,2}/\d{1,2}/\d{4})', re.I | re.S),
]

_ODD_LOT_PROVISION_RE = [
    # Classic explicit clauses
    re.compile(r'odd\s+lot[^.]{0,80}not\s+subject\s+to\s+proration', re.I | re.S),
    re.compile(r'odd\s+lot[^.]{0,80}exempt(?:ed)?\s+from\s+proration', re.I | re.S),
    re.compile(r'odd\s+lot[^.]{0,80}priority[^.]{0,80}tender', re.I | re.S),
    re.compile(r'odd\s+lot[^.]{0,80}preference[^.]{0,80}accept', re.I | re.S),
    re.compile(r'holders\s+of\s+odd\s+lots?[^.]{0,100}accept', re.I | re.S),
    re.compile(r'odd\s+lot[^.]{0,80}tendered[^.]{0,80}accepted\s+in\s+full', re.I | re.S),
    re.compile(r'all\s+shares\s+tendered\s+by\s+odd\s+lot', re.I | re.S),
    re.compile(r'odd\s+lot\s+tender\s+offer', re.I),
    # "accepted for payment before any pro rata" style
    re.compile(r'odd\s+lot[^.]{0,150}accepted\s+for\s+payment\s+before', re.I | re.S),
    re.compile(r'odd\s+lot[^.]{0,200}before\s+any\s+pro\s+rata', re.I | re.S),
    re.compile(r'odd\s+lot[^.]{0,200}accepted[^.]{0,100}pro\s+rata', re.I | re.S),
    # Generic: odd lot mentioned near proration / pro rata
    re.compile(r'odd\s+lot.{0,300}pro\s*rata', re.I | re.S),
    re.compile(r'odd\s+lot.{0,300}proration', re.I | re.S),
]

_ODD_LOT_THRESHOLD_RE = [
    re.compile(r'odd\s+lot[^.]{0,80}(?:fewer|less)\s+than\s+(\d+)\s+shares', re.I),
    re.compile(r'(?:fewer|less)\s+than\s+(\d+)\s+shares[^.]{0,80}odd\s+lot', re.I),
    re.compile(r'(\d+)\s+shares\s+or\s+(?:fewer|less)', re.I),
    re.compile(r'(\d+)\s+or\s+(?:fewer|less)\s+shares', re.I),
    re.compile(r'odd\s+lot\s+holder[^.]{0,80}(?:fewer|less)\s+than\s+(\d+)', re.I),
    re.compile(r'beneficially\s+own(?:ed)?\s+(?:fewer|less)\s+than\s+(\d+)\s+shares', re.I),
]

_DATE_FMTS = ("%B %d, %Y", "%m/%d/%Y", "%B %d,%Y")

# Atom entry title: "SC TO-I/A - Company Name  (CIK 0001234567) (Subject)"
# OR:               "SC TO-I - Company Name (TICK) (0001234567) (Subject)"
_TITLE_RE = re.compile(
    r'^(SC\s+TO-[IT][/A]*)\s+-\s+(.+?)\s+\((\d{7,10})\)\s+\(Subject\)',
    re.I,
)


# ── HTTP client ──────────────────────────────────────────────────────────────

class _SecClient:
    """httpx wrapper that respects SEC's ~10 req/s rate limit.

    The SEC has feelings. Hammer them at 50 req/s and you get 429'd into
    the shadow realm. We do ~8 req/s and everyone stays friends.
    """

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers=_HEADERS, timeout=30.0, follow_redirects=True
        )
        self._last_req: float = 0.0

    def get(self, url: str, **kw) -> httpx.Response:
        elapsed = time.monotonic() - self._last_req
        if elapsed < 0.12:
            time.sleep(0.12 - elapsed)
        self._last_req = time.monotonic()
        return self._client.get(url, **kw)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Main scraper ─────────────────────────────────────────────────────────────

class EdgarScraper:
    """Find and parse odd-lot tender-offer opportunities from EDGAR.

    The workflow:
      Atom feed → list of recent SC TO filings
        → download each filing document
          → "does this say 'odd lot'?" (most don't — interval funds are boring)
            → parse offer price, expiry date, odd-lot threshold
              → profit (literally)
    """

    def __init__(self) -> None:
        self._http = _SecClient()

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Public API ───────────────────────────────────────────────────────────

    def search_odd_lot_filings(
        self, days_back: int = 90, max_hits: int = 60
    ) -> list[dict]:
        """Return recent SC TO filing metadata dicts, newest-first.

        Paginates the EDGAR current-filings Atom feed until we've covered
        *days_back* calendar days or collected *max_hits* entries.
        """
        cutoff = (datetime.now() - timedelta(days=days_back)).date()
        all_filings: list[dict] = []
        seen_acc: set[str] = set()

        # Use "SC TO" to match both SC TO-I and SC TO-T (and their /A amendments)
        for form_type in ("SC TO-I", "SC TO-T", "SC TO-T/A", "SC TO-I/A"):
            dateb = ""           # start from today, paginate backwards
            for _page in range(8):   # at most 8 pages × 40 = 320 entries per type
                page = self._get_current_filings_page(form_type, count=40, dateb=dateb)
                if not page:
                    break
                oldest_on_page = page[-1]["filing_date"]
                for f in page:
                    if f["accession"] in seen_acc:
                        continue
                    if f["filing_date"] >= cutoff:
                        seen_acc.add(f["accession"])
                        all_filings.append(f)
                if oldest_on_page < cutoff:
                    break  # went past our cutoff
                # Use day BEFORE oldest to avoid re-fetching same entries
                prev_day = oldest_on_page - timedelta(days=1)
                dateb = prev_day.strftime("%Y%m%d")

        all_filings.sort(key=lambda f: f["filing_date"], reverse=True)
        logger.debug(
            "Current-filings feed: %d within cutoff %s", len(all_filings), cutoff
        )
        return all_filings[:max_hits]

    def parse_opportunity(self, hit: dict) -> Optional[dict]:
        """Download the filing and return an opportunity dict, or None if no
        odd-lot provision is found."""
        cik       = hit.get("cik", "")
        accession = hit.get("accession", "")
        filename  = hit.get("filename")

        if not cik or not accession:
            return None

        # Build the base result from Atom metadata
        result: dict = {
            "company_name":          hit.get("company_name", "Unknown"),
            "cik":                   cik,
            "accession_number":      accession,
            "form_type":             hit.get("form_type", "SC TO-I"),
            "filing_date":           hit.get("filing_date", date.today()),
            "filing_url":            hit.get("filing_url", ""),
            "offer_price":           None,
            "odd_lot_threshold":     99,
            "expiration_date":       None,
            "has_odd_lot_provision": False,
            "odd_lot_text":          None,
            "ticker":                hit.get("ticker"),
        }

        # Enrich ticker if missing
        if not result["ticker"]:
            result["ticker"] = self._get_ticker(cik)

        # Download filing text
        text = self._get_filing_text(cik, accession, filename)
        if not text:
            return result   # return without offer details rather than None

        # Quick pre-filter: must mention "odd lot"
        if not re.search(r'\bodd\s+lot\b', text, re.I):
            return None     # skip — no odd lot language at all

        self._extract_details(text, result)
        return result

    # ── Atom feed parsing ────────────────────────────────────────────────────

    def _get_current_filings_page(
        self, form_type: str, count: int = 40, dateb: str = ""
    ) -> list[dict]:
        """Fetch one page of current filings from the EDGAR Atom feed."""
        url = f"{EDGAR_BASE}/cgi-bin/browse-edgar"
        params = {
            "action": "getcurrent",
            "type":   form_type,
            "dateb":  dateb,
            "owner":  "include",
            "count":  str(count),
            "output": "atom",
        }
        try:
            resp = self._http.get(url, params=params)
            resp.raise_for_status()
            return self._parse_atom(resp.text)
        except Exception as exc:
            logger.error("Atom feed error for %s: %s", form_type, exc)
            return []

    def _parse_atom(self, xml: str) -> list[dict]:
        """Parse an EDGAR current-filings Atom feed into filing dicts."""
        results = []
        soup = BeautifulSoup(xml, "lxml-xml")

        for entry in soup.find_all("entry"):
            try:
                title_tag  = entry.find("title")
                link_tag   = entry.find("link", rel="alternate")
                updated_tag = entry.find("updated")

                if not (title_tag and link_tag):
                    continue

                title  = title_tag.get_text(strip=True)
                link   = link_tag.get("href", "")
                update = updated_tag.get_text(strip=True) if updated_tag else ""

                # Parse company name, CIK, form type from title
                m = _TITLE_RE.match(title)
                if m:
                    form_type    = m.group(1).strip().upper()
                    company_name = m.group(2).strip()
                    cik          = m.group(3).lstrip("0") or "0"
                else:
                    # Fallback: try to extract CIK from link URL
                    cik_m = re.search(r'/edgar/data/(\d+)/', link)
                    cik   = cik_m.group(1) if cik_m else ""
                    company_name = title.split(" - ", 1)[-1].split("(")[0].strip()
                    form_type = title.split(" - ")[0].strip().upper()

                # Accession from link URL
                acc_m = re.search(
                    r'([\d]{10}-[\d]{2}-[\d]{6})-index\.htm', link
                )
                accession = acc_m.group(1) if acc_m else ""
                nodash = accession.replace("-", "")

                # Filing date from <updated>
                try:
                    filing_date = datetime.fromisoformat(update.split("T")[0]).date()
                except (ValueError, AttributeError):
                    filing_date = date.today()

                # Ticker may appear in company_name "(TICK)"
                tick_m = re.search(r'\(([A-Z]{1,5})\)', company_name)
                ticker = tick_m.group(1) if tick_m else None
                # Strip the ticker from name
                company_name = re.sub(r'\s*\([A-Z]{1,5}\)', "", company_name).strip()

                filing_url = (
                    f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{nodash}/{accession}-index.htm"
                ) if cik and accession else link

                results.append({
                    "company_name": company_name,
                    "cik":          cik,
                    "accession":    accession,
                    "form_type":    form_type,
                    "filing_date":  filing_date,
                    "filing_url":   filing_url,
                    "filename":     None,
                    "ticker":       ticker,
                })
            except Exception as exc:
                logger.debug("Atom entry parse error: %s", exc)

        return results

    # ── Filing document fetching ─────────────────────────────────────────────

    def _get_ticker(self, cik: str) -> Optional[str]:
        padded = cik.zfill(10)
        try:
            resp = self._http.get(f"{DATA_BASE}/submissions/CIK{padded}.json")
            resp.raise_for_status()
            tickers = resp.json().get("tickers", [])
            return tickers[0] if tickers else None
        except Exception:
            return None

    def _get_filing_text(
        self, cik: str, accession: str, filename: Optional[str]
    ) -> Optional[str]:
        nodash = accession.replace("-", "")

        # 1) Direct filename if known
        if filename:
            url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{nodash}/{filename}"
            try:
                resp = self._http.get(url)
                if resp.status_code == 200:
                    return _html_or_text(resp.text)[:300_000]
            except Exception:
                pass

        # 2) Parse the filing index HTML
        index_url = (
            f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{nodash}/{accession}-index.htm"
        )
        doc = self._main_doc_from_index(index_url, cik, nodash)
        if doc:
            return doc

        # 3) Complete submission text file
        txt_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{nodash}/{accession}.txt"
        try:
            resp = self._http.get(txt_url)
            if resp.status_code == 200:
                return _extract_from_sgml(resp.text)[:300_000]
        except Exception:
            pass

        return None

    def _main_doc_from_index(
        self, index_url: str, cik: str, nodash: str
    ) -> Optional[str]:
        try:
            resp = self._http.get(index_url)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "lxml")
            # IMPORTANT: the index page includes the entire SEC.gov navigation
            # menu, full of links to random gov pages. We must ONLY follow links
            # that live inside this specific filing's Archives folder, otherwise
            # we end up parsing the SEC homepage and wondering why it contains
            # no mention of "odd lot". Ask me how I know. Ahem.
            archive_prefix = f"/Archives/edgar/data/{cik}/{nodash}/"
            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                # Must be in the correct filing folder and be an htm/txt file
                if (
                    href.startswith(archive_prefix)
                    and re.search(r'\.(htm|html|txt)$', href, re.I)
                    and "index" not in href.lower()
                ):
                    doc_url = f"{EDGAR_BASE}{href}"
                    try:
                        doc_resp = self._http.get(doc_url)
                        if doc_resp.status_code == 200:
                            return _html_or_text(doc_resp.text)[:300_000]
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("Index parse error: %s", exc)
        return None

    # ── Offer detail extraction ──────────────────────────────────────────────

    @staticmethod
    def _extract_details(text: str, result: dict) -> None:
        EdgarScraper._parse_offer_price(text, result)
        EdgarScraper._parse_expiry(text, result)
        EdgarScraper._parse_odd_lot(text, result)

    @staticmethod
    def _parse_offer_price(text: str, result: dict) -> None:
        for pat in _OFFER_PRICE_RE:
            for m in pat.finditer(text):   # try ALL matches, not just the first
                try:
                    price = float(m.group(1).replace(",", ""))
                    if 1.0 < price < 100_000:    # must be ≥ $1 to exclude par values
                        result["offer_price"] = price
                        return
                except (ValueError, IndexError):
                    pass

    @staticmethod
    def _parse_expiry(text: str, result: dict) -> None:
        for pat in _EXPIRY_RE:
            m = pat.search(text)
            if not m:
                continue
            date_str = m.group(1).strip()
            for fmt in _DATE_FMTS:
                try:
                    result["expiration_date"] = datetime.strptime(date_str, fmt).date()
                    return
                except ValueError:
                    pass

    @staticmethod
    def _parse_odd_lot(text: str, result: dict) -> None:
        for pat in _ODD_LOT_PROVISION_RE:
            m = pat.search(text)
            if m:
                result["has_odd_lot_provision"] = True
                start = max(0, m.start() - 80)
                end   = min(len(text), m.end() + 250)
                result["odd_lot_text"] = " ".join(text[start:end].split())
                break

        for pat in _ODD_LOT_THRESHOLD_RE:
            m = pat.search(text)
            if m:
                try:
                    thr = int(m.group(1))
                    if 1 <= thr <= 10_000:
                        result["odd_lot_threshold"] = thr
                        return
                except (ValueError, IndexError):
                    pass


# ── Utilities ─────────────────────────────────────────────────────────────────

def _html_or_text(data: str) -> str:
    lower = data[:300].lower()
    if "<html" in lower or "<!doctype" in lower or "<body" in lower:
        soup = BeautifulSoup(data, "lxml")
        for tag in soup(["script", "style", "meta", "head"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    return data


def _extract_from_sgml(raw: str) -> str:
    m = re.search(r'<TEXT>(.*?)</TEXT>', raw, re.S | re.I)
    return _html_or_text(m.group(1)) if m else _html_or_text(raw)
