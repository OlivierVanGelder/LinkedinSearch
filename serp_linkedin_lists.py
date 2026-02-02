#!/usr/bin/env python3
import argparse
import json
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


@dataclass
class SerpResult:
    title: str
    url: str
    snippet: str


def _clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s


def ddg_search(query: str, max_results: int = 10, timeout: int = 20) -> List[SerpResult]:
    """
    Query DuckDuckGo HTML results page and parse organic results.
    Uses: https://html.duckduckgo.com/html/?q=...
    """
    base = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.6",
    }

    # Light throttling to reduce chance of being rate limited
    time.sleep(0.8 + random.random() * 0.6)

    resp = requests.post(
        base,
        data={"q": query},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results: List[SerpResult] = []

    # DuckDuckGo HTML layout commonly uses result containers with class "result"
    for r in soup.select(".result"):
        a = r.select_one("a.result__a")
        if not a:
            continue

        title = _clean_text(a.get_text())
        url = a.get("href") or ""
        url = url.strip()

        snippet_el = r.select_one(".result__snippet")
        snippet = _clean_text(snippet_el.get_text()) if snippet_el else ""

        if title and url:
            results.append(SerpResult(title=title, url=url, snippet=snippet))

        if len(results) >= max_results:
            break

    return results

def merge_dedupe(results_lists: list[list[SerpResult]], max_results: int = 10) -> list[SerpResult]:
    seen = set()
    merged: list[SerpResult] = []

    for lst in results_lists:
        for r in lst:
            u = (r.url or "").strip()
            if not u:
                continue
            # Dedup op URL
            if u in seen:
                continue
            seen.add(u)
            merged.append(r)
            if len(merged) >= max_results:
                return merged

    return merged


def build_query_sets(company: str, extra: str | None = None) -> dict[str, list[str]]:
    # extra is meestal stad, bv "Breda"
    extra = (extra or "").strip()
    extra_part = f" {extra}" if extra else ""

    # Maak ook een variant zonder streepjes in de naam, want die verschillen vaak
    company_clean = company.replace("-", " ").strip()

    # Zorg dat je ook zoekt op het domein als je die ooit meegeeft als extra
    # Als extra geen domein is, is dit leeg en doet het niets.
    domain_hint = extra if "." in extra else ""

    people_broad = [
        f'site:linkedin.com/in ("{company_clean}"){extra_part}',
        f'site:linkedin.com/in ("{company}"){extra_part}',
    ]

    # Als je "Breda Actief" als losse term wilt forceren, voeg je die ook toe
    # Handig bij stichtingen waar de officiële naam anders is.
    people_broad.append(f'site:linkedin.com/in ("Breda Actief"){extra_part}')

    if domain_hint:
        people_broad.append(f"site:linkedin.com/in ({domain_hint})")

    people_roles = [
        f'site:linkedin.com/in ("Breda Actief") (communicatie OR marketing OR online){extra_part}',
        f'site:linkedin.com/in ("Breda Actief") (website OR web OR digital OR digitaal){extra_part}',
        f'site:linkedin.com/in ("{company_clean}") (communicatie OR marketing OR online){extra_part}',
    ]

    company_page = [
        f'site:linkedin.com/company ("Breda Actief")',
        f'site:linkedin.com/company ("{company_clean}")',
        f'site:linkedin.com/company ("{company}")',
    ]

    return {
        "strategy_people_broad": people_broad,
        "strategy_people_roles": people_roles,
        "strategy_company_page": company_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Return 3 SERP lists for LinkedIn discovery using DuckDuckGo HTML.")
    parser.add_argument("--company", required=True, help="Company name, for example: Stichting Breda Actief")
    parser.add_argument("--extra", default=None, help="Optional extra context, for example: Breda or domain")
    parser.add_argument("--max", type=int, default=10, help="Max results per strategy (default 10)")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds (default 20)")
    args = parser.parse_args()

    query_sets = build_query_sets(args.company, args.extra)

    output: Dict[str, object] = {
        "company": args.company,
        "extra": args.extra,
        "queries": query_sets,
        "results": {},
    }

    for strategy, queries in query_sets.items():
        all_lists = []
        for q in queries:
            try:
                all_lists.append(ddg_search(q, max_results=args.max, timeout=args.timeout))
            except Exception:
                all_lists.append([])

        merged = merge_dedupe(all_lists, max_results=args.max)
        output["results"][strategy] = [asdict(x) for x in merged]


    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
