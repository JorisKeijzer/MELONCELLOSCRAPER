"""Vind alle Nederlandse slijterijen/drankwinkels die Dolce Cilento (meloncello) verkopen, plus hun e-mailadres.

Stappen:
  1. discover  - haalt ALLE slijterijen/wijnwinkels/drankwinkels in Nederland op uit OpenStreetMap
                 (naam, adres, website, e-mail, telefoon) en voegt de handmatige lijst toe.
  2. check     - bezoekt elke website: zoekt via sitemap.xml en de zoekfunctie van de webshop naar
                 "Dolce Cilento" / "meloncello" en haalt e-mailadressen van home- en contactpagina's.

Gebruik:
    pip install -r requirements.txt
    python scraper.py discover          # -> data/winkels.csv
    python scraper.py check             # -> data/resultaat.csv (kan onderbroken en hervat worden)
    python scraper.py all               # beide

    python scraper.py check --extra urls.txt   # extra websites (één per regel) meenemen
    python scraper.py discover --import kvk_slijterijen.csv   # eigen lijst (bv. KvK-export) toevoegen
"""

import argparse
import csv
import gzip
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

DATA_DIR = "data"
SEED_CSV = os.path.join(DATA_DIR, "dolce_cilento_verkooppunten_nl.csv")
SHOPS_CSV = os.path.join(DATA_DIR, "winkels.csv")
CACHE_JSONL = os.path.join(DATA_DIR, "cache.jsonl")
RESULT_CSV = os.path.join(DATA_DIR, "resultaat.csv")
BUSY_FILE = os.path.join(DATA_DIR, "bezig.txt")

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_HEADERS = {
    "User-Agent": "meloncelloscraper/2.0 (+https://github.com/joriskeijzer/meloncelloscraper)",
    "Accept": "application/json",
}
OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="NL"][admin_level=2]->.nl;
(
  nwr["shop"~"^(alcohol|wine|beverages)$"](area.nl);
  nwr["craft"="distillery"](area.nl);
);
out center tags;
"""

USER_AGENT = "Mozilla/5.0 (compatible; MeloncelloScraper/2.0; +contact via site owner)"
TIMEOUT = 15
MAX_SITEMAPS = 40
MAX_CONTACT_PAGES = 6

BRAND_RE = re.compile(r"dolce[\s\-_]*cilento", re.I)
MELON_RE = re.compile(r"meloncello", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK_EMAIL = re.compile(
    r"\.(png|jpe?g|gif|webp|svg)$|sentry|wixpress|example\.|domain\.|yourdomain|email\.com$|@2x|godaddy",
    re.I,
)
CONTACT_WORDS = ("contact", "over-ons", "over ons", "klantenservice", "adres", "impressum", "voorwaarden", "service")
CONTACT_PATHS = ["/contact", "/contact/", "/over-ons", "/klantenservice"]
SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/product-sitemap.xml", "/sitemap_products_1.xml"]
# Zoek-URL's van veelgebruikte webshopplatforms (WooCommerce, Shopify, Magento, Lightspeed, CCV, topSlijter, ...)
SEARCH_PATHS = [
    "/?s={q}&post_type=product",
    "/search?q={q}",
    "/catalogsearch/result/?q={q}",
    "/search/{q}/",
    "/zoeken?q={q}",
    "/webshop/zoeken?zoekterm={q}",
    "/Zoeken?q={q}",
]

FIELDS = ["naam", "plaats", "adres", "website", "email", "telefoon", "bron"]
RESULT_FIELDS = [
    "verkoopt_dolce_cilento", "meloncello_gevonden", "naam", "plaats", "adres", "emails", "telefoon",
    "website", "gevonden_urls", "methode", "fout",
]

_thread = threading.local()


# ----------------------------------------------------------------------------- helpers

def session():
    if not hasattr(_thread, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"})
        _thread.s = s
    return _thread.s


def fetch(url, binary=False):
    try:
        r = session().get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None, None
    if not r.ok:
        return None, r.url
    return (r.content if binary else r.text), r.url


def normalize_site(url):
    url = (url or "").strip().split()[0] if url else ""
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc or p.netloc.endswith(("facebook.com", "instagram.com", "google.com")):
        return ""
    return f"{p.scheme}://{p.netloc}"


def domain(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def decode_cfemail(encoded):
    key = int(encoded[:2], 16)
    return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))


def extract_emails(html):
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.select('a[href^="mailto:"]'):
        found.add(a["href"][7:].split("?")[0])
    for el in soup.select("[data-cfemail]"):
        try:
            found.add(decode_cfemail(el["data-cfemail"]))
        except ValueError:
            pass
    text = soup.get_text(" ").replace("[at]", "@").replace("(at)", "@").replace(" @ ", "@")
    found.update(EMAIL_RE.findall(text))
    return {e.strip().strip(".").lower() for e in found if e and not JUNK_EMAIL.search(e)}


def contact_links(base, html):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        label = (a.get_text(" ") + " " + a["href"]).lower()
        if any(w in label for w in CONTACT_WORDS):
            link = urljoin(base, a["href"]).split("#")[0]
            if domain(link) == domain(base) and link not in links:
                links.append(link)
    return links


# ----------------------------------------------------------------------------- stap 1: discover

COLUMN_ALIASES = {
    "naam": ("naam", "bedrijfsnaam", "handelsnaam", "name", "bedrijf"),
    "plaats": ("plaats", "woonplaats", "vestigingsplaats", "city", "stad"),
    "adres": ("adres", "straat", "address", "vestigingsadres"),
    "website": ("website", "url", "internetadres", "site", "domein"),
    "email": ("email", "e-mail", "e-mailadres", "emailadres", "mail"),
    "telefoon": ("telefoon", "telefoonnummer", "phone", "tel"),
}


def read_import(path):
    """Lees een eigen lijst (bijv. KvK-export, SBI 47250) met willekeurige kolomnamen; ; of , als scheidingsteken."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        reader = csv.DictReader(f, delimiter=";" if sample.count(";") > sample.count(",") else ",")
        lower = {c.lower().strip(): c for c in reader.fieldnames or []}
        cols = {k: next((lower[a] for a in aliases if a in lower), None) for k, aliases in COLUMN_ALIASES.items()}
        for row in reader:
            yield {k: (row.get(c) or "").strip() if c else "" for k, c in cols.items()}


