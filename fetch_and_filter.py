"""
Domain feed for Fahad's sleeper agent.

Downloads public auction inventory (GoDaddy Auctions inventory files, Dynadot
expired-auction CSV, DropCatch's official auction list download, and - when an
API key is set - Namecheap Market's official Auctions API), applies the hard
filters from the domain-investing-research skill, and writes a small shortlist
the daily Claude agent can read.

Only the Python standard library is used, so GitHub Actions needs no installs.
"""

import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
try:                                   # word commonness (installed in feed.yml: pip install wordfreq)
    from wordfreq import zipf_frequency
except ImportError:
    zipf_frequency = None
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- Rules (from the skill; change here if Fahad's rules change) ----------
BUY_TLDS = {"com", "si"}          # extensions Fahad buys
WATCH_TLDS = {"ai"}               # watchlist only
MAX_PRICE_USD = 50.0              # expired/auction budget cap
MAX_SLD_LEN = 15                  # longest second-level name we keep
MAX_ROWS = 400                    # cap the shortlist so the agent stays cheap
MAX_AI_ROWS = 100
DROPCATCH_MAX_USD = 60.0          # Fahad raised the cap to $60 for DropCatch only (standard .com backorder = $59)
MAX_DC_AUCTION_ROWS = 30          # DropCatch auctions (current price only on the site)
MAX_DC_BACKORDER_ROWS = 50        # DropCatch pending-delete list ($59 backorder, verified 2026-11-15)

GODADDY_INDEX = "https://inventory.auctions.godaddy.com/"
GODADDY_METADATA = "https://inventory.auctions.godaddy.com/metadata.json"
DYNADOT_CSV = (
    "https://www.dynadot.com/market/auction/auctions.csv"
    "?order_by=8&currency=USD&timezone=PST"
)
# DropCatch "Downloads" page (dropcatch.com/downloads): this endpoint returns a
# short-lived link to the official auction list (CSV inside a zip, refreshed every 15 min).
DROPCATCH_FILE_URL = (
    "https://client.dropcatch.com/GetFileUrl"
    "?FileType=csv&RequestType=Auction&AuctionType=AllAuctions"
)
# Pending-delete (backorder) list. Parameters taken from dropcatch.com/downloads site code and
# tested 2026-11-15: RequestType=Dropping, BackorderDay=DaysOut0..DaysOut4 or AllDays.
# File columns: Domain, TLD, Type, Drop Date. Min standard backorder for .com = $59.
DROPCATCH_BACKORDER_URL = (
    "https://client.dropcatch.com/GetFileUrl"
    "?FileType=csv&RequestType=Dropping&BackorderDay=AllDays"
)
# Namecheap Market Auctions API (docs: https://aftermarketapi.namecheap.com/client/docs/).
# Needs a key from the Namecheap Market Dashboard, stored as the GitHub secret
# NAMECHEAP_MARKET_API_KEY. Without the key this source is skipped.
NAMECHEAP_API = "https://aftermarketapi.namecheap.com/client/api/sales"
NAMECHEAP_MAX_PAGES = 40          # 100 per page -> at most 4000 auctions per query

OUT = Path("data")
UA = "Mozilla/5.0 (domain-feed; personal research script)"
log_lines = []


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    log_lines.append(line)


def fetch(url, timeout=120, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
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
END_KEYS = ["drop date", "auctionendtime", "endtime", "end time", "end date", "enddate",
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


TLDCOUNT_KEYS = ["dotdb tld count", "keywordregistrations", "exactmatchtlds", "developedtlds",
                 "extensionstaken"]
VOLUME_KEYS = ["semrushsearchvolume", "search volume", "keywordsearchcount"]
AGE_KEYS = ["domainage", "age"]
REFDOM_KEYS = ["majesticreferringdomains", "semrushreferringdomains", "links", "backlinkscount"]
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


_now = datetime.now(timezone.utc)
TODAY = _now.strftime("%Y-%m-%d")
DROP_CUTOFF = TODAY if _now.hour < 12 else (_now + timedelta(days=1)).strftime("%Y-%m-%d")
# Grammar words: very common, so word-frequency alone would push junk like "forwas" or "canwho" up.
FUNCTION_WORDS = set("""a an the and or nor but so yet for of to in on at by with from as into onto upon than too
very such only just also even still any all some each every both either neither not no yes this that these those
there here be being been is are am was were has had have having do does did done doing can could would should will
shall may might must he she it we you me him her his hers its our ours your yours their theirs them they us i my
who whom whose which what when where why how whether because though although while again ever never always often
own same other another much many more most less least few out off over under up down let lets said say says got
went goes gone made make makes making used about around back then now well really against without within among
between toward towards one two three four five first last next thing things something someone anything nothing
everything anyone everyone come came comes take took get gets see saw seen know knew think thought want give gave
look looked use way per via ok etc put puts set sets keep kept""".split())
# Generic nouns: fine next to a real keyword, but a name made only of these is weak (dayswho, mantime).
GENERIC_WORDS = set("""day days time times year years man men woman women people person world life good great best
better new old high low big small long right left real true""".split())


def load_dropcatch(url=DROPCATCH_FILE_URL, kind="auction"):
    """Official DropCatch auction list. It has no prices or metrics, only
    Domain, TLD, Type (Dropped / PreRelease / PrivateSeller) and Auction End."""
    rows = []
    try:
        meta = json.loads(fetch(url, timeout=60, headers={
            "Accept": "application/json",
            "Origin": "https://www.dropcatch.com",
            "Referer": "https://www.dropcatch.com/downloads",
        }))
        file_url = (meta.get("result") or {}).get("fileUrl")
        if not file_url:
            log(f"DropCatch: no file link in response: {str(meta)[:200]}")
            return rows
        raw = fetch(file_url, timeout=300)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            text = z.read(name).decode("utf-8-sig", "ignore")
        # The file starts with a notice line ("****Auction lists are generated...") before the header.
        lines = text.splitlines()
        start = next((i for i, l in enumerate(lines) if l.lower().startswith("domain,")), 0)
        reader = csv.DictReader(lines[start:])
        log(f"DropCatch file {name}, columns: {reader.fieldnames}")
        for rec in reader:
            r = normalise(rec, "DropCatch")
            if r:
                r["no_metrics"] = kind
                rows.append(r)
        log(f"DropCatch {kind}: {len(rows)} records")
    except Exception as e:
        log(f"DropCatch {kind} download failed: {e}")
    return rows


def load_namecheap():
    """Namecheap Market Auctions API. Skipped when NAMECHEAP_MARKET_API_KEY is not set."""
    key = os.environ.get("NAMECHEAP_MARKET_API_KEY", "").strip()
    if not key:
        log("Namecheap: skipped (NAMECHEAP_MARKET_API_KEY secret not set)")
        return []
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    queries = [
        # Buy extensions inside the budget.
        {"tld": ",".join(sorted(BUY_TLDS)), "price": f"_{int(MAX_PRICE_USD)}",
         "nameLength": f"5_{MAX_SLD_LEN}"},
        # .ai watchlist: short names only, any price.
        {"tld": ",".join(sorted(WATCH_TLDS)), "nameLength": "5_8"},
    ]
    rows = []
    now_year = datetime.now(timezone.utc).year
    for q in queries:
        params = {**q, "noHyphens": "true", "noNumbers": "true", "nsfw": "false",
                  "pageSize": "100", "orderBy": "end_time", "direction": "asc"}
        cursor, pages, n = None, 0, 0
        try:
            while pages < NAMECHEAP_MAX_PAGES:
                if cursor:
                    params["cursor"] = cursor
                url = NAMECHEAP_API + "?" + urllib.parse.urlencode(params)
                data = json.loads(fetch(url, timeout=60, headers=headers))
                for rec in data.get("items") or []:
                    rec = dict(rec)
                    reg = str(rec.get("registeredDate") or "")[:4]
                    if reg.isdigit():
                        rec["age"] = max(0, now_year - int(reg))
                    rec["type"] = rec.get("saleType") or rec.get("auctionType") or ""
                    r = normalise(rec, "Namecheap")
                    if r:
                        rows.append(r)
                        n += 1
                pages += 1
                cursor = data.get("nextCursor")
                if not data.get("hasMore") or not cursor:
                    break
                time.sleep(0.5)               # stay gentle with the API
            log(f"Namecheap tld={q['tld']}: {n} records ({pages} pages)")
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "ignore")
            log(f"Namecheap API error {e.code} for tld={q['tld']}: {body}")
            if e.code in (401, 403):
                break                         # bad key or phone not verified; no point retrying
        except Exception as e:
            log(f"Namecheap API failed for tld={q['tld']}: {e}")
    return rows


# ---------- Main ----------
def main():
    OUT.mkdir(exist_ok=True)
    started = time.time()
    load_words()
    # Sources with prices and metrics come first, so a domain listed in several
    # places keeps the richer record.
    all_rows = (load_godaddy() + load_dynadot() + load_namecheap() + load_dropcatch()
                + load_dropcatch(DROPCATCH_BACKORDER_URL, "backorder"))
    log(f"Total records downloaded: {len(all_rows)}")

    seen, buy, watch, dc_auction, dc_backorder = set(), [], [], [], []
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
        if r.get("no_metrics"):
            # DropCatch: no price or metrics in the file. Keep clean real-word names in
            # their own slots; the agent checks the live price on dropcatch.com.
            if tld in WATCH_TLDS:
                continue
            # No metrics in DropCatch files, so rank by how common the words are (wordfreq Zipf:
            # ~3 = rare, 5+ = everyday). Obscure words and inflections (forefeet, affronts) drop out.
            parts = words.split("+") if words else [sld]
            if zipf_frequency:
                if parts[0] in CONNECTORS or parts[-1] in CONNECTORS:
                    continue                  # "teefor", "forwas" style
                core = [w for w in parts if w not in CONNECTORS]
                if any(w in FUNCTION_WORDS for w in core):
                    continue                  # "canwho", "howour": grammar words, not a business name
                if len(core) > 1 and all(w in GENERIC_WORDS for w in core):
                    continue                  # "daysgood", "bestday"
                zs = [min(zipf_frequency(w, "en"), 5.5) for w in core]
                if not zs or min(zs) < (3.3 if len(core) == 1 else 3.5):
                    continue                  # rare words / odd inflections (forefeet, affronts)
                avg = sum(zs) / len(zs)
                r["signal_score"] = round(avg * 10 - (0 if len(core) == 1 else 5) - len(sld) * 0.3, 1)
            else:
                r["signal_score"] = round((20 if len(parts) == 1 else 10) + (15 - len(sld)), 1)
            if r["no_metrics"] == "backorder" and r["ends"] and str(r["ends"])[:10] < DROP_CUTOFF:
                continue                      # already dropped
            if r["no_metrics"] == "backorder":
                r["price_usd"] = 59
                r["type"] = "DropCatch backorder $59 (pending delete)"
                dc_backorder.append(r)
            else:
                r["price_usd"] = ""
                r["type"] = f"{r['type']} (price on site)".strip()
                dc_auction.append(r)
            continue
        # Signal score: how many other extensions are taken, real search demand, age, links.
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
    dc_auction.sort(key=lambda r: -r["signal_score"])
    dc_backorder.sort(key=lambda r: -r["signal_score"])
    dc = dc_auction[:MAX_DC_AUCTION_ROWS] + dc_backorder[:MAX_DC_BACKORDER_ROWS]
    buy = buy[:MAX_ROWS - len(dc)] + dc
    watch = watch[:MAX_AI_ROWS]

    fields = ["domain", "words", "signal_score", "source", "type", "price_usd", "bids", "ends",
              "tld_count", "search_volume", "age_years", "ref_domains"]
    for path, rows in [(OUT / "candidates.csv", buy), (OUT / "ai_watch.csv", watch)]:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    by_source = {}
    for r in buy:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    log(f"Shortlist by source: {by_source}")
    log(f"Shortlist: {len(buy)} buy candidates (.com/.si <= ${MAX_PRICE_USD:.0f}; DropCatch <= ${DROPCATCH_MAX_USD:.0f}), "
        f"{len(watch)} .ai watch names (out of {len(seen)} unique domains). Took {time.time() - started:.0f}s")
    (OUT / "run_log.txt").write_text(
        f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}\n" + "\n".join(log_lines) + "\n",
        encoding="utf-8",
    )
    if not all_rows:
        sys.exit("No data downloaded from any source - see run_log.txt")


if __name__ == "__main__":
    main()
