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
import sys
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

def ddg_search(query: str, max_results: int = 10, timeout: int = 25, debug: bool = True) -> List[SerpResult]:
    """
    DuckDuckGo search met fallback:
    - eerst html.duckduckgo.com (GET)
    - parse alle links
    - filter op LinkedIn profielen en company pages
    """

    session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
        "Referer": "https://duckduckgo.com/",
    }

    time.sleep(1.2 + random.random())

    resp = session.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()

    if debug:
        print("DDG status:", resp.status_code, file=sys.stderr)
        print("DDG length:", len(resp.text), file=sys.stderr)
        print("DDG head (400):", file=sys.stderr)
        print(resp.text[:400], file=sys.stderr)

    soup = BeautifulSoup(resp.text, "html.parser")

    results: List[SerpResult] = []
    seen = set()

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if "uddg=" in href:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            href = unquote(qs.get("uddg", [""])[0])

        if not href:
            continue

        if "linkedin.com/in/" not in href and "linkedin.com/company/" not in href:
            continue

        href = href.split("#")[0]
        if href in seen:
            continue
        seen.add(href)

        title = _clean_text(a.get_text()) or href

        results.append(
            SerpResult(
                title=title,
                url=href,
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
    company = (company or "").strip()
    extra = (extra or "").strip()

    extra_norm = extra.title() if extra else ""
    extra_strict = f" {extra_norm}" if extra_norm else ""
    extra_soft = f" ({extra_norm} OR Gelderland)" if extra_norm else ""

    company_no_dashes = re.sub(r"[-–—]+", " ", company).strip()
    company_no_dashes = re.sub(r"\s+", " ", company_no_dashes)

    core = re.sub(
        r"\b(bv|b\.v\.|nv|n\.v\.|holding|groep|stichting|vereniging)\b\.?",
        "",
        company_no_dashes,
        flags=re.IGNORECASE,
    ).strip()

    words = core.split() if core else company_no_dashes.split()
    short = " ".join(words[:4]).strip()

    name_variants = []
    for n in [core, short, company_no_dashes, company]:
        if n and n not in name_variants:
            name_variants.append(n)

    role_block = "(marketing OR communicatie OR online OR digital OR digitaal OR website OR web OR content)"
    seniority_block = '(eigenaar OR directeur OR founder OR oprichter OR manager OR "head of" OR lead)'

    people_broad = [
        f'site:linkedin.com/in ("{n}"){extra_strict}'
        for n in name_variants
    ]
    if core and extra_soft:
        people_broad.append(f'site:linkedin.com/in ("{core}"){extra_soft}')

    people_roles = [
        f'site:linkedin.com/in ("{n}") {role_block}{extra_strict}'
        for n in name_variants
    ]
    if core and extra_soft:
        people_roles.append(f'site:linkedin.com/in ("{core}") {role_block}{extra_soft}')

    people_seniority = []
    if core:
        people_seniority.append(f'site:linkedin.com/in ("{core}") {seniority_block}{extra_strict}')
        if extra_soft:
            people_seniority.append(f'site:linkedin.com/in ("{core}") {seniority_block}{extra_soft}')

    company_page = [
        f'site:linkedin.com/company ("{n}")'
        for n in name_variants
    ]

    return {
        "strategy_people_broad": people_broad,
        "strategy_people_roles": people_roles,
        "strategy_people_seniority": people_seniority,
        "strategy_company_page": company_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Return 3 SERP lists for LinkedIn discovery using DuckDuckGo Lite.")
    parser.add_argument("--company", required=True, help="Bedrijfsnaam, bijvoorbeeld: Stichting Breda-Actief")
    parser.add_argument("--extra", default="", help="Extra context, bijvoorbeeld stad of domein")
    parser.add_argument("--tags", default="", help="Tags, gescheiden door komma's")
    parser.add_argument("--max", type=int, default=10, help="Max resultaten per strategie (default 10)")
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconden (default 25)")
    parser.add_argument("--debug", action="store_true", help="Print debug output (status, length, head html)")
    args = parser.parse_args()

    raw_tags = args.tags or ""
    tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]


    # Debug is standaard aan zoals gevraagd, tenzij je het expliciet uitzet door --debug niet te gebruiken?
    # Jij wilde debug regels altijd, dus we zetten hem standaard True.
    debug = True

    query_sets = build_query_sets(args.company, args.extra)

    output: Dict[str, object] = {
        "company": args.company,
        "extra": args.extra,
        "tags": tags_list,
        "queries": query_sets,
        "results": {},
    }

    for strategy, queries in query_sets.items():
        all_lists: List[List[SerpResult]] = []

        for q in queries:
            try:
                res = ddg_search(q, max_results=args.max, timeout=args.timeout, debug=debug)
                all_lists.append(res)
            except Exception as e:
                if debug:
                    print("DDG error for query:", q, file=sys.stderr)
                    print("DDG error:", str(e), file=sys.stderr)
                all_lists.append([])

        merged = merge_dedupe(all_lists, max_results=args.max)
        output["results"][strategy] = [asdict(x) for x in merged]

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
