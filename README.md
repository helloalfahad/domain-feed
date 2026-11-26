# domain-feed

**An open-source data pipeline that turns ~4.6 million expiring-domain auction records into a shortlist of 400 quality candidates — twice a day, at zero cost.**

Built and maintained by a solo developer in Chattogram, Bangladesh, `domain-feed` makes expired-domain auctions accessible to small, independent investors who cannot manually scan millions of listings or afford enterprise tools.

> This project never bids, buys, backorders, or registers any domain. It only collects, filters, and publishes data. Every buying decision is made by a human.

---

## Why this exists

Every day, millions of domains flow through auctions at GoDaddy, Dynadot, and DropCatch. Almost all of them are junk — hyphenated, numeric, misspelled, spam-associated, or meaningless. The genuinely good names are buried, auction inventories are hard to access from cloud and automated environments, and existing tooling is built for large portfolio holders.

`domain-feed` solves that with a simple idea: let GitHub Actions (which has open internet access) do the heavy downloading and filtering, publish the results as plain CSV files in this public repository, and let any downstream tool — an AI agent, a dashboard, or a person — work from that clean feed.

## How it works

A single Python script, `fetch_and_filter.py` (~790 lines, standard library + `wordfreq`), runs on GitHub Actions **twice a day**:

- **22:40 Dhaka time** (16:40 UTC) — after GoDaddy's daily file update
- **06:50 Dhaka time** (00:50 UTC) — fresh data before the morning agent run

Each run:

1. **Downloads** the official public auction files from GoDaddy Auctions, Dynadot, and DropCatch (auctions + pending-delete backorder lists) — about **4.6 million records** in a typical run.
2. **Builds a keyword demand map** of ~1.59 million keywords, using exact-match extension counts (`exactMatchTlds` / DotDB tld count) to measure real demand for each name.
3. **Applies hard filters** (see below) and drops reversed word-order names (e.g. `PanelsSolar` when `SolarPanels` is the natural order).
4. **Ranks** what survives with a `signal_score` based on extension count, search volume, domain age, and referring domains.
5. **Enriches** the shortlist with Archive.org history (first/last snapshot years and snapshot count).
6. **Publishes** a maximum of **400 candidates**, with per-source quotas so no single marketplace dominates: GoDaddy 220, Dynadot 100, DropCatch 80.

A full run takes roughly 11–18 minutes and costs nothing.

## Filters

A name must pass **all** of these to reach the shortlist:

- Extension is **.com** or **.si** only (.ai names go to a separate watchlist)
- Price **≤ $50** (DropCatch backorders ≤ $60)
- Second-level length between 5 and 15 characters
- No hyphens, no numbers
- Made of real dictionary words — one word, or two real words combined (63,746-word dictionary, with word-frequency checks to drop obscure and grammatical filler words)
- No adult, gambling, or abusive terms
- Natural word order (reversed-order variants are dropped)
- Not a pure hype keyword

## Output files

All outputs live in the [`data/`](data/) folder and are updated by every run:

| File | Contents |
|---|---|
| `data/candidates.csv` | Up to 400 filtered buy candidates |
| `data/handreg_available.csv` | Keywords checked via registry RDAP for hand-registration availability (.com / .si) |
| `data/ai_watch.csv` | .ai watchlist names (watch-only, not buy candidates) |
| `data/run_log.txt` | Run timestamp, per-source record counts, filter statistics, and any source errors |

### `candidates.csv` columns

`domain`, `words`, `signal_score`, `source`, `type`, `price_usd`, `bids`, `ends`, `tld_count`, `search_volume`, `age_years`, `ref_domains`, `archive_snapshots`, `archive_first_year`, `archive_last_year`

## Data sources

| Source | Data | Status |
|---|---|---|
| GoDaddy Auctions | Official inventory files (biddable, expiring, ending today/tomorrow, closeout) | ✅ Active |
| Dynadot Auctions | Official auction CSV | ✅ Active |
| DropCatch | Official auction list + pending-delete backorder list | ✅ Active |
| Archive.org | Wayback Machine availability/history for shortlisted names | ✅ Active (partial — rate limits apply) |
| Registry RDAP | .com / .si availability for hand-registration checks | ✅ Active |
| Namecheap Market | Official API | ⏸️ Paused (no API key; the project runs at $0 cost) |

## Who uses it

`domain-feed` is in daily production use as the **sole data source** for:

- **Domain Sleeper Agent** — a daily AI agent that reads the feed every morning at 08:13 Dhaka time, deep-checks the top 10 candidates, scores each one 0–100 (70+ = buy shortlist), labels trademark risk, and writes a daily report.
- **DomainDesk** — a personal dashboard that displays the shortlist with live auction countdowns and reminders.

The project is still early (created November 14, 2026, with active daily development), and the maintainer is its primary user — but the feed and the filtering logic are public, and anyone can clone this repository and build on them.

## Run it yourself

The pipeline runs automatically on GitHub Actions — the CSV files in `data/` are always the latest output, so in most cases you only need to clone and read:

```bash
git clone https://github.com/helloalfahad/domain-feed.git
```

To run the fetcher locally you will need Python 3.12, an English word list (e.g. the `wamerican` package on Ubuntu), and the `wordfreq` package:

```bash
pip install wordfreq
python fetch_and_filter.py
```

## Roadmap

- [ ] Live RDAP priority-watch file for specific high-value names approaching expiry
- [ ] Comparable-sales (comps) data in the feed, to strengthen candidate scoring
- [ ] Auction-result tracking — recording final closing prices so scoring can learn what quality names actually sell for within budget
- [ ] Additional auction sources as free access becomes available

## Important notes

- **Prices are snapshots.** Feed prices can be hours old and good names rise fast — always verify the live price and end time on the auction site before bidding.
- **Trademark risk is real.** A name passing these filters is not a legal clearance. Always check TMview / USPTO yourself before buying.
- **No automated buying.** This script and the agents that read its feed never place bids, backorders, or payments.

## Maintainer

**Fahad** — web designer, digital marketer, and domain investor based in Chattogram, Bangladesh. This is a solo, unfunded open-source project, built and maintained with the goal of giving small investors in emerging markets the same data advantage that large portfolio holders already have.

Contributions, issues, and forks are welcome.