def discover(imports=()):
    shops = {}

    if os.path.exists(SEED_CSV):
        with open(SEED_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter=";"):
                site = normalize_site(row["website"])
                shops[domain(site)] = {
                    "naam": row["bedrijf"], "plaats": row["plaats"], "adres": row["adres"], "website": site,
                    "email": row["email"], "telefoon": row["telefoon"], "bron": "handmatig",
                }

    elements = []
    for url in OVERPASS_URLS:
        try:
            print(f"OpenStreetMap ophalen via {url} (kan 1-3 minuten duren)...")
            r = requests.post(url, data={"data": OVERPASS_QUERY}, timeout=300, headers=OVERPASS_HEADERS)
            r.raise_for_status()
            elements = r.json()["elements"]
            break
        except (requests.RequestException, ValueError) as e:
            print(f"  mislukt: {e}")
    if not elements:
        print("Geen enkele OpenStreetMap-server gaf antwoord. Probeer het over een paar minuten opnieuw.")
    print(f"{len(elements)} winkels gevonden in OpenStreetMap")

    for path in imports:
        n = 0
        for row in read_import(path):
            site = normalize_site(row["website"])
            key = domain(site) if site else f"import-{row['naam'].lower()}-{row['plaats'].lower()}"
            if key in shops:
                for k in ("email", "telefoon", "adres"):
                    shops[key][k] = shops[key][k] or row[k]
                continue
            shops[key] = dict(row, website=site, bron=os.path.basename(path))
            n += 1
        print(f"{n} nieuwe winkels uit {path}")

    for el in elements:
        t = el.get("tags", {})
        site = normalize_site(t.get("website") or t.get("contact:website") or t.get("url"))
        street = " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber")]))
        adres = ", ".join(filter(None, [street, " ".join(filter(None, [t.get("addr:postcode"), t.get("addr:city")]))]))
        row = {
            "naam": t.get("name", ""), "plaats": t.get("addr:city", ""), "adres": adres, "website": site,
            "email": t.get("email") or t.get("contact:email", ""),
            "telefoon": t.get("phone") or t.get("contact:phone", ""), "bron": "openstreetmap",
        }
        if not site:
            key = f"osm-{el['type']}-{el['id']}"
        else:
            key = domain(site)
            if key in shops:  # ketens met één website: eerste vermelding houden, e-mail aanvullen
                shops[key]["email"] = shops[key]["email"] or row["email"]
                continue
        shops[key] = row

    with open(SHOPS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";")
        w.writeheader()
        w.writerows(shops.values())
    no_site = sum(not s["website"] for s in shops.values())
    print(f"{len(shops)} unieke winkels -> {SHOPS_CSV} ({no_site} zonder website: die komen wel in de lijst, maar worden niet gecheckt)")


