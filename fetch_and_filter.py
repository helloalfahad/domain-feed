"""
Domain feed for Fahad's sleeper agent.

Downloads public auction inventory (GoDaddy Auctions inventory files + Dynadot
expired-auction CSV), applies the hard filters from the domain-investing-research
skill, and writes a small shortlist the daily Claude agent can read.

Only the Python standard library is used, so GitHub Actions needs no installs.
"""

import csv
import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ---------- Rules (from the skill; change here if Fahad's rules change) ----------
BUY_TLDS = {"com", "si"}          # extensions Fahad buys
WATCH_TLDS = {"ai"}               # watchlist only
MAX_PRICE_USD = 50.0              # expired/auction budget cap
MAX_SLD_LEN = 15                  # longest second-level name we keep
MAX_ROWS = 400                    # cap the shortlist so the agent stays cheap
MAX_AI_ROWS = 100

GODADDY_INDEX = "https://inventory.auctions.godaddy.com/"
GODADDY_METADATA = "https://inventory.auctions.godaddy.com/metadata.json"
DYNADOT_CSV = (
    "https://www.dynadot.com/market/auction/auctions.csv"
    "?order_by=8&currency=USD&timezone=PST"
)

OUT = Path("data")
UA = "Mozilla/5.0 (domain-feed; personal research script)"
log_lines = []


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    log_lines.append(line)


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- Name filters ----------
VOWELS = set("aeiouy")


def looks_like_words(sld):
    """Cheap gibberish check: needs vowels and no long consonant runs."""
    if not any(c in VOWELS for c in sld):
        return False
    if re.search(r"[^aeiouy]{5,}", sld):
        return False
    if re.search(r"(.)\1\1", sld):          # same letter 3x in a row
        return False
    return True


def split_domain(domain):
    domain = (domain or "").strip().lower().rstrip(".")
    if "." not in domain:
        return None, None
    sld, tld = domain.split(".", 1)
    return sld, tld


def passes_hard_filters(domain):
    sld, tld = split_domain(domain)
    if not sld:
        return False, None
    if tld not in BUY_TLDS | WATCH_TLDS:
        return False, tld
    if "-" in sld or any(ch.isdigit() for ch in sld):
        return False, tld
    if not sld.isascii() or not sld.isalpha():
        return False, tld
    if len(sld) < 3 or len(sld) > MAX_SLD_LEN:
        return False, tld
    if tld in WATCH_TLDS and len(sld) > 8:
        return False, tld
    if not looks_like_words(sld):
        return False, tld
    return True, tld


# ---------- Flexible field access (feeds change their column names) ----------
DOMAIN_KEYS = ["domainname", "domain", "domain name", "name", "domain_name"]
PRICE_KEYS = ["price", "currentprice", "current price", "currentbid", "current bid",
              "minbid", "min bid", "bidprice", "buynowprice", "buy now price", "amount"]
BIDS_KEYS = ["numberofbids", "bids", "bid count", "bidcount", "num bids"]
END_KEYS = ["auctionendtime", "endtime", "end time", "end date", "enddate",
            "auction end", "time left", "timeleft", "expires"]
TYPE_KEYS = ["auctiontype", "type", "listingtype"]


def pick(row, keys):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def to_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r"[\d,]+(?:\.\d+)?", str(val))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def normalise(row, source):
    domain = pick(row, DOMAIN_KEYS)
    if not domain:
        return None
    price = to_price(pick(row, PRICE_KEYS))
    return {
        "domain": str(domain).strip().lower(),
        "source": source,
        "type": pick(row, TYPE_KEYS) or "",
        "price_usd": price,
        "bids": "" if pick(row, BIDS_KEYS) is None else pick(row, BIDS_KEYS),
        "ends": pick(row, END_KEYS) or "",
    }


# ---------- Sources ----------
def iter_json_records(obj):
    """Yield dict records from whatever shape the JSON has."""
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                yield from v


