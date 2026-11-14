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
    if len(sld) < 5 or len(sld) > MAX_SLD_LEN:
        return False, tld
    if tld in WATCH_TLDS and len(sld) > 8:
        return False, tld
    if not looks_like_words(sld):
        return False, tld
    return True, tld


# ---------- Flexible field access (feeds change their column names) ----------
DOMAIN_KEYS = ["domainname", "domain", "domain name", "name", "domain_name"]
PRICE_KEYS = ["price", "bid price", "currentprice", "current price", "currentbid", "current bid",
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


TLDCOUNT_KEYS = ["dotdb tld count", "keywordregistrations", "exactmatchtlds", "developedtlds"]
VOLUME_KEYS = ["semrushsearchvolume", "search volume"]
AGE_KEYS = ["domainage", "age"]
REFDOM_KEYS = ["majesticreferringdomains", "semrushreferringdomains", "links"]
ADULT_KEYS = ["isadult"]


def to_num(val):
    if val is None or val == "":
        return 0.0
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, (list, tuple)):
        return float(len(val))
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(val))
    try:
        return float(m.group(0).replace(",", "")) if m else 0.0
    except ValueError:
        return 0.0


def best(row, keys):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    return max((to_num(low.get(k)) for k in keys), default=0.0)


def normalise(row, source):
    domain = pick(row, DOMAIN_KEYS)
    if not domain:
        return None
    price = to_price(pick(row, PRICE_KEYS))
    adult = pick(row, ADULT_KEYS)
    return {
        "domain": str(domain).strip().lower(),
        "source": source,
        "type": pick(row, TYPE_KEYS) or "",
        "price_usd": price,
        "bids": "" if pick(row, BIDS_KEYS) is None else pick(row, BIDS_KEYS),
        "ends": pick(row, END_KEYS) or "",
        "tld_count": int(best(row, TLDCOUNT_KEYS)),
        "search_volume": int(best(row, VOLUME_KEYS)),
        "age_years": int(best(row, AGE_KEYS)),
        "ref_domains": int(best(row, REFDOM_KEYS)),
        "adult": str(adult).lower() in ("true", "1", "yes"),
    }


# ---------- Dictionary: keep only real words or clean two-word combos ----------
WORDS = set()
SHORT_OK = {"go", "my", "up"}
BLOCK_WORDS = {"ass", "sex", "porn", "xxx", "nude", "nudes", "slut", "milf", "fetish", "escort",
               "casino", "bet", "bets", "betting", "viagra", "cialis", "loan", "loans", "vape", "weed",
               "kill", "drug", "drugs", "gun", "guns"}


def load_words():
    for path in ["/usr/share/dict/american-english", "/usr/share/dict/words"]:
        try:
            for raw in Path(path).read_text(encoding="utf-8", errors="ignore").split():
                raw = raw.strip()
                if not raw or raw[0].isupper():      # skip names like Gail, Ron, Jimmie
                    continue
                w = raw.lower()
                if w.isascii() and w.isalpha() and len(w) >= 3 and "'" not in w:
                    WORDS.add(w)
        except OSError:
            continue
    log(f"Dictionary words loaded: {len(WORDS)}")


def is_word(w):
    return w in WORDS or w in SHORT_OK


def split_words(sld):
    """Return 'word' or 'word+word' if the name is one or two real words, else ''."""
    if sld in WORDS:
        return "" if sld in BLOCK_WORDS else sld
    for i in range(2, len(sld) - 1):
        a, b = sld[:i], sld[i:]
        if a in BLOCK_WORDS or b in BLOCK_WORDS:
            continue
        if (a in WORDS or a in SHORT_OK) and b in WORDS and len(b) >= 3:
            return f"{a}+{b}"
    # three parts, e.g. park+and+charge
    for i in range(2, len(sld) - 3):
        for j in range(i + 1, len(sld) - 1):
            a, b, c = sld[:i], sld[i:j], sld[j:]
            if a in WORDS and c in WORDS and b in CONNECTORS and not ({a, c} & BLOCK_WORDS):
                return f"{a}+{b}+{c}"
    return ""


CONNECTORS = {"and", "n", "the", "for", "of", "my", "to", "in", "on", "or"}


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
    load_words()
    all_rows = load_godaddy() + load_dynadot()
    log(f"Total records downloaded: {len(all_rows)}")

    seen, buy, watch = set(), [], []
    for r in all_rows:
        d = r["domain"]
        if d in seen:
            continue
        seen.add(d)
        ok, tld = passes_hard_filters(d)
        if not ok or r["adult"]:
            continue
        sld = d.split(".", 1)[0]
        words = split_words(sld) if WORDS else ""
        if WORDS and not words:
            continue                      # not one or two real words
        r["words"] = words
        # Signal score: how many other extensions are taken, real search demand, age, links.
        import math
        r["signal_score"] = round(
            min(r["tld_count"], 25) * 2
            + math.log10(r["search_volume"] + 1) * 8
            + min(r["age_years"], 20) * 0.5
            + math.log10(r["ref_domains"] + 1) * 3
            + (5 if "+" not in words else 0),   # single real word bonus
            1,
        )
        if tld in WATCH_TLDS:
            watch.append(r)
            continue
        if r["price_usd"] is None or r["price_usd"] > MAX_PRICE_USD:
            continue
        if r["tld_count"] < 2 and r["search_volume"] == 0:
            continue                      # no sign anyone else wants this keyword
        buy.append(r)

    buy.sort(key=lambda r: (-r["signal_score"], r["price_usd"] or 0))
    watch.sort(key=lambda r: -r["signal_score"])
    buy, watch = buy[:MAX_ROWS], watch[:MAX_AI_ROWS]

    fields = ["domain", "words", "signal_score", "source", "type", "price_usd", "bids", "ends",
              "tld_count", "search_volume", "age_years", "ref_domains"]
    for path, rows in [(OUT / "candidates.csv", buy), (OUT / "ai_watch.csv", watch)]:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    log(f"Shortlist: {len(buy)} buy candidates (.com/.si <= ${MAX_PRICE_USD:.0f}), "
        f"{len(watch)} .ai watch names (out of {len(seen)} unique domains). Took {time.time() - started:.0f}s")
    (OUT / "run_log.txt").write_text(
        f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}\n" + "\n".join(log_lines) + "\n",
        encoding="utf-8",
    )
    if not all_rows:
        sys.exit("No data downloaded from any source - see run_log.txt")


if __name__ == "__main__":
    main()
