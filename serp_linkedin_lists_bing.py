#!/usr/bin/env python3
"""
serp_linkedin_lists.py

Zoekt direct in LinkedIn Search en levert lijsten terug (top N) per strategie.

Waarom deze aanpak:
- DuckDuckGo en andere zoekmachines blokkeren GitHub Actions vaak (status 202, lege SERP).
- LinkedIn Search endpoints geven (soms beperkt) publiek HTML terug waarin profiel- en company-links staan.
- We parseren alleen LinkedIn links (/in/ en /company/) en dedupliceren.

Let op:
- LinkedIn kan throttlen of blokkeren (status 999, 429). We gebruiken jitter en beperkte requests.
- Resultaten kunnen wisselen of beperkt zijn zonder ingelogde sessie.

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
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote, urlencode

import requests
from bs4 import BeautifulSoup


@dataclass
class SerpResult:
    title: str
    url: str
    snippet: str
    source_query: str


def _sleep_jitter(base: float = 2.0, jitter: float = 2.0) -> None:
    # LinkedIn is snel gevoelig voor bursts. Jitter helpt, zeker in Actions.
    time.sleep(max(0.0, base + random.random() * jitter))


def _clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _to_abs_linkedin_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.linkedin.com" + href
    return href


def _unwrap_linkedin_authwall(url: str) -> str:
    # LinkedIn gebruikt vaak /authwall?sessionRedirect=<encoded_url>
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if "linkedin.com" not in (parsed.netloc or ""):
            return url
        if "/authwall" not in (parsed.path or ""):
            return url

        qs = parse_qs(parsed.query or "")
        redirect = ""
        if "sessionRedirect" in qs and qs["sessionRedirect"]:
            redirect = qs["sessionRedirect"][0]
        elif "trk" in qs and qs["trk"]:
            # soms staat redirect niet in sessionRedirect; laat dan de url zoals hij is
            redirect = ""

        if redirect:
            redirect = unquote(redirect)
            # redirect kan relatief zijn
            redirect = _to_abs_linkedin_url(redirect)
            return redirect
        return url
    except Exception:
        return url


def _extract_real_url(href: str) -> str:
    url = _to_abs_linkedin_url(href)
    url = _unwrap_linkedin_authwall(url)
    url = (url or "").split("#")[0].strip()
    return url


def _parse_linkedin_links(html: str, source_query: str, max_results: int, want: str) -> List[SerpResult]:
    soup = BeautifulSoup(html or "", "html.parser")
    results: List[SerpResult] = []
    seen = set()

    # We nemen anchors, maar filteren strikt op LinkedIn paths
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        url = _extract_real_url(href)
        if not url:
            continue

        # Normaliseer tracking
        if "?" in url:
            url = url.split("?")[0]

        if want == "people":
            if "linkedin.com/in/" not in url:
                continue
        elif want == "companies":
            if "linkedin.com/company/" not in url:
                continue
        else:
            if "linkedin.com/in/" not in url and "linkedin.com/company/" not in url:
                continue

        if url in seen:
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


def _looks_like_bing_search(html: str) -> bool:
    if not html:
        return False
    h = html.lower()
    # Een echte search pagina heeft meestal 'search' of resultaten container.
    if "search" in h and "linkedin" in h and ("/in/" in h or "/company/" in h):
        return True
    # Ook een login wall kan nog links bevatten, dus check op /in/ of /company/
    if "/in/" in h or "/company/" in h:
        return True
    return False


def bing_search(
    keywords: str,
    kind: str,
    max_results: int = 10,
    timeout: int = 25,
    debug: bool = True,
) -> Tuple[List[SerpResult], Dict[str, object]]:
    """
    Zoekt via Bing HTML en filtert op LinkedIn links.
    kind: 'people' of 'companies'
    Geeft resultaten plus diagnostics terug.

    Dit is stabieler dan direct LinkedIn Search vanuit GitHub Actions,
    omdat LinkedIn agressief blokkeert datacenter IPs.
    """
    session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    site = "site:linkedin.com/in/" if kind == "people" else "site:linkedin.com/company/"
    q = f"{site} {keywords}".strip()

    url = "https://www.bing.com/search"
    params = {
        "q": q,
        "setlang": "nl-nl",
        "cc": "NL",
        "count": str(max(10, min(50, max_results * 2))),
    }

    diagnostics: Dict[str, object] = {
        "kind": kind,
        "keywords": keywords,
        "status": None,
        "blocked": False,
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

    for attempt in range(1, 4):
        _sleep_jitter(1.5, 2.5)
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=True)
            html = resp.text or ""
            diagnostics["status"] = resp.status_code

            if debug:
                name = f"BING_{kind.upper()}_{attempt}"
                print(f"{name} status: {resp.status_code}", file=sys.stderr)
                print(f"{name} length: {len(html)}", file=sys.stderr)
                print(f"{name} head (400):", file=sys.stderr)
                print(html[:400], file=sys.stderr)

            if resp.status_code in (429, 403):
                diagnostics["blocked"] = True
                continue

            if resp.status_code != 200:
                continue

            low = html.lower()
            if "unusual traffic" in low or "captcha" in low or "verify you are a human" in low:
                diagnostics["blocked"] = True
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
                pattern = r"https?://(?:[a-z0-9\-]+\.)?linkedin\.com/(?:in|company)/[^\s\"\'<>\)]+"
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

            return results, diagnostics

        except requests.RequestException:
            diagnostics["blocked"] = True
            diagnostics["status"] = None
            continue

    return [], diagnostics


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

    # Normaliseer bedrijfsnaam (verwijder veelvoorkomende suffixen)
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

    # Strategie 1: breed op bedrijfsnaam
    people_broad = [f"{n}{extra_part}".strip() for n in base_names]

    # Strategie 2: rollen rond marketing/communicatie
    role_terms = ["marketing", "communicatie", "online marketing", "digital marketing", "content"]
    people_roles = []
    for n in base_names[:2]:
        for rt in role_terms[:3]:
            people_roles.append(f"{n} {rt}{extra_part}".strip())

    # Strategie 3: senioriteit
    seniority_terms = ["directeur", "eigenaar", "founder", "oprichter", "manager"]
    people_seniority = []
    for n in base_names[:2]:
        for st in seniority_terms[:3]:
            people_seniority.append(f"{n} {st}{extra_part}".strip())

    # Strategie 4: bedrijfspagina
    company_page = [f"{n}".strip() for n in base_names]

    # Dedup per lijst
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Return LinkedIn search lists for LinkedIn discovery (zonder externe SERP API). ")
    parser.add_argument("--company", required=True, help="Bedrijfsnaam, bijvoorbeeld: Stichting Breda-Actief")
    parser.add_argument("--extra", default="", help="Extra context, bijvoorbeeld stad of domein")
    parser.add_argument("--tags", default="", help="Tags, gescheiden door komma's")
    parser.add_argument("--max", type=int, default=10, help="Max resultaten per strategie (default 10)")
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconden (default 25)")
    parser.add_argument("--debug", action="store_true", help="Zet debug expliciet aan (debug staat in Actions meestal aan)")
    args = parser.parse_args()

    raw_tags = args.tags or ""
    tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]

    # Debug altijd aan, naar stderr. Flag blijft bestaan voor lokale runs.
    debug = True

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

        for q in queries:
            res, diag = bing_search(q, kind=kind, max_results=args.max, timeout=args.timeout, debug=debug)
            all_lists.append(res)
            diag_list.append(diag)

        merged = merge_dedupe(all_lists, max_results=args.max)
        output["results"][strategy] = [asdict(x) for x in merged]
        output["diagnostics"][strategy] = diag_list

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
