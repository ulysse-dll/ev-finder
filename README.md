# ⚡ EV+ Finder — Winamax Value Bet Scanner

> **Zero-cost, fully automated sports betting edge detector.**
> Scrapes Winamax odds, cross-references a 30+ bookmaker consensus from Oddsportal,
> strips the vig, and surfaces every bet where Winamax is paying more than the true probability.

---

## What it does

Most bettors lose because the bookmaker's margin (vig) eats their edge over time.
**EV+ Finder flips that**: it uses the market consensus of 30+ books as a proxy for true probability, then flags every Winamax line that sits above that fair value.

```
Winamax odds: 2.10   ──┐
True prob (devigged): 52%  ──┤──▶  EV = (0.52 × 2.10 − 1) × 100 = +9.2%  ✅
```

Every bet with **EV > 0%** has a positive long-term expected return.
Stakes are sized automatically using **Quarter Kelly** to maximise growth while controlling risk.

---

## Features

| | |
|---|---|
| 🔴 **Live Winamax scraping** | Extracts odds in real time via headless Chrome + `PRELOADED_STATE` |
| 📊 **Multi-book consensus** | Oddsportal aggregates Bet365, Pinnacle, Unibet, 1xBet and 30+ others |
| 🧮 **Additive devigging** | Strips bookmaker margin to recover fair probabilities |
| 📈 **Multi-market support** | 1X2, Over/Under 2.5, BTTS — all detected and matched automatically |
| 🤖 **Auto-settlement** | Results fetched from Winamax + ESPN API fallback, bets resolved automatically |
| 💰 **Kelly sizing** | Quarter Kelly criterion — optimal stake per bet, capped at 5% of bankroll |
| 📉 **P/L tracker** | Live bankroll chart, ROI, win rate, full bet ledger with CSV export |
| 🔤 **Fuzzy team matching** | Handles name mismatches between books (`"PSG"` ↔ `"Paris Saint-Germain"`) |

---

## Stack

```
Backend    Flask · Python 3.11
Scraping   Selenium · Chrome Headless · BeautifulSoup
Data       Oddsportal (multi-book) · Winamax · ESPN unofficial API
Frontend   Vanilla JS · HTML5 Canvas (P/L chart) · CSS glassmorphism
```

---

## Quick start

**Requirements:** Python 3.10+, Google Chrome installed

```bash
git clone https://github.com/ulysse-dll/ev-finder.git
cd ev-finder
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5120** — the first scan starts automatically.

> ChromeDriver is managed automatically via `webdriver-manager`.
> No API keys, no paid subscriptions required.

---

## How it works

```
┌─────────────────┐     ┌──────────────────────┐
│  Winamax        │     │  Oddsportal           │
│  (live odds)    │     │  (30+ book consensus) │
└────────┬────────┘     └──────────┬────────────┘
         │                         │
         │    Fuzzy team match      │
         └────────────┬────────────┘
                      │
              ┌───────▼────────┐
              │  De-vig        │  Remove bookmaker margin
              │  (additive)    │  P_fair = P_implied / ΣP
              └───────┬────────┘
                      │
              ┌───────▼────────┐
              │  EV calc       │  EV% = (P_fair × odds − 1) × 100
              └───────┬────────┘
                      │
              ┌───────▼────────┐
              │  Kelly sizing  │  f* = (b×p − q) / b  ×  0.25
              └───────┬────────┘
                      │
              ┌───────▼────────┐
              │  Bankroll      │  Auto-settle via Winamax / ESPN
              └────────────────┘
```

---

## Configuration

Edit `config.py` to tune the system:

```python
MIN_EV_THRESHOLD = 1.0     # Minimum EV% to flag a bet
KELLY_FRACTION   = 0.25    # Quarter Kelly (0.25 = conservative)
MAX_STAKE_PCT    = 0.05    # Max 5% of bankroll per bet
CACHE_DURATION   = 300     # Re-scan every 5 minutes
FLASK_PORT       = 5120
```

---

## Project structure

```
ev-finder/
├── app.py            # Flask server + scan orchestration
├── scraper.py        # Winamax Selenium scraper + ESPN result fetcher
├── odds_api.py       # Oddsportal multi-market scraper (H2H / O/U / BTTS)
├── ev_calculator.py  # Devigging + EV calculation + multi-market dispatch
├── bankroll.py       # Kelly sizing, bet ledger, auto-settlement
├── config.py         # All tunable parameters
├── static/
│   └── style.css     # Dark glassmorphism UI
└── templates/
    └── dashboard.html
```

---

## Dashboard

- **Stats bar** — live count of value bets, avg EV, events scanned, top sport
- **Scan panel** — real-time progress bar + log stream during each scan
- **Bankroll tracker** — balance, P/L, ROI, win rate, Kelly stakes
- **P/L chart** — canvas-drawn equity curve
- **Active bets / Settled history** — full ledger with market badges (O/U, BTTS)
- **Filters** — by sport, min EV%, odds range

---

## ⚠️ Work in Progress

This project is **actively under development**. Scrapers may break when sites update their structure, odds matching can miss games, and settlement logic is not yet battle-tested across all markets. Expect bugs.

**Do not use this tool as your sole basis for betting decisions.** The EV signals are only as reliable as the scraped data — if Oddsportal returns stale or incomplete odds, the calculation is off. Always cross-check manually before placing real money.

## Disclaimer

This tool is built for educational purposes and statistical analysis.
It does not guarantee profit. Sports betting involves financial risk — please gamble responsibly.

---

*Built with Python + Flask · No paid APIs · Zero subscriptions*