# ----------------------------------------------------------------------------- stap 2: check

def robots_for(base):
    rp = RobotFileParser()
    text, _ = fetch(base + "/robots.txt")
    rp.parse((text or "").splitlines())
    sitemaps = rp.site_maps() or []
    return rp, sitemaps


def sitemap_matches(base, extra_sitemaps):
    """Loop door (geneste) sitemaps en geef product-URL's terug met cilento/meloncello in de URL."""
    queue = list(dict.fromkeys(extra_sitemaps + [base + p for p in SITEMAP_PATHS]))
    seen, matches, any_sitemap = set(), [], False
    while queue and len(seen) < MAX_SITEMAPS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        content, _ = fetch(url, binary=True)
        if not content:
            continue
        if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
            try:
                content = gzip.decompress(content)
            except OSError:
                continue
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            continue
        any_sitemap = True
        for loc in root.iter():
            if not loc.tag.endswith("loc") or not loc.text:
                continue
            link = loc.text.strip()
            if root.tag.endswith("sitemapindex"):
                # productsitemaps eerst
                (queue.insert(0, link) if "product" in link.lower() else queue.append(link))
            elif BRAND_RE.search(link) or MELON_RE.search(link):
                matches.append(link)
    return any_sitemap, matches


def search_matches(base, rp):
    """Gebruik de zoekfunctie van de webshop. Zoekterm is 'dolce cilento'; we tellen alleen een hit als
    de resultaatpagina ook 'meloncello' of een productlink naar dolce-cilento bevat (niet alleen de echo van de zoekterm)."""
    q = quote_plus("dolce cilento")
    for pattern in SEARCH_PATHS:
        url = base + pattern.format(q=q if "{q}/" not in pattern else "dolce-cilento")
        if not rp.can_fetch(USER_AGENT, url):
            continue
        html, final = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [urljoin(final, a["href"]) for a in soup.find_all("a", href=True)]
        product_links = [h for h in hrefs if BRAND_RE.search(h) and domain(h) == domain(base) and "search" not in h.lower()
                         and "zoek" not in h.lower() and "?s=" not in h]
        melon_in_text = bool(MELON_RE.search(soup.get_text(" ")))
        if product_links or melon_in_text:
            return list(dict.fromkeys(product_links))[:10], melon_in_text, url
    return [], False, None


def find_emails(base, rp, known):
    emails = set(filter(None, [known.lower()])) if known else set()
    home, final = fetch(base)
    if home is None:
        return emails, False
    emails |= extract_emails(home)
    pages = contact_links(final or base, home) + [base + p for p in CONTACT_PATHS]
    for page in list(dict.fromkeys(pages))[:MAX_CONTACT_PAGES]:
        if not rp.can_fetch(USER_AGENT, page):
            continue
        html, _ = fetch(page)
        if html:
            emails |= extract_emails(html)
    return emails, True


