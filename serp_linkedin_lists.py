#!/usr/bin/env python3
"""
serp_linkedin_lists.py

Zoekt via DuckDuckGo Lite (HTML) en levert 3 lijsten terug (top N) op basis van 3 strategieën.
Robuuste parsing: pakt alle links, decoded DDG redirect links (uddg), filtert daarna op LinkedIn.

Geschikt voor GitHub Actions. Debug staat aan en print altijd:
- status code
- response length
- eerste 800 tekens HTML

Gebruik:
  python serp_linkedin_lists.py --company "Stichting Breda-Actief" --extra "Breda" --max 10 > output.json
"""

import argparse
import json
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup


@dataclass
class SerpResult:
    title: str
    url: str
    snippet: str
    source_query: str


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def ddg_search_lite(query: str, max_results: int = 10, timeout: int = 25, debug: bool = True) -> List[SerpResult]:
    """
    Query DuckDuckGo Lite endpoint and parse LinkedIn URLs from the HTML.
    Uses POST https://lite.duckduckgo.com/lite/ with form data {"q": query}.
    """
    base = "https://lite.duckduckgo.com/lite/"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://lite.duckduckgo.com",
        "Referer": "https://lite.duckduckgo.com/lite/",
    }

    # mild rate limiting to reduce the chance of getting blocked
    time.sleep(1.2 + random.random() * 1.0)

    resp = requests.post(base, data={"q": query}, headers=headers, timeout=timeout)
    resp.raise_for_status()

    if debug:
        print("DDG status:", resp.status_code)
        print("DDG length:", len(resp.text or ""))
        print("DDG head (800):")
        print((resp.text or "")[:800])

    html = resp.text or ""
    soup = BeautifulSoup(html, "html.parser")

    results: List[SerpResult] = []
    seen = set()

    def extract_real_url(href: str) -> str:
        href = (href or "").strip()
        if not href:
            return ""

        # Vaak DDG redirect: /l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2F...
        if "uddg=" in href:
            # Kan relatief zijn
            if href.startswith("/"):
                href_full = "https://lite.duckduckgo.com" + href
            else:
                href_full = href

            parsed = urlparse(href_full)
            qs = parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                return unquote(qs["uddg"][0])

        return href

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        url = extract_real_url(href)

        if not url:
            continue

        # Filter: alleen LinkedIn profielen of bedrijfspagina's
        if "linkedin.com/in/" not in url and "linkedin.com/company/" not in url:
            continue

        # Normaliseer tracking fragmenten
        url = url.split("#")[0].strip()

        if url in seen:
            continue
        seen.add(url)

        title = _clean_text(a.get_text()) or url

        results.append(
            SerpResult(
                title=title,
                url=url,
                snippet="",
                source_query=query,
            )
        )

        if len(results) >= max_results:
            break

    return results


def merge_dedupe(results_lists: List[List[SerpResult]], max_results: int = 10) -> List[SerpResult]:
    """
    Merge multiple result lists, dedupe by URL, return first max_results.
    """
    seen = set()
    merged: List[SerpResult] = []

    for lst in results_lists:
        for r in lst:
            u = (r.url or "").strip()
            if not u:
                continue
            if u in seen:
                continue
            seen.add(u)
            merged.append(r)
            if len(merged) >= max_results:
                return merged

    return merged


def build_query_sets(company: str, extra: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Brede queries, niet te strikt.
    We filteren pas na ophalen op linkedin.com/in en linkedin.com/company.

    Strategieën:
    - strategy_people_broad: brede people search
    - strategy_people_roles: people met rolwoorden
    - strategy_company_page: bedrijfspagina
    """
    company = (company or "").strip()
    extra = (extra or "").strip()

    # Normaliseer extra: BREDA -> Breda
    extra_norm = extra.title() if extra else ""
    extra_part = f" {extra_norm}" if extra_norm else ""

    # Maak ook een variant zonder streepjes, want dat verschilt vaak
    company_clean = re.sub(r"[-–—]+", " ", company).strip()
    company_clean = re.sub(r"\s+", " ", company_clean)

    people_broad = [
        f'site:linkedin.com ("Breda Actief"){extra_part}',
        f'site:linkedin.com ("{company_clean}"){extra_part}',
        f'site:linkedin.com ("{company}"){extra_part}',
    ]

    people_roles = [
        f'site:linkedin.com ("Breda Actief") (communicatie OR marketing OR online OR digital OR digitaal){extra_part}',
        f'site:linkedin.com ("{company_clean}") (communicatie OR marketing OR online OR digital OR digitaal){extra_part}',
        f'site:linkedin.com ("Breda Actief") (website OR web OR content OR social){extra_part}',
    ]

    company_page = [
        'site:linkedin.com/company ("Breda Actief")',
        f'site:linkedin.com/company ("{company_clean}")',
        f'site:linkedin.com/company ("{company}")',
    ]

    return {
        "strategy_people_broad": people_broad,
        "strategy_people_roles": people_roles,
        "strategy_company_page": company_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Return 3 SERP lists for LinkedIn discovery using DuckDuckGo Lite.")
    parser.add_argument("--company", required=True, help="Bedrijfsnaam, bijvoorbeeld: Stichting Breda-Actief")
    parser.add_argument("--extra", default="", help="Extra context, bijvoorbeeld stad of domein")
    parser.add_argument("--max", type=int, default=10, help="Max resultaten per strategie (default 10)")
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconden (default 25)")
    parser.add_argument("--debug", action="store_true", help="Print debug output (status, length, head html)")
    args = parser.parse_args()

    # Debug is standaard aan zoals gevraagd, tenzij je het expliciet uitzet door --debug niet te gebruiken?
    # Jij wilde debug regels altijd, dus we zetten hem standaard True.
    debug = True

    query_sets = build_query_sets(args.company, args.extra)

    output: Dict[str, object] = {
        "company": args.company,
        "extra": args.extra,
        "queries": query_sets,
        "results": {},
    }

    for strategy, queries in query_sets.items():
        all_lists: List[List[SerpResult]] = []

        for q in queries:
            try:
                res = ddg_search_lite(q, max_results=args.max, timeout=args.timeout, debug=debug)
                all_lists.append(res)
            except Exception as e:
                if debug:
                    print("DDG error for query:", q)
                    print("DDG error:", str(e))
                all_lists.append([])

        merged = merge_dedupe(all_lists, max_results=args.max)
        output["results"][strategy] = [asdict(x) for x in merged]

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
