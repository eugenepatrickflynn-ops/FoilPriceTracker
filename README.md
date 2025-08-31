# Multi‑Shop Retail + Used Listings Tracker

This version tracks many retailers **and** scans used-market searches (eBay, Craigslist, etc.).
It emails you when:
- A retailer’s current price drops X% vs its baseline.
- A used listing is **≤ your `alert_below`** or **≥ `alert_percent_below_msrp`** drop relative to `msrp`.

## What changed
- **Resilient extraction**: JSON‑LD (schema.org Offer), Open Graph, then CSS selectors.
- **Used search engine** with per-site selectors and keyword filters.
- **Duplicate suppression** via `state.searches[*].seen`.

## Setup
1. Edit `config.yaml`:
   - Set `smtp` credentials.
   - Update/confirm each retailer `url`. Leave selectors as fallback; JSON‑LD often suffices.
   - For used searches, paste real search URLs (e.g., your local Craigslist region search).
   - Optionally set `msrp` for percent-based used triggers.
2. Install & run:
   ```bash
   pip install requests lxml pyyaml
   python price_tracker.py -c config.yaml
   ```
3. Schedule (cron, hourly recommended).

## Tips
- **Variant-specific prices**: for pages listing multiple sizes on one page, add a `price_regex`
  with **one capture group** for the numeric price you want.
- **Craigslist**: use your local subdomain search URL and adjust selectors if Craigslist updates UI.
- **More sites**: copy a `searches` block and tweak the four selectors to match the site’s cards.

Happy hunting!
