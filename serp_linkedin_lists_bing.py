#!/usr/bin/env python3
"""
serp_linkedin_lists.py

Zoekt via Bing HTML en filtert op LinkedIn links.
Doel: stabieler draaien in GitHub Actions dan direct LinkedIn Search.

Belangrijkste aanpassingen:
- Eén gedeelde requests Session voor alle queries
- Realistischere headers en cookies (taal, referer, consent hints)
- Langzamer tempo met jitter en backoff
- Strakkere detectie van "blocked" bij status 200 zonder echte resultaten
- Minder agressieve retry logica
"""

import sys
import argparse
import json
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup


@dataclass
class SerpResult:
    title: str
    url: str
    snippet: str
    source_query: str


def _sleep_jitter(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def merge_dedupe(results_lists: List[List[SerpResult]], max_results: int = 10) -> List[SerpResult]:
    seen = set()
    merged: List[SerpResult] = []
    for lst in results_lists:
        for r in lst:
            u = (r.url or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            merged.append(r)
            if len(merged) >= max_results:
                return merged
    return merged


def build_query_sets(company: str, extra: Optional[str] = None) -> Dict[str, List[str]]:
    company = (company or "").strip()
    extra = (extra or "").strip()

    company_no_punct = re.sub(r"[\t\n\r]+", " ", company).strip()
    company_no_punct = re.sub(r"\s+", " ", company_no_punct)

    core = re.sub(
        r"\b(bv|b\.v\.|nv|n\.v\.|holding|groep|stichting|vereniging)\b\.?",
        "",
        company_no_punct,
        flags=re.IGNORECASE,
    ).strip()
    core = re.sub(r"\s+", " ", core).strip()

    base_names = []
    for n in [core, company_no_punct, company]:
        n = (n or "").strip()
        if n and n not in base_names:
            base_names.append(n)

    extra_part = f" {extra}" if extra else ""

    people_broad = [f"{n}{extra_part}".strip() for n in base_names]

    role_terms = ["marketing", "communicatie", "online marketing", "digital marketing", "content"]
    people_roles = []
    for n in base_names[:2]:
        for rt in role_terms[:3]:
            people_roles.append(f"{n} {rt}{extra_part}".strip())

    seniority_terms = ["directeur", "eigenaar", "founder", "oprichter", "manager"]
    people_seniority = []
    for n in base_names[:2]:
        for st in seniority_terms[:3]:
            people_seniority.append(f"{n} {st}{extra_part}".strip())

    company_page = [f"{n}".strip() for n in base_names]

    def dedup_list(xs: List[str]) -> List[str]:
        out = []
        seen = set()
        for x in xs:
            x = (x or "").strip()
            if not x or x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    return {
        "strategy_people_broad": dedup_list(people_broad),
        "strategy_people_roles": dedup_list(people_roles),
        "strategy_people_seniority": dedup_list(people_seniority),
        "strategy_company_page": dedup_list(company_page),
    }


def _extract_linkedin_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    if "linkedin.com/" in raw:
        return raw.split("?")[0].split("#")[0]

    try:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query or "")
        for key in ["url", "u", "r", "RU", "target"]:
            if key in qs and qs[key]:
                candidate = unquote(qs[key][0])
                if "linkedin.com/" in candidate:
                    return candidate.split("?")[0].split("#")[0]
    except Exception:
        return ""

    return ""


def _bing_blocked_signals(html: str) -> bool:
    if not html:
        return True

    low = html.lower()

    hard_signals = [
        "unusual traffic",
        "captcha",
        "verify you are a human",
        "our systems have detected",
        "detected unusual activity",
        "sorry, but we need to make sure",
        "enter the characters you see",
    ]
    if any(s in low for s in hard_signals):
        return True

    soup = BeautifulSoup(html, "html.parser")

    has_results_container = soup.select_one("#b_results") is not None
    has_algo = soup.select_one("li.b_algo") is not None

    if not has_results_container:
        return True

    if not has_algo:
        text = _clean_text(soup.get_text(" ", strip=True))
        if len(text) < 2000:
            return True

    return False


def bing_search(
    session: requests.Session,
    keywords: str,
    kind: str,
    max_results: int = 10,
    timeout: int = 25,
    debug: bool = True,
) -> Tuple[List[SerpResult], Dict[str, object]]:
    site = "site:linkedin.com/in/" if kind == "people" else "site:linkedin.com/company/"
    q = f"{site} {keywords}".strip()

    url = "https://www.bing.com/search"
    params = {
        "q": q,
        "count": str(max(10, min(50, max_results * 2))),
        "setlang": "nl-nl",
        "cc": "NL",
    }

    diagnostics: Dict[str, object] = {
        "kind": kind,
        "keywords": keywords,
        "status": None,
        "blocked": False,
    }

    for attempt in range(1, 4):
        _sleep_jitter(6.0, 11.5)

        try:
            resp = session.get(url, params=params, timeout=timeout, allow_redirects=True)
            html = resp.text or ""
            diagnostics["status"] = resp.status_code

            if debug:
                name = f"BING_{kind.upper()}_{attempt}"
                print(f"{name} status: {resp.status_code}", file=sys.stderr)
                print(f"{name} length: {len(html)}", file=sys.stderr)
                print(f"{name} head (400):", file=sys.stderr)
                print(html[:400], file=sys.stderr)

            if resp.status_code in (403, 429):
                diagnostics["blocked"] = True
                _sleep_jitter(10.0, 18.0)
                continue

            if resp.status_code != 200:
                continue

            if _bing_blocked_signals(html):
                diagnostics["blocked"] = True
                _sleep_jitter(10.0, 18.0)
                continue

            results: List[SerpResult] = []
            seen = set()

            soup = BeautifulSoup(html, "html.parser")

            for a in soup.select("li.b_algo h2 a[href]"):
                href = a.get("href") or ""
                clean = _extract_linkedin_url(href)
                if not clean:
                    continue

                if kind == "people" and "linkedin.com/in/" not in clean:
                    continue
                if kind != "people" and "linkedin.com/company/" not in clean:
                    continue

                if clean in seen:
                    continue
                seen.add(clean)

                title = _clean_text(a.get_text(" ", strip=True))
                results.append(SerpResult(title=title or clean, url=clean, snippet="", source_query=keywords))
                if len(results) >= max_results:
                    break

            if not results:
                pattern = r"https?://(?:[a-z0-9\-]+\.)?linkedin\.com/(?:in|company)/[^\s\"\'<>\)]+"  # noqa: W605
                for raw in re.findall(pattern, html, flags=re.IGNORECASE):
                    clean = raw.split("?")[0].split("#")[0]
                    if kind == "people" and "linkedin.com/in/" not in clean:
                        continue
                    if kind != "people" and "linkedin.com/company/" not in clean:
                        continue
                    if clean in seen:
                        continue
                    seen.add(clean)
                    results.append(SerpResult(title=clean, url=clean, snippet="", source_query=keywords))
                    if len(results) >= max_results:
                        break

            if results:
                diagnostics["blocked"] = False
                return results, diagnostics

            diagnostics["blocked"] = True
            _sleep_jitter(8.0, 14.0)

        except requests.RequestException:
            diagnostics["blocked"] = True
            diagnostics["status"] = None
            _sleep_jitter(10.0, 18.0)
            continue

    return [], diagnostics


def make_session() -> requests.Session:
    s = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.bing.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    s.headers.update(headers)

    cookies = {
        "SRCHHPGUSR": "SRCHLANG=nl&ADLT=MODERATE",
    }
    s.cookies.update(cookies)

    return s


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Return LinkedIn link lists via Bing HTML (zonder externe SERP API)."
    )
    parser.add_argument("--company", required=True, help="Bedrijfsnaam")
    parser.add_argument("--extra", default="", help="Extra context, bijvoorbeeld stad")
    parser.add_argument("--tags", default="", help="Tags, gescheiden door komma's")
    parser.add_argument("--max", type=int, default=10, help="Max resultaten per strategie")
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconden")
    parser.add_argument("--debug", action="store_true", help="Zet debug aan")
    args = parser.parse_args()

    tags_list = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    debug = True

    session = make_session()
    query_sets = build_query_sets(args.company, args.extra)

    output: Dict[str, object] = {
        "company": args.company,
        "extra": args.extra,
        "tags": tags_list,
        "queries": query_sets,
        "results": {},
        "diagnostics": {},
    }

    for strategy, queries in query_sets.items():
        all_lists: List[List[SerpResult]] = []
        diag_list: List[Dict[str, object]] = []

        kind = "companies" if strategy == "strategy_company_page" else "people"

        blocked_streak = 0

        for q in queries:
            res, diag = bing_search(
                session=session,
                keywords=q,
                kind=kind,
                max_results=args.max,
                timeout=args.timeout,
                debug=debug,
            )

            all_lists.append(res)
            diag_list.append(diag)

            if diag.get("blocked"):
                blocked_streak += 1
            else:
                blocked_streak = 0

            if blocked_streak >= 2:
                _sleep_jitter(18.0, 28.0)
                break

        merged = merge_dedupe(all_lists, max_results=args.max)
        output["results"][strategy] = [asdict(x) for x in merged]
        output["diagnostics"][strategy] = diag_list

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