def check_shop(shop):
    base = shop["website"]
    result = {k: shop.get(k, "") for k in ("naam", "plaats", "adres", "telefoon", "website")}
    result.update(verkoopt_dolce_cilento="onbekend", meloncello_gevonden="", emails=shop.get("email", ""),
                  gevonden_urls="", methode="", fout="")
    try:
        rp, sitemaps = robots_for(base)
        emails, reachable = find_emails(base, rp, shop.get("email", ""))
        result["emails"] = ", ".join(sorted(emails))
        if not reachable:
            result["fout"] = "website niet bereikbaar"
            return result

        has_sitemap, urls = sitemap_matches(base, sitemaps)
        method = "sitemap"
        melon = any(MELON_RE.search(u) for u in urls)
        if not urls:
            urls, melon_text, search_url = search_matches(base, rp)
            melon = melon or melon_text or any(MELON_RE.search(u) for u in urls)
            method = f"zoekfunctie ({search_url})" if search_url else ("sitemap" if has_sitemap else "")

        if urls or melon:
            result["verkoopt_dolce_cilento"] = "ja"
            result["meloncello_gevonden"] = "ja" if melon else "nee (wel ander Dolce Cilento product)"
            result["gevonden_urls"] = " | ".join(urls[:10])
            result["methode"] = method
        elif has_sitemap or method:
            result["verkoopt_dolce_cilento"] = "nee"
            result["methode"] = method or "sitemap"
        else:
            result["methode"] = "geen sitemap/zoekfunctie herkend"
    except Exception as e:  # noqa: BLE001 - één kapotte site mag de run niet stoppen
        result["fout"] = f"{type(e).__name__}: {e}"[:200]
    return result


class BusyTracker:
    """Houdt in data/bezig.txt bij welke sites nu gecheckt worden. Wordt het proces van buitenaf gestopt
    (bijv. door macOS die een verdachte website blokkeert), dan worden die sites bij de volgende start overgeslagen."""

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = set()

    def _write(self):
        with open(BUSY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(self.busy)))
            f.flush()
            os.fsync(f.fileno())

    def start(self, site):
        with self.lock:
            self.busy.add(site)
            self._write()

    def done(self, site):
        with self.lock:
            self.busy.discard(site)
            self._write()


def skip_crashed(cache, cache_f):
    """Sites die bezig waren toen het proces de vorige keer werd gestopt: markeren als overgeslagen."""
    if not os.path.exists(BUSY_FILE):
        return
    with open(BUSY_FILE, encoding="utf-8") as f:
        crashed = [line.strip() for line in f if line.strip()]
    for site in crashed:
        if domain(site) in cache:
            continue
        r = {"naam": domain(site), "website": site, "verkoopt_dolce_cilento": "onbekend", "meloncello_gevonden": "",
             "emails": "", "gevonden_urls": "", "methode": "",
             "fout": "overgeslagen: proces werd gestopt tijdens het checken van deze site (mogelijk verdachte website)"}
        cache_f.write(json.dumps(r, ensure_ascii=False) + "\n")
        cache[domain(site)] = r
        print(f"Overgeslagen (proces stopte hierbij): {site}")
    cache_f.flush()
    os.remove(BUSY_FILE)