def godaddy_file_urls():
    """Find JSON inventory file URLs from metadata.json (fallback: index page links)."""
    urls = []
    try:
        meta = json.loads(fetch(GODADDY_METADATA))
        text = json.dumps(meta)
        urls = re.findall(r'[\w./:-]+\.json\.zip', text)
        log(f"GoDaddy metadata.json ok, {len(urls)} json.zip names found")
    except Exception as e:
        log(f"GoDaddy metadata.json failed: {e}")
    if not urls:
        try:
            html = fetch(GODADDY_INDEX).decode("utf-8", "ignore")
            urls = re.findall(r'href="([^"]+\.json\.zip)"', html)
            log(f"GoDaddy index page: {len(urls)} json.zip links")
        except Exception as e:
            log(f"GoDaddy index failed: {e}")
    full = []
    for u in urls:
        if not u.startswith("http"):
            u = GODADDY_INDEX.rstrip("/") + "/" + u.lstrip("/")
        full.append(u)
    # Expiring + closeout lists are what we want; skip duplicates.
    wanted = [u for u in full if re.search(r"expir|closeout|ending|tdnam|all", u, re.I)]
    return list(dict.fromkeys(wanted or full))[:6]


def load_godaddy():
    rows, first_keys = [], None
    for url in godaddy_file_urls():
        try:
            raw = fetch(url, timeout=300)
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for name in z.namelist():
                    if not name.lower().endswith(".json"):
                        continue
                    data = json.loads(z.read(name))
                    n = 0
                    for rec in iter_json_records(data):
                        if first_keys is None:
                            first_keys = list(rec.keys())
                        r = normalise(rec, "GoDaddy")
                        if r:
                            rows.append(r)
                            n += 1
                    log(f"GoDaddy {url.rsplit('/', 1)[-1]}:{name} -> {n} records")
        except Exception as e:
            log(f"GoDaddy file failed {url}: {e}")
    if first_keys:
        log(f"GoDaddy record keys seen: {first_keys}")
    return rows


def load_dynadot():
    rows = []
    try:
        text = fetch(DYNADOT_CSV, timeout=300).decode("utf-8-sig", "ignore")
        reader = csv.DictReader(io.StringIO(text))
        log(f"Dynadot CSV columns: {reader.fieldnames}")
        for rec in reader:
            r = normalise(rec, "Dynadot")
            if r:
                rows.append(r)
        log(f"Dynadot: {len(rows)} records")
    except Exception as e:
        log(f"Dynadot CSV failed: {e}")
    return rows


# ---------- Main ----------
def main():
    OUT.mkdir(exist_ok=True)
    started = time.time()
    all_rows = load_godaddy() + load_dynadot()
    log(f"Total records downloaded: {len(all_rows)}")

    seen, buy, watch = set(), [], []
    for r in all_rows:
        d = r["domain"]
        if d in seen:
            continue
        seen.add(d)
        ok, tld = passes_hard_filters(d)
        if not ok:
            continue
        if tld in WATCH_TLDS:
            watch.append(r)
            continue
        if r["price_usd"] is None or r["price_usd"] > MAX_PRICE_USD:
            continue
        buy.append(r)

    # Shorter names first, then cheaper.
    buy.sort(key=lambda r: (len(r["domain"]), r["price_usd"] or 0))
    watch.sort(key=lambda r: len(r["domain"]))
    buy, watch = buy[:MAX_ROWS], watch[:MAX_AI_ROWS]

    fields = ["domain", "source", "type", "price_usd", "bids", "ends"]
    for path, rows in [(OUT / "candidates.csv", buy), (OUT / "ai_watch.csv", watch)]:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    log(f"Shortlist: {len(buy)} buy candidates (.com/.si <= ${MAX_PRICE_USD:.0f}), "
        f"{len(watch)} .ai watch names. Took {time.time() - started:.0f}s")
    (OUT / "run_log.txt").write_text(
        f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}\n" + "\n".join(log_lines) + "\n",
        encoding="utf-8",
    )
    if not all_rows:
        sys.exit("No data downloaded from any source - see run_log.txt")


if __name__ == "__main__":
    main()
