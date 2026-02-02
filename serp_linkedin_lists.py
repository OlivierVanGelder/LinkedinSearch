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


def build_queries(company: str, extra: Optional[str] = None) -> Dict[str, str]:
    """
    Three strategies:
    1) Company page
    2) People search with role keywords
    3) Executive or leadership fallback
    """
    extra_part = f" {extra}" if extra else ""

    q1 = f'site:linkedin.com/company "{company}"{extra_part}'
    q2 = (
        f'site:linkedin.com/in "{company}" (communicatie OR marketing OR online OR digital OR digitaal){extra_part}'
    )
    q3 = f'site:linkedin.com/in "{company}" (directeur OR manager OR bestuurder OR "raad van toezicht"){extra_part}'

    return {
        "strategy_company_page": q1,
        "strategy_people_roles": q2,
        "strategy_leadership_fallback": q3,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Return 3 SERP lists for LinkedIn discovery using DuckDuckGo HTML.")
    parser.add_argument("--company", required=True, help="Company name, for example: Stichting Breda Actief")
    parser.add_argument("--extra", default=None, help="Optional extra context, for example: Breda or domain")
    parser.add_argument("--max", type=int, default=10, help="Max results per strategy (default 10)")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds (default 20)")
    args = parser.parse_args()

    queries = build_queries(args.company, args.extra)

    output: Dict[str, object] = {
        "company": args.company,
        "extra": args.extra,
        "queries": queries,
        "results": {},
    }

    for key, q in queries.items():
        try:
            res = ddg_search(q, max_results=args.max, timeout=args.timeout)
            output["results"][key] = [asdict(x) for x in res]
        except Exception as e:
            output["results"][key] = {
                "error": str(e),
                "items": [],
            }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
