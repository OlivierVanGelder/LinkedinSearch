#!/usr/bin/env python3
"""
serp_linkedin_lists.py

Zoekt via DuckDuckGo en levert lijsten terug (top N) op basis van meerdere strategieën.
Robuuste aanpak:
- Probeert eerst https://html.duckduckgo.com/html/ (GET) met q=...
- Als dat niet bruikbaar is, fallback naar https://duckduckgo.com/html/ (GET)
- Als dat niet bruikbaar is, fallback naar https://lite.duckduckgo.com/lite/ (POST)
- Decode DDG redirect links (uddg)
- Filtert op LinkedIn /in en /company
- Dedupe op URL
- Debug gaat naar stderr, stdout blijft pure JSON (voor output.json en webhook)

Gebruik:
  python serp_linkedin_lists.py --company "Stichting Breda-Actief" --extra "Breda" --tags "tag1,tag2" --max 10 > output.json
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


def _sleep_jitter(base: float = 1.1, jitter: float = 0.9) -> None:
    time.sleep(base + random.random() * jitter)


def _extract_real_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""

    if "uddg=" in href:
        # Kan relatief zijn
        if href.startswith("/"):
            href_full = "https://duckduckgo.com" + href
        else:
            href_full = href

        parsed = urlparse(href_full)
        qs = parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return unquote(qs["uddg"][0])

    return href


def _parse_linkedin_results(html: str, source_query: str, max_results: int) -> List[SerpResult]:
    soup = BeautifulSoup(html or "", "html.parser")
    results: List[SerpResult] = []
    seen = set()

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        url = _extract_real_url(href)

        if not url:
            continue

        if "linkedin.com/in/" not in url and "linkedin.com/company/" not in url:
            continue

        url = url.split("#")[0].strip()
        if not url or url in seen:
            continue
        seen.add(url)

        title = _clean_text(a.get_text()) or url

        results.append(
            SerpResult(
                title=title,
                url=url,
                snippet="",
                source_query=source_query,
            )
        )

        if len(results) >= max_results:
            break

    return results


def _looks_like_results_page(html: str) -> bool:
    if not html:
        return False

    h = html.lower()

    # Als er LinkedIn in de HTML zit, is het meestal al goed genoeg
    if "linkedin.com/in/" in h or "linkedin.com/company/" in h:
        return True

    # Sommige DDG HTML varianten gebruiken 'result' classes/ids
    if "result__a" in h or "result-link" in h or "results" in h:
        return True

    return False


def ddg_search(query: str, max_results: int = 10, timeout: int = 25, debug: bool = True) -> List[SerpResult]:
    """
    Probeert meerdere DDG endpoints in volgorde en pakt LinkedIn URLs uit de HTML.
    """

    session = requests.Session()

    headers_common = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    attempts: List[Dict[str, object]] = [
        {
            "name": "DDG_HTML_1",
            "method": "GET",
            "url": "https://html.duckduckgo.com/html/",
            "params": {"q": query},
            "data": None,
            "headers": {**headers_common, "Referer": "https://duckduckgo.com/"},
        },
        {
            "name": "DDG_HTML_2",
            "method": "GET",
            "url": "https://duckduckgo.com/html/",
            "params": {"q": query},
            "data": None,
            "headers": {**headers_common, "Referer": "https://duckduckgo.com/"},
        },
        {
            "name": "DDG_LITE",
            "method": "POST",
            "url": "https://lite.duckduckgo.com/lite/",
            "params": None,
            "data": {"q": query},
            "headers": {
                **headers_common,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://lite.duckduckgo.com",
                "Referer": "https://lite.duckduckgo.com/lite/",
            },
        },
    ]

    last_html = ""
    for i, att in enumerate(attempts):
        _sleep_jitter(1.0, 1.0)

        name = str(att["name"])
        method = str(att["method"])
        url = str(att["url"])
        params = att.get("params")
        data = att.get("data")
        headers = att.get("headers") or headers_common

        try:
            if method == "GET":
                resp = session.get(url, params=params, headers=headers, timeout=timeout)
            else:
                resp = session.post(url, data=data, headers=headers, timeout=timeout)

            last_html = resp.text or ""

            if debug:
                print(f"{name} status: {resp.status_code}", file=sys.stderr)
                print(f"{name} length: {len(last_html)}", file=sys.stderr)
                print(f"{name} head (400):", file=sys.stderr)
                print(last_html[:400], file=sys.stderr)

            # Alleen bij 200 proberen we te parsen
            if resp.status_code != 200:
                continue

            # Sommige responses zijn "landing pages" zonder resultaten
            if not _looks_like_results_page(last_html):
                continue

            parsed = _parse_linkedin_results(last_html, source_query=query, max_results=max_results)

            # Als we meteen LinkedIn urls hebben, klaar
            if parsed:
                return parsed

            # Als er geen LinkedIn urls zijn, kan het alsnog een results page zijn,
            # maar dan is je query te strikt. We proberen nog een volgende attempt.
            continue

        except Exception as e:
            if debug:
                print(f"{name} error for query: {query}", file=sys.stderr)
                print(f"{name} error: {str(e)}", file=sys.stderr)
            continue

    # Als alles faalt: geen resultaten
    return []


def merge_dedupe(results_lists: List[List[SerpResult]], max_results: int = 10) -> List[SerpResult]:
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
    core = re.sub(r"\s+", " ", core).strip()

    words = core.split() if core else company_no_dashes.split()
    short = " ".join(words[:4]).strip() if words else core

    name_variants: List[str] = []
    for n in [core, short, company_no_dashes, company]:
        n = (n or "").strip()
        if n and n not in name_variants:
            name_variants.append(n)

    role_block = "(marketing OR communicatie OR online OR digital OR digitaal OR website OR web OR content)"
    seniority_block = '(eigenaar OR directeur OR founder OR oprichter OR manager OR "head of" OR lead)'

    people_broad = [f'site:linkedin.com/in ("{n}"){extra_strict}' for n in name_variants]
    if core and extra_soft:
        people_broad.append(f'site:linkedin.com/in ("{core}"){extra_soft}')

    people_roles = [f'site:linkedin.com/in ("{n}") {role_block}{extra_strict}' for n in name_variants]
    if core and extra_soft:
        people_roles.append(f'site:linkedin.com/in ("{core}") {role_block}{extra_soft}')

    people_seniority: List[str] = []
    if core:
        people_seniority.append(f'site:linkedin.com/in ("{core}") {seniority_block}{extra_strict}')
        if extra_soft:
            people_seniority.append(f'site:linkedin.com/in ("{core}") {seniority_block}{extra_soft}')
    elif name_variants:
        people_seniority.append(f'site:linkedin.com/in ("{name_variants[0]}") {seniority_block}{extra_strict}')

    company_page = [f'site:linkedin.com/company ("{n}")' for n in name_variants]

    return {
        "strategy_people_broad": people_broad,
        "strategy_people_roles": people_roles,
        "strategy_people_seniority": people_seniority,
        "strategy_company_page": company_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Return SERP lists for LinkedIn discovery using DuckDuckGo.")
    parser.add_argument("--company", required=True, help="Bedrijfsnaam, bijvoorbeeld: Stichting Breda-Actief")
    parser.add_argument("--extra", default="", help="Extra context, bijvoorbeeld stad of domein")
    parser.add_argument("--tags", default="", help="Tags, gescheiden door komma's")
    parser.add_argument("--max", type=int, default=10, help="Max resultaten per strategie (default 10)")
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconden (default 25)")
    parser.add_argument("--debug", action="store_true", help="Zet debug expliciet aan (wordt standaard al gedaan)")
    args = parser.parse_args()

    raw_tags = args.tags or ""
    tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]

    # Debug altijd aan, naar stderr
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
