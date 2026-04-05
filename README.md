# odd-lot-arb

A terminal app that scans SEC EDGAR for **odd lot tender offer arbitrage** opportunities in real time.

---

## What is an odd lot tender offer?

### Background: Tender Offers

When a company (or a third party) wants to buy back shares, they sometimes launch a **tender offer** — a public bid to purchase shares at a fixed price above the current market price. For example:

> Acme Corp is trading at **$9.80**. The company announces a tender offer to buy back shares at **$10.50**, expiring in 30 days.

Shareholders can "tender" their shares and receive $10.50 instead of selling on the open market for $9.80 — a **7.1% spread**.

### The Problem: Proration

Tender offers are often oversubscribed. If the company only wants to buy 10% of outstanding shares but 40% are tendered, each tendering shareholder gets only 25% of their shares accepted (the rest are returned). This is called **proration**, and it kills the math.

### The Odd Lot Exception

Most tender offer documents include an **odd lot provision**: shareholders holding **fewer than 100 shares** (an "odd lot") are **exempt from proration**. Every single one of their shares gets accepted at the full offer price.

This creates a textbook arbitrage opportunity:

| Step | Action |
|------|--------|
| 1 | Find a tender offer with an odd lot provision |
| 2 | Buy fewer than 100 shares on the open market (e.g., 99 shares @ $9.80) |
| 3 | Tender your shares at the offer price ($10.50) |
| 4 | Collect the spread (~$69.30 on 99 shares, no proration risk) |

### Concrete Example

```
Company:       Acme Corp (ACME)
Current price: $9.80
Offer price:   $10.50
Shares bought: 99 (odd lot threshold: <100)
Spread:        $0.70/share = 7.14%
Days to expiry: 21
Annualized:    ~124%
Capital at risk: $970.20
Profit:        ~$69.30
```

The risk is very low — you own real shares, the offer is a public legal commitment, and you have priority acceptance. The main risks are:
- The offer is withdrawn (rare, but possible)
- You miss the deadline
- The company is non-traded (no liquid exit if the offer falls through)

---

## This Tool

`odd-lot-arb` scrapes SEC EDGAR for SC TO-I (issuer tender offers) and SC TO-T (third-party tender offers) filings, parses the documents for odd lot provisions and offer prices, enriches them with live market data, and presents everything in a color-coded terminal UI.

### Features

- Scans EDGAR Atom feed for recent tender offer filings
- Parses offer price, expiration date, and odd lot clause from filing documents
- Fetches current market price via `yfinance`
- Computes spread %, annualized return, and days to expiry
- Color-coded TUI with detail panel and background auto-refresh
- 30-minute local cache to avoid hammering EDGAR
- `--list` mode for plain text / scripting output

---

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/redrussian1917/odd-lot-arb.git
cd odd-lot-arb
uv sync
```

---

## Usage

### Interactive TUI (default)

```bash
uv run oddlot
```

Launches a curses terminal UI. Use arrow keys to navigate, `Enter` to view filing detail, `r` to force refresh, `q` to quit.

### Plain text output

```bash
uv run oddlot --list
```

Prints a formatted table to stdout. Good for piping or scripting.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--days N` | 90 | How many days back to search EDGAR |
| `--no-cache` | off | Skip the 30-min cache and fetch fresh |
| `--list` | off | Print to stdout instead of TUI |
| `--active-only` | off | Show only actionable opportunities (has odd lot clause, not expired) |
| `--debug` | off | Enable debug logging to stderr |

### Examples

```bash
# Default: TUI, last 90 days
uv run oddlot

# Search last 6 months
uv run oddlot --days 180

# Force fresh data, print table
uv run oddlot --list --no-cache

# Only show trades you can actually do right now
uv run oddlot --list --active-only

# Last 30 days, active only, no cache
uv run oddlot --list --days 30 --active-only --no-cache
```

---

## Output columns

| Column | Meaning |
|--------|---------|
| TICKER | Stock ticker (blank for non-traded funds) |
| COMPANY | Issuer name |
| OFFER | Tender offer price |
| CURR | Current market price |
| SPREAD | (Offer - Market) / Market % |
| ANN.RET | Spread annualized to a yearly rate |
| EXPIRES | Offer expiration date |
| RATING | HOT / GOOD / THIN / NEGATIVE / NO ODD LOT / NO PRICE / EXPIRED |

### Ratings

| Rating | Meaning |
|--------|---------|
| HOT | Spread > 3% — worth acting on |
| GOOD | Spread 1–3% — solid risk-adjusted return |
| THIN | Spread 0–1% — marginal |
| NEGATIVE | Market price exceeds offer price |
| NO ODD LOT | Tender offer exists, but no odd lot priority clause |
| NO PRICE | Non-traded fund — can't compute spread |
| EXPIRED | Offer deadline has passed |

---

## Caveats

- **Not financial advice.** Tender offers can be withdrawn. Read the actual filing before trading.
- Most SC TO-I filers are non-traded BDC/interval funds doing periodic redemptions — these rarely have meaningful odd lot provisions.
- Annualized return figures are illustrative. You cannot repeat this trade 26x/year on the same position.
- Always check the actual filing URL (shown in detail view) to confirm the odd lot clause applies to your situation.

---

## Data sources

- **SEC EDGAR** — filing metadata and documents (public, no API key required)
- **yfinance** — live and historical market prices

---

## License

MIT