def load_cache():
    done = {}
    if os.path.exists(CACHE_JSONL):
        with open(CACHE_JSONL, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    done[domain(r["website"])] = r
                except (ValueError, KeyError):
                    pass
    return done


def drop_skipped():
    """Haal overgeslagen sites uit de cache zodat ze opnieuw gecheckt worden."""
    if not os.path.exists(CACHE_JSONL):
        return
    with open(CACHE_JSONL, encoding="utf-8") as f:
        lines = f.readlines()
    keep = [line for line in lines if '"overgeslagen:' not in line]
    with open(CACHE_JSONL, "w", encoding="utf-8") as f:
        f.writelines(keep)
    print(f"{len(lines) - len(keep)} overgeslagen sites worden één voor één opnieuw geprobeerd")


def check(extra_file=None, workers=8):
    with open(SHOPS_CSV, newline="", encoding="utf-8") as f:
        all_shops = list(csv.DictReader(f, delimiter=";"))
    shops = [r for r in all_shops if r["website"]]
    no_site = [dict({k: r.get(k, "") for k in ("naam", "plaats", "adres", "telefoon", "website")},
                    verkoopt_dolce_cilento="onbekend", emails=r.get("email", ""), fout="geen website bekend")
               for r in all_shops if not r["website"]]
    if extra_file:
        with open(extra_file, encoding="utf-8") as f:
            for line in f:
                site = normalize_site(line)
                if site:
                    shops.append({"naam": domain(site), "website": site, "bron": "extra"})

    unique = {}
    for s in shops:
        unique.setdefault(domain(s["website"]), s)
    cache = load_cache()
    with open(CACHE_JSONL, "a", encoding="utf-8") as cache_f:
        skip_crashed(cache, cache_f)
    # overgeslagen sites alsnog met naam/adres uit de winkellijst tonen
    for d, s in unique.items():
        if d in cache and cache[d].get("fout", "").startswith("overgeslagen"):
            cache[d] = dict(cache[d], **{k: s.get(k, "") for k in ("naam", "plaats", "adres", "telefoon")},
                            emails=cache[d]["emails"] or s.get("email", ""))
    todo = [s for d, s in unique.items() if d not in cache]
    print(f"{len(unique)} websites, {len(cache)} al gedaan, {len(todo)} te checken met {workers} threads...")

    lock = threading.Lock()
    tracker = BusyTracker()

    def run(shop):
        tracker.start(shop["website"])
        try:
            return check_shop(shop)
        finally:
            tracker.done(shop["website"])

    with open(CACHE_JSONL, "a", encoding="utf-8") as cache_f, ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, s): s for s in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            with lock:
                cache_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                cache_f.flush()
                cache[domain(r["website"])] = r
            flag = "  <-- VERKOOPT DOLCE CILENTO" if r["verkoopt_dolce_cilento"] == "ja" else ""
            print(f"[{i}/{len(todo)}] {r['naam'] or r['website']}: {r['verkoopt_dolce_cilento']}{flag}")
            time.sleep(0.05)

    order = {"ja": 0, "onbekend": 1, "nee": 2}
    rows = sorted((cache[d] for d in unique if d in cache),
                  key=lambda r: (order.get(r["verkoopt_dolce_cilento"], 3), r["meloncello_gevonden"] != "ja", r["naam"]))
    rows += sorted(no_site, key=lambda r: (r["plaats"], r["naam"]))
    with open(RESULT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_FIELDS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    hits = sum(r["verkoopt_dolce_cilento"] == "ja" for r in rows)
    print(f"\nKlaar: {hits} winkels verkopen Dolce Cilento -> {RESULT_CSV} (bovenaan de lijst)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stap", choices=["discover", "check", "all"])
    p.add_argument("--extra", help="bestand met extra website-URL's, één per regel")
    p.add_argument("--import", dest="imports", action="append", default=[],
                   help="eigen CSV met winkels (bijv. KvK-export); mag vaker gebruikt worden")
    p.add_argument("--workers", type=int, default=8, help="aantal websites tegelijk (standaard 8)")
    p.add_argument("--opnieuw", action="store_true",
                   help="overgeslagen sites één voor één opnieuw proberen (stopt macOS het proces weer, "
                        "dan wordt alleen die ene site overgeslagen)")
    args = p.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    if args.stap in ("discover", "all"):
        discover(args.imports)
    if args.stap in ("check", "all"):
        if args.opnieuw:
            drop_skipped()
            args.workers = 1
        check(args.extra, args.workers)


if __name__ == "__main__":
    main()
