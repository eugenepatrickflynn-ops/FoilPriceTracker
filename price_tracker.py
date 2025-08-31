#!/usr/bin/env python3
"""
Price Tracking Agent — Retailers + Used Listings
------------------------------------------------
- Tracks exact product pages across many shops.
- Searches used marketplaces (e.g., eBay, Craigslist) and emails when finds are
  below an absolute ceiling (alert_below) or below a percent vs. MSRP/baseline.

Resilience improvements:
- Attempts JSON-LD (schema.org/Offer) and Open Graph price extraction
  before falling back to CSS selectors.
- Variant-aware regex option remains available (price_regex).

CONFIG OVERVIEW (config.yaml):
- default_drop_percent: 10
- msrp: 2747.00     # use for used-market percent comparisons (optional)
- smtp: {...}       # email credentials
- products:         # exact retailer pages (track percent drop vs. baseline per product)
    - { id, name, url, price_regex?, selector?, attr?, drop_percent?, baseline? }
- searches:         # used marketplace/search pages
    - {
        id, name, url,
        item_selector, title_selector, price_selector, url_selector,
        include_keywords: ["110L","6'7"], exclude_keywords: ["90L","105L"],
        alert_below: 2300,      # optional absolute ceiling
        alert_percent_below_msrp: 20, # or % below msrp (if msrp present)
        site: "ebay" | "craigslist" | "generic",  # optional hints
      }

STATE:
- prices_state.json stores baselines, history, and "seen" listing URLs for searches to avoid repeats.
"""
import os, re, json, time, ssl, smtplib, logging, math
from typing import Optional, Dict, Any, List, Tuple
from email.mime.text import MIMEText

import requests, yaml
from lxml import html, etree

STATE_FILE = os.environ.get("PT_STATE_FILE", "prices_state.json")

# ----------------- Utilities -----------------
def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def extract_price_number(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")
    if last_comma > last_dot:
        main = cleaned.replace(".", "").replace(",", ".")
    else:
        main = cleaned.replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", main)
    return float(m.group(0)) if m else None

def pct_drop(old: float, new: float) -> float:
    if old <= 0:
        return 0.0
    return ((old - new) / old) * 100.0

def fetch_text(session: requests.Session, url: str, timeout: int = 25, headers: Dict[str, str] = None) -> str:
    resp = session.get(url, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.text

def fetch_doc(session: requests.Session, url: str, timeout: int = 25, headers: Dict[str, str] = None) -> html.HtmlElement:
    resp = session.get(url, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return html.fromstring(resp.content)

def jsonld_prices(text: str) -> List[float]:
    """Parse all application/ld+json blobs for Offer/lowPrice/price."""
    prices = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, flags=re.S|re.I):
        blob = m.group(1)
        # naive extraction of "price" / "lowPrice" values
        for k in ("price", "lowPrice", "highPrice"):
            for pm in re.finditer(rf'"{k}"\s*:\s*"?([0-9][0-9\.,]*)"?', blob):
                p = extract_price_number(pm.group(1))
                if p is not None:
                    prices.append(p)
    return prices

def og_price(text: str) -> Optional[float]:
    m = re.search(r'property=["\']product:price:amount["\'][^>]*content=["\']([^"\']+)["\']', text, flags=re.I)
    if not m:
        m = re.search(r'name=["\']twitter:data1["\'][^>]*content=["\']([^"\']+)["\']', text, flags=re.I)  # rare
    return extract_price_number(m.group(1)) if m else None

# ----------------- Retailer price fetch -----------------
def fetch_product_price(session: requests.Session, prod: Dict[str, Any]) -> Optional[float]:
    url = prod["url"]
    headers = {"User-Agent": prod.get("user_agent", "Mozilla/5.0")}
    timeout = int(prod.get("timeout", 25))

    # 1) Variant regex if provided
    if prod.get("price_regex"):
        text = fetch_text(session, url, timeout=timeout, headers=headers)
        m = re.search(prod["price_regex"], text, flags=re.I)
        if m:
            return extract_price_number(m.group(1))

    # 2) JSON-LD / OG
    text = fetch_text(session, url, timeout=timeout, headers=headers)
    jl = jsonld_prices(text)
    if jl:
        # choose the minimum positive price to bias variant/lowPrice
        return min([p for p in jl if p > 0]) if any(p > 0 for p in jl) else None
    og = og_price(text)
    if og:
        return og

    # 3) CSS selector fallback
    if "selector" in prod and prod["selector"]:
        doc = html.fromstring(text.encode("utf-8"))
        nodes = doc.cssselect(prod["selector"])
        if nodes:
            node = nodes[0]
            raw = (node.get(prod.get("attr")) if prod.get("attr") else node.text_content()) or ""
            return extract_price_number(raw)

    return None

# ----------------- Used search scraping -----------------
def text_ok(s: str, includes: List[str], excludes: List[str]) -> bool:
    S = s.lower()
    if includes and not all(k.lower() in S for k in includes):
        return False
    if excludes and any(k.lower() in S for k in excludes):
        return False
    return True

def extract_items(doc: html.HtmlElement, cfg: Dict[str, Any]) -> List[Tuple[str,str,Optional[float]]]:
    """Return list of (title, url, price)."""
    items = []
    for card in doc.cssselect(cfg["item_selector"]):
        title = ""
        url = ""
        price = None
        # title
        try:
            if cfg.get("title_selector"):
                tnode = card.cssselect(cfg["title_selector"])
                if tnode:
                    title = tnode[0].text_content().strip()
        except Exception:
            pass
        # url
        try:
            if cfg.get("url_selector"):
                unode = card.cssselect(cfg["url_selector"])
                if unode:
                    href = unode[0].get("href", "").strip()
                    # make absolute if needed
                    if href and href.startswith("//"):
                        href = "https:" + href
                    elif href and href.startswith("/") and cfg["url"].startswith("http"):
                        from urllib.parse import urljoin
                        href = urljoin(cfg["url"], href)
                    url = href
        except Exception:
            pass
        # price
        try:
            if cfg.get("price_selector"):
                pnode = card.cssselect(cfg["price_selector"])
                if pnode:
                    price = extract_price_number(pnode[0].text_content())
        except Exception:
            pass

        if title or url:
            items.append((title, url, price))
    return items

def search_used_market(session: requests.Session, scfg: Dict[str, Any], seen: set, msrp: Optional[float]) -> List[Dict[str, Any]]:
    headers = {"User-Agent": scfg.get("user_agent", "Mozilla/5.0")}
    doc = fetch_doc(session, scfg["url"], headers=headers, timeout=int(scfg.get("timeout", 25)))
    candidates = extract_items(doc, scfg)
    results = []

    inc = scfg.get("include_keywords", [])
    exc = scfg.get("exclude_keywords", [])
    alert_below = scfg.get("alert_below")
    alert_pct = scfg.get("alert_percent_below_msrp")

    for title, url, price in candidates:
        if not text_ok(title, inc, exc):
            continue
        if url and url in seen:
            continue

        trigger = False
        reason = ""
        if alert_below and price is not None and price <= float(alert_below):
            trigger = True
            reason = f"<= ${float(alert_below):,.0f}"
        elif msrp and alert_pct and price is not None:
            drop = pct_drop(float(msrp), price)
            if drop >= float(alert_pct):
                trigger = True
                reason = f"{drop:.1f}% below MSRP"

        if trigger:
            results.append({"title": title, "url": url, "price": price, "reason": reason})

    return results

# ----------------- Email -----------------
def send_email(cfg: Dict[str, Any], subject: str, body: str) -> None:
    smtp = cfg["smtp"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp["from"]
    msg["To"] = ", ".join(smtp["to"])

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp["host"], smtp.get("port", 465), context=context) as server:
        server.login(smtp["username"], smtp["password"])
        server.sendmail(smtp["from"], smtp["to"], msg.as_string())

# ----------------- Main -----------------
def main(config_path: str = "config.yaml") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    cfg = load_yaml(config_path)
    state = load_state(STATE_FILE)
    session = requests.Session()
    if "http" in cfg:
        session.headers.update(cfg["http"].get("headers", {}))

    smtp_ok = "smtp" in cfg and all(k in cfg["smtp"] for k in ("host", "username", "password", "from", "to"))
    if not smtp_ok:
        logging.warning("SMTP not fully configured; alerts will be logged but not emailed.")

    # ----- Retailers -----
    for prod in cfg.get("products", []):
        pid = prod.get("id") or prod["url"]
        st = state.setdefault("products", {}).setdefault(pid, {})
        baseline = st.get("baseline")

        try:
            price = fetch_product_price(session, prod)
        except Exception as e:
            logging.error("Failed to fetch %s: %s", pid, e)
            continue

        if price is None:
            logging.error("Could not parse price for %s.", pid)
            continue

        now = int(time.time())
        # Initialize baseline
        if "baseline" in prod and isinstance(prod["baseline"], (int, float)):
            baseline = float(prod["baseline"])
        if baseline is None:
            baseline = price
            logging.info("Initialized baseline for %s at %.2f", pid, baseline)

        drop_needed = float(prod.get("drop_percent", cfg.get("default_drop_percent", 10)))
        drop_now = pct_drop(baseline, price)

        logging.info("Retailer: %s | current=%.2f | baseline=%.2f | drop=%.2f%% (needs %.2f%%)",
                     prod.get("name", pid), price, baseline, drop_now, drop_needed)

        hist = st.get("history", [])
        hist.append({"t": now, "price": price})
        st.update({
            "history": hist[-300:],
            "last_price": price,
            "baseline": baseline,
            "url": prod["url"],
            "name": prod.get("name", pid),
        })

        if drop_now >= drop_needed:
            subject = f"[Retail Price Drop] {prod.get('name', pid)} → {price:.2f} ({drop_now:.1f}% down)"
            body = (
                f"{prod.get('name', pid)}\n{prod['url']}\n\n"
                f"Current: ${price:,.2f}\nBaseline: ${baseline:,.2f}\nDrop: {drop_now:.2f}% (threshold {drop_needed:.2f}%)\n"
            )
            if smtp_ok:
                try:
                    send_email(cfg, subject, body)
                    logging.info("Email sent for %s", pid)
                except Exception as e:
                    logging.error("Failed to send email: %s", e)

    # ----- Used Searches -----
    msrp = float(cfg.get("msrp", 0) or 0)
    for scfg in cfg.get("searches", []):
        sid = scfg.get("id") or scfg["url"]
        sstate = state.setdefault("searches", {}).setdefault(sid, {})
        seen = set(sstate.get("seen", []))

        try:
            matches = search_used_market(session, scfg, seen, msrp if msrp > 0 else None)
        except Exception as e:
            logging.error("Failed used-search %s: %s", sid, e)
            continue

        if matches:
            lines = []
            for m in matches:
                price_part = f" — ${m['price']:,.0f}" if m.get("price") is not None else ""
                lines.append(f"- {m['title']}{price_part}\n  {m['url']}\n  Trigger: {m['reason']}")
                if m.get("url"):
                    seen.add(m["url"])
            subject = f"[Used Finds] {scfg.get('name', sid)} — {len(matches)} new match(es)"
            body = f"{scfg.get('name', sid)}\n{scfg['url']}\n\n" + "\n\n".join(lines)
            if smtp_ok:
                try:
                    send_email(cfg, subject, body)
                except Exception as e:
                    logging.error("Failed to send used-search email: %s", e)
        # persist seen set
        sstate["seen"] = sorted(list(seen))[-2000:]

    save_state(STATE_FILE, state)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Track retailer prices and search used listings; email on triggers.")
    ap.add_argument("-c", "--config", default="config.yaml", help="Path to YAML config.")
    args = ap.parse_args()
    main(args.config)
