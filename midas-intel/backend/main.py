"""
MIDAS Pre Sales Intelligence — FastAPI Backend
Ported from Streamlit app. All crawling, AI analysis, enrichment, and storage logic.
"""

import os
import io
import re
import csv
import json
import time
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI
from supabase import create_client, Client
from bs4 import BeautifulSoup

# ── CONFIG ───────────────────────────────────────────────────────────────────

app = FastAPI(title="MIDAS Pre Sales Intel API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Lock down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Env vars (set via Railway/Render secrets or .env)
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
FIRECRAWL_KEY = os.environ["FIRECRAWL_KEY"]
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_KEY", "")
COMPANIES_HOUSE_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

# Shared HTTP session
http = requests.Session()
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
http.mount("http://", adapter)
http.mount("https://", adapter)
http.headers.update({"User-Agent": "Mozilla/5.0"})


def now_gmt2():
    return datetime.now(timezone.utc) + timedelta(hours=2)


# ── PYDANTIC MODELS ──────────────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    url: str

class BatchRequest(BaseModel):
    urls: list[str]
    recrawl: bool = False

class NoteUpdate(BaseModel):
    domain: str
    note: str

class EmailRequest(BaseModel):
    company_data: dict
    sales_data: dict


# ── STORAGE ──────────────────────────────────────────────────────────────────

def load_history():
    try:
        res = supabase.table("midas_history").select("*").order("date", desc=True).execute()
        return res.data or []
    except:
        return []

def save_history(entry):
    try:
        supabase.table("midas_history").upsert({
            "domain":       entry["domain"],
            "company":      entry["company"],
            "score":        entry["score"],
            "date":         entry["date"],
            "pages_count":  entry["pages_count"],
            "company_data": entry["company_data"],
            "sales_data":   entry["sales_data"],
        }, on_conflict="domain").execute()
    except Exception as e:
        print(f"Could not save history: {e}")

def find_in_history(domain):
    try:
        res = supabase.table("midas_history").select("*").eq("domain", domain).execute()
        return res.data[0] if res.data else None
    except:
        return None

def delete_from_history(domain):
    try:
        supabase.table("midas_history").delete().eq("domain", domain).execute()
    except Exception as e:
        print(f"Could not delete: {e}")

def get_note(domain):
    try:
        res = supabase.table("midas_notes").select("*").eq("domain", domain).execute()
        if res.data:
            r = res.data[0]
            return {"text": r["note_text"], "updated": r["updated"]}
    except:
        pass
    return {}

def save_note_db(domain, note):
    try:
        supabase.table("midas_notes").upsert({
            "domain":    domain,
            "note_text": note,
            "updated":   now_gmt2().strftime("%d %b %Y %H:%M")
        }, on_conflict="domain").execute()
    except Exception as e:
        print(f"Could not save note: {e}")


# ── HELPERS ──────────────────────────────────────────────────────────────────

def extract_domain(url):
    return urlparse(url).netloc.replace("www.", "")

def days_ago(date_str):
    try:
        dt = datetime.strptime(date_str, "%d %b %Y %H:%M")
        now = now_gmt2().replace(tzinfo=None)
        diff = (now - dt).days
        if diff <= 0:
            return "today"
        elif diff == 1:
            return "yesterday"
        else:
            return f"{diff} days ago"
    except:
        return "recently"

def safe_json(text):
    try:
        cleaned = re.sub(r"```json|```", "", text).strip()
        return json.loads(cleaned)
    except:
        try:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
        except:
            pass
        return {}

def extract_employee_count_from_text(text):
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text)
    patterns = [
        r"(\d[\d,]*(?:\+)?(?:\s*[-–]\s*\d[\d,]*(?:\+)?)?)\s+(?:employees|staff)",
        r"(?:company size|employees)[:\s]+(\d[\d,]*(?:\+)?(?:\s*[-–]\s*\d[\d,]*(?:\+)?)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            count = re.sub(r"\s*[-–]\s*", "-", match.group(1).strip())
            return f"{count} employees"
    return ""

def employee_count_floor(employee_count):
    nums = re.findall(r"[\d,]+", str(employee_count or "").replace(",", ""))
    return int(nums[0]) if nums else 0

def extract_locations_from_text(text):
    if not text:
        return []
    normalized = re.sub(r"\s+", " ", text)
    locations = []
    seen = set()
    location_stopwords = {
        "a", "an", "the", "this", "that", "these", "those", "and", "or", "to", "from", "in",
        "on", "at", "of", "for", "with", "including", "include", "set",
    }
    junk_location_terms = (
        r"set to", r"companies house", r"include", r"company", r"officer",
        r"appointed", r"registered", r"jurisdiction", r"courts?", r"certification",
        r"exclusive", r"terms", r"privacy", r"policy", r"cookies?", r"training",
    )

    def add_location(value):
        value = re.sub(r"\s+", " ", value or "").strip(" ,.-")
        if not value:
            return
        first_token = re.split(r"[\s,]+", value.lower(), maxsplit=1)[0]
        if first_token in location_stopwords:
            return
        if re.search(r"\b(" + "|".join(junk_location_terms) + r")\b", value, re.IGNORECASE):
            return
        if value.lower().startswith(("the ", "to ", "and ", "or ")):
            return
        first_part = value.split(",", 1)[0].strip()
        if len(first_part.split()) > 3:
            return
        key = value.lower()
        if key not in seen:
            seen.add(key)
            locations.append(value)

    uk_postcode = r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}"
    address_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    for idx, line in enumerate(address_lines):
        if not re.search(uk_postcode, line, re.IGNORECASE):
            continue
        window = " ".join(address_lines[max(0, idx - 3):idx + 1])
        line_match = re.search(
            rf"\b([A-Za-z' .-]{{2,35}})\s+([A-Za-z' .-]{{2,35}})\s+"
            rf"(?:United Kingdom|UK|England|Scotland|Wales)\s*{uk_postcode}",
            window,
            re.IGNORECASE,
        )
        if line_match:
            town = line_match.group(1).strip().title()
            county = line_match.group(2).strip().title()
            if not re.search(r"\b(road|street|lane|avenue|drive|close|way|place|park|court|yard)\b", town, re.IGNORECASE):
                add_location(f"{town}, {county}, United Kingdom")

    uk_street_address_pattern = re.compile(
        rf"\d{{1,5}}\s+[A-Za-z0-9' .-]+?\b(?:road|street|lane|avenue|drive|close|way|place|park|court|yard)\b\s+"
        rf"([A-Za-z' .-]{{2,35}}?)\s+"
        rf"([A-Za-z' .-]{{2,35}}?)\s+"
        rf"(?:United Kingdom|UK|England|Scotland|Wales)\s*{uk_postcode}",
        re.IGNORECASE,
    )
    for match in uk_street_address_pattern.finditer(normalized):
        town = (match.group(1) or "").strip().title()
        county = (match.group(2) or "").strip().title()
        add_location(f"{town}, {county}, United Kingdom")

    address_pattern = re.compile(
        rf"(\d{{1,5}}\s+[A-Za-z0-9' .-]+?\s+"
        rf"([A-Z][A-Za-z' .-]{{2,40}})\s+"
        rf"([A-Z][A-Za-z' .-]{{2,40}})?\s*"
        rf"(?:United Kingdom|UK|England|Scotland|Wales)?\s*{uk_postcode})",
        re.IGNORECASE,
    )
    for match in address_pattern.finditer(normalized):
        town = (match.group(2) or "").strip()
        county = (match.group(3) or "").strip()
        if re.search(r"\b(road|street|lane|avenue|drive|close|way|place|park|court|yard)\b", town, re.IGNORECASE):
            continue
        if county and not re.search(r"united kingdom|england|scotland|wales|uk", county, re.IGNORECASE):
            add_location(f"{town}, {county.title()}, United Kingdom")
        else:
            add_location(f"{town}, United Kingdom")

    return locations[:5]

def direct_homepage_text(url):
    try:
        resp = http.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-GB,en;q=0.9"}, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except:
        return ""

def clean_locations(locations):
    cleaned = []
    seen = set()
    location_stopwords = {"a", "an", "the", "this", "that", "these", "those", "and", "or", "to", "from", "in", "on", "at", "of", "for", "with"}
    for loc in locations or []:
        value = re.sub(r"\s+", " ", str(loc or "")).strip(" ,.-")
        if not value:
            continue
        first_token = re.split(r"[\s,]+", value.lower(), maxsplit=1)[0]
        if first_token in location_stopwords:
            continue
        if re.search(r"\b(set to|companies house|include|company|officer|appointed|registered|jurisdiction|courts?|certification|exclusive|terms|privacy|policy|cookies?|training)\b", value, re.IGNORECASE):
            continue
        if value.lower().startswith(("the ", "to ", "and ", "or ")):
            continue
        if len(value) > 80:
            continue
        first_part = value.split(",", 1)[0].strip()
        if len(first_part.split()) > 3:
            continue
        key = value.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(value)
    return cleaned[:5]


# ── CRAWLING ─────────────────────────────────────────────────────────────────

def firecrawl_scrape_single(url, firecrawl_key):
    try:
        resp = http.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {firecrawl_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"]}, timeout=20
        )
        md = resp.json().get("data", {}).get("markdown", "")
        return [{"url": url, "markdown": md}] if md else []
    except:
        return []


def firecrawl_multi_scrape(base_url, firecrawl_key):
    results = []
    visited = set()

    def scrape_one(url):
        if url in visited:
            return None
        visited.add(url)
        try:
            resp = http.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {firecrawl_key}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["markdown"]}, timeout=20
            )
            md = resp.json().get("data", {}).get("markdown", "")
            if md.strip():
                return {"url": url, "markdown": md}
        except:
            pass
        return None

    home = scrape_one(base_url)
    if not home:
        return []
    results.append(home)

    try:
        html_resp = http.get(base_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(html_resp.text, "html.parser")
        domain = urlparse(base_url).netloc
        priority_keywords = ["people","team","our-team","staff","leadership","directors","who-we-are","about","careers","jobs","vacancies","join","projects","services","what-we-do","contact"]
        all_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            if (parsed.netloc == domain and
                not any(full.endswith(ext) for ext in [".pdf",".jpg",".png",".zip"]) and
                "#" not in full and full != base_url and full not in visited):
                all_links.append(full)

        def priority_score(link):
            lower = link.lower()
            for i, kw in enumerate(priority_keywords):
                if kw in lower:
                    return i
            return 999

        sorted_links = sorted(set(all_links), key=priority_score)
    except:
        sorted_links = []

    for link in sorted_links[:14]:
        page = scrape_one(link)
        if page:
            results.append(page)
    return results


def direct_fetch(url, max_subpages=14):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": "cookielawinfo-checkbox-necessary=yes; cookielawinfo-checkbox-analytics=yes; viewed_cookie_policy=yes; cookie_consent=accepted; gdpr=1; euconsent=1"
        }
        resp = http.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)

        if len(text) > 500:
            results = [{"url": url, "markdown": text}]
        else:
            results = []

        domain = urlparse(url).netloc
        visited = {url}
        priority_keywords = ["people","team","our-team","staff","leadership","directors","who-we-are",
                             "about","careers","jobs","vacancies","join","projects","services",
                             "what-we-do","contact","expertise","our-expertise","sectors","capabilities"]
        all_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full = urljoin(url, href)
            parsed = urlparse(full)
            if (parsed.netloc == domain and "#" not in full and full not in visited and
                not any(full.endswith(ext) for ext in [".pdf",".jpg",".png",".zip"])):
                all_links.append(full)
                visited.add(full)

        def priority_score(link):
            lower = link.lower()
            for i, kw in enumerate(priority_keywords):
                if kw in lower:
                    return i
            return 999

        sorted_links = sorted(set(all_links), key=priority_score)

        def fetch_subpage(link):
            try:
                sub = http.get(link, headers=headers, timeout=10)
                sub_soup = BeautifulSoup(sub.text, "html.parser")
                for tag in sub_soup(["script", "style", "noscript", "iframe"]):
                    tag.decompose()
                sub_text = sub_soup.get_text(separator="\n", strip=True)
                if len(sub_text) > 200:
                    return {"url": link, "markdown": sub_text}
            except:
                return None
            return None

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(fetch_subpage, link) for link in sorted_links[:max_subpages]]
            for future in as_completed(futures):
                page = future.result()
                if page:
                    results.append(page)
        return results
    except:
        return []


def serpapi_search(query, num_results=10):
    if not SERPER_API_KEY:
        return []
    try:
        resp = http.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": SERPER_API_KEY, "num": num_results},
            timeout=20
        )
        return resp.json().get("organic_results", []) or []
    except:
        return []


def format_serpapi_results(results, max_chars=4000):
    lines = []
    for item in results:
        parts = [p for p in [item.get("title",""), item.get("snippet",""), item.get("link","")] if p]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)[:max_chars]


def fetch_serpapi_site_results(url):
    results = []
    seen = set()
    parsed = urlparse(url)
    domain = parsed.netloc
    base = f"{parsed.scheme}://{parsed.netloc}"

    queries = [
        f"site:{domain} team OR people OR staff OR leadership",
        f"site:{domain} engineers OR directors OR principal OR associate OR consultant",
    ]

    for query in queries:
        for item in serpapi_search(query, num_results=8):
            link = item.get("link", "")
            if not link or domain not in link or link in seen:
                continue
            text = " | ".join([p for p in [item.get("title",""), item.get("snippet",""), link] if p])
            if len(text) > 80:
                results.append({"url": link, "markdown": text})
                seen.add(link)
    return results


def scrape_with_scrapingbee(url):
    if not SCRAPINGBEE_KEY:
        return []
    try:
        headers_sb = {"api_key": SCRAPINGBEE_KEY, "render_js": "true", "wait": "2000"}
        results = []
        visited = set()

        def sb_scrape(target_url):
            if target_url in visited:
                return None, None
            visited.add(target_url)
            try:
                resp = http.get(
                    "https://app.scrapingbee.com/api/v1/",
                    params={**headers_sb, "url": target_url}, timeout=30
                )
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                if len(text) > 500:
                    return {"url": target_url, "markdown": text}, soup
            except:
                pass
            return None, None

        home_result, home_soup = sb_scrape(url)
        if not home_result:
            return []
        results.append(home_result)

        if home_soup:
            domain = urlparse(url).netloc
            priority_keywords = ["team","people","about","projects","services","careers","who-we-are","our-work"]
            links = []
            for a in home_soup.find_all("a", href=True):
                href = a["href"].strip()
                full = urljoin(url, href)
                parsed_link = urlparse(full)
                if (parsed_link.netloc == domain and "#" not in full and full != url and
                    full not in visited and
                    not any(full.endswith(ext) for ext in [".pdf",".jpg",".png",".zip"])):
                    links.append(full)

            def priority_score(link):
                lower = link.lower()
                for i, kw in enumerate(priority_keywords):
                    if kw in lower:
                        return i
                return 999

            sorted_links = sorted(set(links), key=priority_score)
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(sb_scrape, link) for link in sorted_links[:5]]
                for future in as_completed(futures):
                    result, _ = future.result()
                    if result:
                        results.append(result)
        return results
    except:
        return []


def firecrawl_crawl(url, firecrawl_key, max_pages=30, status_callback=None):
    try:
        # Try scraping with actions for cookie popups
        if status_callback:
            status_callback("crawling", f"Scraping homepage...", 6)
        action_resp = http.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {firecrawl_key}", "Content-Type": "application/json"},
            json={
                "url": url, "formats": ["markdown"],
                "actions": [
                    {"type": "wait", "milliseconds": 2000},
                    {"type": "click", "selector": "button[class*='accept'], button[id*='accept'], button[class*='agree'], .cookie-accept, #cookie-accept"},
                    {"type": "wait", "milliseconds": 1000}
                ]
            }, timeout=30
        )
        homepage_md = action_resp.json().get("data", {}).get("markdown", "")

        if homepage_md and len(homepage_md) > 500:
            results = [{"url": url, "markdown": homepage_md}]
            try:
                if status_callback:
                    status_callback("crawling", f"Homepage done, crawling subpages...", 8)
                resp = http.post(
                    "https://api.firecrawl.dev/v1/crawl",
                    headers={"Authorization": f"Bearer {firecrawl_key}", "Content-Type": "application/json"},
                    json={"url": url, "limit": max_pages, "scrapeOptions": {"formats": ["markdown"]}}, timeout=30
                )
                job_id = resp.json().get("id")
                if job_id:
                    for poll_i in range(40):
                        time.sleep(3)
                        poll = http.get(
                            f"https://api.firecrawl.dev/v1/crawl/{job_id}",
                            headers={"Authorization": f"Bearer {firecrawl_key}"}, timeout=15
                        ).json()
                        status = poll.get("status")
                        pages = poll.get("data", [])
                        page_count = len(pages)
                        # Live progress during polling
                        if status_callback:
                            crawl_pct = min(8 + (page_count / max_pages) * 17, 25)
                            status_callback("crawling", f"Crawled {page_count} pages...", crawl_pct)
                        if status == "completed" or (status == "scraping" and page_count >= max_pages - 2):
                            extra = [
                                {"url": p.get("metadata", {}).get("sourceURL", url), "markdown": p.get("markdown", "")}
                                for p in pages if p.get("markdown", "").strip() and len(p.get("markdown","")) > 500
                            ]
                            results.extend(extra)
                            break
                        if status == "failed":
                            break
            except:
                pass
            return results

        # Fallback to normal crawl
        if status_callback:
            status_callback("crawling", f"Starting full crawl...", 8)
        resp = http.post(
            "https://api.firecrawl.dev/v1/crawl",
            headers={"Authorization": f"Bearer {firecrawl_key}", "Content-Type": "application/json"},
            json={"url": url, "limit": max_pages, "scrapeOptions": {"formats": ["markdown"]}}, timeout=30
        )
        job_id = resp.json().get("id")
        if not job_id:
            return firecrawl_multi_scrape(url, firecrawl_key)

        for poll_i in range(36):
            time.sleep(5)
            poll = http.get(
                f"https://api.firecrawl.dev/v1/crawl/{job_id}",
                headers={"Authorization": f"Bearer {firecrawl_key}"}, timeout=15
            ).json()
            status = poll.get("status")
            pages = poll.get("data", [])
            page_count = len(pages)
            # Live progress during polling
            if status_callback:
                crawl_pct = min(8 + (page_count / max_pages) * 17, 25)
                status_callback("crawling", f"Crawled {page_count} pages...", crawl_pct)
            if status == "completed" or (status == "scraping" and page_count >= max_pages - 2):
                results = [
                    {"url": p.get("metadata", {}).get("sourceURL", url), "markdown": p.get("markdown", "")}
                    for p in pages if p.get("markdown","").strip() and len(p.get("markdown","")) > 500
                ]
                if results:
                    return results
                return firecrawl_multi_scrape(url, firecrawl_key)
            if status == "failed":
                break

        return firecrawl_multi_scrape(url, firecrawl_key)
    except:
        return firecrawl_multi_scrape(url, firecrawl_key)


# ── AI ANALYSIS ──────────────────────────────────────────────────────────────

def build_corpus(pages):
    chunks = []
    for p in pages:
        md = p.get("markdown", "").strip()
        if not md:
            continue
        md = re.sub(r'!\[.*?\]\(.*?\)', '', md)
        md = re.sub(r'\n{3,}', '\n\n', md)
        chunks.append(f"[PAGE: {p.get('url','')}]\n{md[:15000]}")
    return "\n\n---\n\n".join(chunks)[:40000]


def ask_deepseek(system, user, max_tokens=2000, temperature=0.1):
    try:
        resp = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"DeepSeek API error: {e}")
        return "{}"


def analyze_company(corpus):
    return ask_deepseek(
        "You are a B2B sales analyst for MIDAS IT (FEA/FEM software). Extract facts only. Respond in pure JSON, no markdown. CRITICAL: Translate ALL descriptive content into English. However, NEVER translate or modify people's names — keep all person names exactly as written.",
        f"""Return ONLY valid JSON:
{{
  "company_name": "string",
  "tagline": "string or null",
  "locations": ["cities where the company has offices or is based — include headquarters, branch offices, and any city mentioned as a company location"],
  "founded": "year or null",
  "employee_count": "string or null",
  "overview": ["bullet 1", "bullet 2", "bullet 3"],
  "engineering_capabilities": ["bullet 1"],
  "project_types": ["bridge"],
  "software_mentioned": ["any FEA/CAD/BIM tools"],
  "people": [{{"name": "Full Name", "role": "Job Title", "tier": "Owner|Founder|Director|Principal|Senior|Engineer|Graduate|Technician|Other"}}],
  "open_roles": [{{"title": "Job title", "skills": ["skill1"], "fem_mentioned": true}}],
  "projects": [{{"name": "Project name", "type": "Bridge|Building|Metro|Infrastructure|Residential|Industrial|Geotechnical|Tunnel|Foundation|Slope|Dam|Retaining Wall|Other", "location": "City or null", "client": "Client name or null", "description": "One sentence summary", "fem_relevant": true}}],
  "confidence": "High|Medium|Low",
  "confidence_reason": "One sentence explaining why"
}}
Extract ALL people — engineering and technical staff only.
For locations: include any city mentioned as company headquarters, office, or base location — check footer addresses, contact pages, and about sections.
For employee_count: check ALL sources including Glassdoor, LinkedIn, Companies House.
For projects: extract ALL completed or ongoing projects mentioned anywhere.
For fem_relevant: set to true if the project involves structural analysis, FEA, FEM, geotechnical modelling, soil analysis, slope stability, foundation design, tunnelling, or any work where engineering analysis software would be used.
Website content:
{corpus}""",
        max_tokens=8000
    )


MIDAS_PRODUCTS = """
MIDAS NX PRODUCT SUITE — FULL SALES KNOWLEDGE BASE

════════════════════════════════════════════════
1. MIDAS CIVIL NX — Bridges & Civil Infrastructure
════════════════════════════════════════════════
WHAT IT IS: Next-generation structural analysis and design software specialised for bridges and civil infrastructure. Combines advanced FEA, automation, and integrated design workflows.

WHAT IT DOES:
- Structural modelling and analysis (static, dynamic, seismic, nonlinear, time-history)
- Construction stage simulation — analyse structures at every phase of build
- Moving load and traffic simulation — vehicles across bridges, stress/deflection/safety
- Design and code checking — international standards, automated compliance verification
- Automation and parametric workflows — Excel-based model generation, API, batch processing
- BIM interoperability and integration with other MIDAS tools

KEY CAPABILITIES:
- Linear and nonlinear analysis including large displacement and material nonlinearity
- Seismic and pushover analysis
- Pre-built templates for bridges, culverts, and infrastructure
- Auto-meshing, fast post-processing, detailed reporting
- Moving loads, construction loads, environmental loads (temperature, seismic)
- Excel integration and API/plugin ecosystem

TYPICAL USE CASES:
- Cable-stayed, suspension, PSC, and steel bridge design and validation
- Highway and railway bridges with traffic load simulation
- Construction stage analysis for segmental construction and temporary supports
- Earthquake response studies and time-dependent behaviour
- Water treatment facilities and underground/industrial structures
- Parametric design optimisation and bulk scenario analysis

VALUE PROPOSITION: High accuracy FEA + automation + specialised bridge focus = complete bridge design lifecycle in one platform.
POSITIONING: Best global analysis tool for bridge and infrastructure firms.

════════════════════════════════════════════════
2. MIDAS GEN NX — Buildings & General Structures
════════════════════════════════════════════════
WHAT IT IS: Next-generation structural analysis and design platform for buildings and general structures. Integrates modelling, analysis, design, and automation in one environment.

WHAT IT DOES:
- Models RC, steel, and composite structures
- Static, dynamic, seismic, and nonlinear analysis
- Building design and code compliance — international design codes, automated steel and RC design
- Integrated workflow: Modelling → Analysis → Design in one platform
- Automation via Excel, Grasshopper, and API for custom workflows
- AI-assisted workflow with built-in guidance and smart tools

KEY CAPABILITIES:
- 4K-ready modern UI with customisable toolbars
- Advanced FEA with fast solver
- Automated design optimisation for cost and material efficiency
- Pushover analysis and nonlinear time-history for seismic design
- Excel-driven parametric workflows and Grasshopper integration
- Auto-generated reports and calculations

TYPICAL USE CASES:
- High-rise, residential, and commercial building design
- Steel, RC, and composite structure engineering
- Earthquake-resistant seismic design
- Batch processing and parametric design optimisation
- Industrial structures — factories and plants

VALUE PROPOSITION: Efficiency through automation + accuracy through advanced analysis + modern usability = integrated building design workflow.
POSITIONING: Best global design tool for building and general structure firms.

════════════════════════════════════════════════
3. MIDAS FEA NX — Detailed Local & Nonlinear Analysis
════════════════════════════════════════════════
WHAT IT IS: High-end finite element analysis software designed for detailed, local, and nonlinear analysis of civil and structural systems. Used when global tools are not sufficient. Also used as a research and academic FEM platform.

WHAT IT DOES:
- 2D and 3D element modelling (plates, solids) for complex geometry
- Advanced linear and nonlinear analysis — material nonlinearity, cracking, yielding, large deformation
- CAD-based modelling with import from AutoCAD, SolidWorks, STEP, IGES
- Automatic, mapped, and hybrid mesh generation
- Multi-physics analysis — structural, geotechnical, crack, fatigue, buckling, thermal
- Integrates with CIVIL NX and GEN NX for global-to-local workflows

KEY CAPABILITIES:
- Crack modelling, contact and interface behaviour, plasticity and failure simulation
- High-quality meshing for accurate geometry representation
- 3D solid and plate modelling for detailed joints, anchors, and connections
- Stress contours, crack visualisation, deformation plots
- Parallel computing for large models
- Modern UI for faster preprocessing and postprocessing

TYPICAL USE CASES — COMMERCIAL:
- Steel connections, anchor zones, bridge joints
- Deep beams, shear walls, slabs
- Bridge local analysis — anchorage zones, bearings
- Geotechnical analysis — foundations, soil-structure interaction details
- Failure analysis — concrete cracking, fatigue
- Nonlinear problems — large deformation, contact problems

TYPICAL USE CASES — RESEARCH & ACADEMIC:
- University structural engineering research (PhD, Masters thesis work)
- Parametric FEM studies — material model validation, mesh sensitivity analysis
- Novel structural system investigation — new connection types, innovative materials
- Concrete cracking and fracture mechanics research
- Composite material behaviour modelling
- Progressive collapse and failure mechanism studies
- Benchmark studies comparing FEA results with experimental data
- Seismic performance research on new structural systems
- FRP strengthening analysis and bond-slip modelling

CRITICAL — FEA NX TARGET COMPANIES (often missed):
- University civil/structural engineering departments (research licences)
- R&D departments within large engineering firms
- Structural forensic investigation firms (failure analysis, accident reconstruction)
- Testing laboratories that need FEM to correlate with physical test results
- Specialist consultancies doing unusual/non-standard structural analysis
- Firms doing structural health monitoring that need FEM baseline models
- Companies developing new construction products (need FEA validation)

FEA NX COMPETITIVE POSITIONING:
- vs ANSYS/ABAQUS → More accessible for civil engineers, civil-specific material models and workflows, lower cost for civil applications
- vs DIANA → Comparable nonlinear capabilities, better integration with MIDAS ecosystem
- vs ATENA → Competitive for concrete analysis, broader capability range
- vs LS-DYNA → More suitable for quasi-static civil problems vs crash/impact dynamics

VALUE PROPOSITION: High accuracy for complex local analysis that global tools cannot handle. Seamless integration with CIVIL NX and GEN NX. Also a standalone research-grade FEM platform for universities and R&D.
POSITIONING: Detailed local analysis tool — pairs with CIVIL NX or GEN NX for commercial work. Standalone research FEM platform for academic and R&D users.

════════════════════════════════════════════════
4. MIDAS GTS NX — Geotechnical Analysis
════════════════════════════════════════════════
WHAT IT IS: Geotechnical analysis software for soil, rock, and underground engineering problems. Focuses on ground behaviour, soil-structure interaction, and construction processes.

WHAT IT DOES:
- 2D and 3D FEA of soil and rock behaviour — deformation, stress, stability
- Soil-structure interaction — foundations, retaining walls, tunnels
- Excavation and construction stage analysis — staged construction, deep excavation, tunnelling
- Groundwater and seepage analysis — water flow through soil, hydrostatic pressure
- Dynamic and seismic analysis — earthquake response, vibration
- CAD-based 2D/3D modelling with CAD import

KEY CAPABILITIES:
- Advanced material models — elastic and nonlinear soil behaviour
- High-quality automatic and hybrid mesh generation for geotechnical problems
- 3D terrain modelling from borehole data with layered soil modelling
- Static, nonlinear, dynamic, construction stage, and slope stability analysis
- Contours, deformation, vectors — full ground behaviour visualisation
- Automated result reports with Excel export

TYPICAL USE CASES:
- Foundation engineering — shallow and deep foundations, pile analysis
- Metro tunnels and underground caverns
- Deep excavation projects and retaining structures
- Slope stability — landslides and open pit mining
- Dam engineering and seepage analysis
- Soil-structure interaction for buildings and bridges
- Embankment design and ground improvement modelling
- Landfill engineering and environmental geotechnics
- Consolidation and settlement analysis

CRITICAL — GTS NX TARGET COMPANIES (often missed):
- Geotechnical investigation/research firms (soil testing, borehole analysis, geomechanics labs)
- Ground engineering consultancies
- Geotechnical design firms (foundations, retaining walls, piling)
- Tunnelling and underground construction firms
- Mining and quarry engineering
- Dam and reservoir engineers
- Slope stability and landslide assessment firms
- Environmental geotechnics (landfill, contamination modelling)
- Firms doing CPT, SPT, triaxial testing, plate load tests — they need GTS NX to model results
- Any firm producing geotechnical elaborates, soil investigation reports, or foundation recommendations

GTS NX COMPETITIVE POSITIONING:
- vs PLAXIS → Better BIM integration, better mesh generation, more intuitive UI, better value
- vs GeoStudio/SLOPE/W → Full 3D FEA vs simplified 2D limit equilibrium
- vs FLAC/UDEC → More accessible for consulting engineers, faster model setup
- vs manual/spreadsheet calculations → Automated FEA replaces conservative hand calculations

VALUE PROPOSITION: Accurate soil and rock modelling + advanced geotechnical capabilities + full ground engineering coverage.
POSITIONING: Essential for any firm doing geotechnical, tunnelling, underground, or foundation work.

════════════════════════════════════════════════
CROSS-SELL LOGIC — MATCH TO COMPANY TYPE
════════════════════════════════════════════════
- Bridge/infrastructure firm → CIVIL NX (primary) + FEA NX (local detailing)
- Building/structural firm → GEN NX (primary) + FEA NX (connection design)
- Geotechnical/ground engineering firm → GTS NX (primary) + CIVIL NX (structure interaction)
- Geotech investigation/research firm → GTS NX (they model soil behaviour from their test data)
- Piling/foundation specialist → GTS NX + CIVIL NX (soil-structure interaction)
- Mixed civil firm (bridges + buildings) → CIVIL NX + GEN NX + FEA NX
- Full service firm (all disciplines) → Full suite: CIVIL NX + GEN NX + FEA NX + GTS NX
- Metro/tunnelling firm → GTS NX + CIVIL NX
- Dam/embankment firm → GTS NX + CIVIL NX
- Consulting/advisory firm → Start with CIVIL NX or GEN NX depending on focus
- University/research institution → FEA NX (research licence) + relevant NX product for teaching
- Structural forensic/testing lab → FEA NX (failure analysis, test correlation)
- R&D department → FEA NX (validation, parametric studies)

════════════════════════════════════════════════
COMPETITIVE POSITIONING
════════════════════════════════════════════════
- vs LUSAS/STAAD/SAP2000 → MIDAS offers better automation, modern UI, and construction stage analysis
- vs PLAXIS → GTS NX is directly competitive, with better BIM integration and CAD workflow
- vs ETABS → GEN NX offers more automation and parametric design capabilities
- vs ANSYS/ABAQUS → FEA NX is more accessible for civil engineers with civil-specific workflows and lower cost
- vs DIANA/ATENA → FEA NX is competitive for nonlinear concrete analysis with better ecosystem integration
- vs GeoStudio → GTS NX offers full 3D FEA vs simplified 2D methods
- No existing FEA software detected → Clean opportunity, position as first professional FEA platform

════════════════════════════════════════════════
ONE-LINE MASTER PITCH
════════════════════════════════════════════════
MIDAS NX Suite provides a complete structural and geotechnical engineering ecosystem — from global bridge and building design to detailed local analysis, ground engineering, and research-grade FEM — all within one integrated, automated workflow.
"""


def analyze_sales(corpus, company_json):
    """Two-phase: DeepSeek provides strategy + factual signals, Python calculates score."""
    raw = ask_deepseek(
        f"You are a senior B2B sales strategist for MIDAS IT. Use the product knowledge below. Be specific and actionable. Respond in pure JSON, no markdown. Always respond in English.\n\n{MIDAS_PRODUCTS}",
        f"""Return ONLY valid JSON with sales strategy AND factual scoring signals.

{{
  "fem_opportunities": ["detailed specific use case 1", "use case 2", "use case 3"],
  "pain_points": ["specific pain point", "pain 2", "pain 3"],
  "entry_point": "Specific person name and role to approach first, with reasoning",
  "value_positioning": "2-3 sentence positioning of MIDAS for this company",
  "likely_objections": ["specific objection", "objection 2", "objection 3"],
  "hiring_signals": ["specific signal", "signal 2"],
  "expansion_signals": ["specific expansion signal", "signal 2"],
  "pre_meeting_mention": ["specific thing about their projects", "thing 2", "thing 3"],
  "smart_questions": ["specific question about their workflow", "question 2", "question 3"],
  "opening_line": "One strong personalised opening line",
  "recommended_products": ["only from: CIVIL NX, GEN NX, FEA NX, GTS NX"],
  "product_reason": "3-4 sentence explanation of why these products fit",

  "signals": {{
    "core_service": "structural_only|geotech_only|structural_and_geotech|multi_discipline_with_structural|civil_no_structural|not_engineering",
    "project_complexity": "complex|moderate|simple|none",
    "fem_evidence": "explicit_fem_mentioned|likely_fem_from_projects|possible_fem|no_fem",
    "competitor_software": "none_detected|basic_tools_only|competitor_detected|locked_in",
    "competitor_names": ["list any FEA/structural software mentioned"],
    "company_size": "micro_1_10|small_11_50|medium_51_200|large_201_plus|unknown",
    "people_found_count": 0,
    "decision_makers_found": true,
    "hiring_structural": false,
    "hiring_any": false,
    "recent_project_wins": false,
    "expanding_offices": false,
    "is_government_body": false,
    "is_university": false,
    "project_count_on_site": 0,
    "has_bridges": false,
    "has_buildings": false,
    "has_geotech": false,
    "has_tunnels": false,
    "has_foundations": false,
    "has_dams": false,
    "has_marine": false
  }}
}}

For signals: answer each field based ONLY on evidence from the data. If unsure, pick the conservative option. Do not guess or inflate.

Company data: {company_json}
Website excerpt: {corpus[:4000]}""",
        max_tokens=4000
    )

    sales_data = safe_json(raw)
    sig = sales_data.get("signals", {})
    original_sig = dict(sig)

    # Repair conservative/incorrect LLM scoring signals with facts already extracted
    # from the company profile. The LLM is useful for strategy text, but it can miss
    # obvious FEM relevance in projects and then mark strong engineering firms cold.
    company_data = safe_json(company_json) if isinstance(company_json, str) else (company_json or {})
    if not isinstance(company_data, dict):
        company_data = {}

    def as_list(value):
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    def text_join(*values):
        parts = []
        for value in values:
            if isinstance(value, dict):
                parts.extend(str(v) for v in value.values() if v)
            elif isinstance(value, list):
                parts.extend(text_join(v) for v in value)
            elif value:
                parts.append(str(value))
        return " ".join(parts).lower()

    projects = [p for p in as_list(company_data.get("projects")) if isinstance(p, dict)]
    project_types = [str(t).lower() for t in as_list(company_data.get("project_types"))]
    software = [str(s).strip() for s in as_list(company_data.get("software_mentioned")) if str(s).strip()]
    company_blob = text_join(
        company_data.get("tagline"),
        company_data.get("overview"),
        company_data.get("engineering_capabilities"),
        company_data.get("project_types"),
        projects,
        company_data.get("open_roles"),
        corpus[:8000],
    )

    def has_any(terms, blob=company_blob):
        return any(term in blob for term in terms)

    bridge_terms = ("bridge", "viaduct", "flyover", "highway", "railway", "rail", "transport infrastructure")
    building_terms = ("building", "residential", "commercial", "mixed-use", "mixed use", "high-rise", "housing")
    geotech_terms = ("geotechnical", "ground engineering", "soil", "slope", "retaining", "basement", "excavation", "earthworks")
    tunnel_terms = ("tunnel", "tunnelling", "underground", "metro")
    foundation_terms = ("foundation", "piling", "pile", "underpinning")
    dam_terms = ("dam", "reservoir", "embankment")
    marine_terms = ("marine", "coastal", "harbour", "port", "quay")
    structural_terms = (
        "structural engineer", "structural engineering", "structural design",
        "civil engineer", "civil engineering", "temporary works", "steelwork",
        "reinforced concrete", "rc frame", "frame design", "facade engineering",
    )
    non_engineering_terms = ("marketing agency", "law firm", "accountancy", "restaurant", "retail shop")

    type_blob = " ".join(project_types)
    has_bridges = has_any(bridge_terms) or "bridge" in type_blob
    has_buildings = has_any(building_terms) or any(t in type_blob for t in ("building", "residential", "industrial"))
    has_geotech = has_any(geotech_terms) or "geotechnical" in type_blob
    has_tunnels = has_any(tunnel_terms) or "tunnel" in type_blob
    has_foundations = has_any(foundation_terms) or "foundation" in type_blob
    has_dams = has_any(dam_terms) or "dam" in type_blob
    has_marine = has_any(marine_terms)

    detected_flags = {
        "has_bridges": has_bridges,
        "has_buildings": has_buildings,
        "has_geotech": has_geotech,
        "has_tunnels": has_tunnels,
        "has_foundations": has_foundations,
        "has_dams": has_dams,
        "has_marine": has_marine,
    }
    for key, detected in detected_flags.items():
        if detected:
            sig[key] = True
    detected_labels = [
        label for key, label in (
            ("has_bridges", "bridge/infrastructure work"),
            ("has_buildings", "building/general structural work"),
            ("has_geotech", "geotechnical or retaining works"),
            ("has_tunnels", "tunnel/underground work"),
            ("has_foundations", "foundation or piling work"),
            ("has_dams", "dam/reservoir/embankment work"),
            ("has_marine", "marine/coastal work"),
        )
        if sig.get(key)
    ]

    fem_project_count = sum(1 for p in projects if p.get("fem_relevant"))
    engineering_project_count = sum(1 for detected in detected_flags.values() if detected)
    if projects and not sig.get("project_count_on_site"):
        sig["project_count_on_site"] = len(projects)

    explicit_fem_terms = (
        "fea", "fem", "finite element", "finite-element", "analysis model",
        "nonlinear", "non-linear", "seismic", "dynamic analysis", "structural analysis",
        "soil analysis", "slope stability", "construction stage", "load analysis",
    )
    fem_from_text = has_any(explicit_fem_terms)
    fem_evidence_items = []
    if fem_project_count:
        fem_evidence_items.append(f"{fem_project_count} FEM-relevant project(s)")
    if detected_labels:
        fem_evidence_items.extend(detected_labels[:3])
    if fem_from_text:
        fem_evidence_items.insert(0, "analysis/FEM language found")
    complex_terms = (
        "bridge", "tunnel", "geotechnical", "foundation", "retaining", "dam",
        "seismic", "nonlinear", "non-linear", "temporary works", "rail",
        "metro", "underground", "high-rise", "deep excavation",
    )

    if fem_from_text:
        sig["fem_evidence"] = "explicit_fem_mentioned"
    elif fem_project_count or has_bridges or has_geotech or has_tunnels or has_foundations or has_dams:
        if sig.get("fem_evidence") in (None, "", "no_fem", "possible_fem"):
            sig["fem_evidence"] = "likely_fem_from_projects"
    elif engineering_project_count or has_any(structural_terms):
        if sig.get("fem_evidence") in (None, "", "no_fem"):
            sig["fem_evidence"] = "possible_fem"

    if has_any(complex_terms) or fem_project_count:
        if sig.get("project_complexity") in (None, "", "none", "simple"):
            sig["project_complexity"] = "complex"
    elif engineering_project_count and sig.get("project_complexity") in (None, "", "none"):
        sig["project_complexity"] = "moderate"

    if (has_geotech or has_tunnels or has_foundations or has_dams) and (has_bridges or has_buildings or has_any(structural_terms)):
        sig["core_service"] = "structural_and_geotech"
    elif has_geotech or has_tunnels or has_foundations or has_dams:
        if sig.get("core_service") in (None, "", "not_engineering", "civil_no_structural"):
            sig["core_service"] = "geotech_only"
    elif has_bridges or has_buildings or has_any(structural_terms):
        if sig.get("core_service") in (None, "", "not_engineering", "civil_no_structural"):
            sig["core_service"] = "structural_only"
    elif has_any(("civil engineering", "infrastructure", "engineering consultancy")) and not has_any(non_engineering_terms):
        if sig.get("core_service") in (None, "", "not_engineering"):
            sig["core_service"] = "multi_discipline_with_structural"

    if software:
        lower_sw = " ".join(software).lower()
        competitor_terms = ("etabs", "sap2000", "staad", "tekla", "robot", "plaxis", "lusas", "ansys", "abaqus", "diana", "atena", "sofistik")
        basic_terms = ("autocad", "revit", "civil 3d", "civils 3d", "navisworks", "bim")
        if any(term in lower_sw for term in competitor_terms):
            sig["competitor_software"] = "competitor_detected"
        elif any(term in lower_sw for term in basic_terms) and sig.get("competitor_software") in (None, "", "none_detected"):
            sig["competitor_software"] = "basic_tools_only"

    people = as_list(company_data.get("people"))
    if people and not sig.get("people_found_count"):
        sig["people_found_count"] = len(people)
    if people and not sig.get("decision_makers_found"):
        decision_terms = ("owner", "founder", "director", "principal", "partner", "associate")
        sig["decision_makers_found"] = any(
            any(term in str(p.get("tier", "") + " " + p.get("role", "")).lower() for term in decision_terms)
            for p in people if isinstance(p, dict)
        )

    if sig.get("company_size") in (None, "", "unknown"):
        emp_text = str(company_data.get("employee_count") or "").lower()
        nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", emp_text)]
        emp_n = nums[0] if nums else len(people)
        if emp_n:
            if emp_n <= 10:
                sig["company_size"] = "micro_1_10"
            elif emp_n <= 50:
                sig["company_size"] = "small_11_50"
            elif emp_n <= 200:
                sig["company_size"] = "medium_51_200"
            else:
                sig["company_size"] = "large_201_plus"

    correction_keys = (
        "core_service", "fem_evidence", "project_complexity", "project_count_on_site",
        "competitor_software", "people_found_count", "decision_makers_found", "company_size",
        "has_bridges", "has_buildings", "has_geotech", "has_tunnels",
        "has_foundations", "has_dams", "has_marine",
    )
    signal_corrections = []
    for key in correction_keys:
        before = original_sig.get(key)
        after = sig.get(key)
        if before != after and after not in (None, "", False):
            signal_corrections.append({
                "field": key,
                "from": before,
                "to": after,
            })

    # ══ DETERMINISTIC SCORING — Python calculates, LLM only provides facts ══

    # Category 1: Structural/Geotech Relevance (0-30)
    core = sig.get("core_service", "not_engineering")
    rel_base = {"structural_and_geotech": 28, "structural_only": 26, "geotech_only": 25,
                "multi_discipline_with_structural": 18, "civil_no_structural": 7, "not_engineering": 1}
    rel = rel_base.get(core, 4)
    proj_flags = [sig.get(k, False) for k in ("has_bridges","has_buildings","has_geotech","has_tunnels","has_foundations","has_dams","has_marine")]
    rel = min(30, rel + sum(proj_flags))

    # Category 2: FEM/FEA Need (0-25)
    fem_ev = sig.get("fem_evidence", "no_fem")
    fem_base = {"explicit_fem_mentioned": 22, "likely_fem_from_projects": 16, "possible_fem": 9, "no_fem": 1}
    fem = fem_base.get(fem_ev, 4)
    fem = min(25, fem + {"complex": 3, "moderate": 1, "simple": 0, "none": 0}.get(sig.get("project_complexity", "none"), 0))

    # Category 3: Buying Signals (0-20)
    buy = 3
    if sig.get("hiring_structural"): buy += 5
    elif sig.get("hiring_any"): buy += 2
    if sig.get("recent_project_wins"): buy += 4
    if sig.get("expanding_offices"): buy += 3
    pc = sig.get("project_count_on_site", 0) or 0
    if pc > 20: buy += 4
    elif pc > 10: buy += 3
    elif pc > 5: buy += 2
    elif pc > 0: buy += 1
    buy = min(20, buy)

    # Category 4: Accessibility (0-15)
    acc = 3
    ppl = sig.get("people_found_count", 0) or 0
    if ppl >= 10: acc += 3
    elif ppl >= 5: acc += 4
    elif ppl >= 2: acc += 5
    elif ppl >= 1: acc += 3
    if sig.get("decision_makers_found"): acc += 3
    sz = sig.get("company_size", "unknown")
    if sz in ("micro_1_10", "small_11_50"): acc += 2
    elif sz == "large_201_plus": acc -= 2
    if sig.get("is_government_body"): acc -= 4
    if sig.get("is_university"): acc += 1
    acc = max(0, min(15, acc))

    # Category 5: Competitive Landscape (0-10)
    cmp = {"none_detected": 9, "basic_tools_only": 6, "competitor_detected": 3, "locked_in": 1}.get(sig.get("competitor_software", "none_detected"), 5)

    # Total
    lead_score = max(0, min(100, rel + fem + buy + acc + cmp))
    overall = "Hot" if lead_score >= 70 else ("Warm" if lead_score >= 40 else "Cold")
    evidence_summary = "; ".join(dict.fromkeys(fem_evidence_items or detected_labels)) or "no strong structural/FEM evidence detected"
    structural_reason = f"Core: {core.replace('_',' ')}"
    if detected_labels:
        structural_reason += f"; evidence: {', '.join(detected_labels[:4])}"
    else:
        structural_reason += f"; {sum(proj_flags)} project type(s)"
    fem_reason = f"FEM evidence: {fem_ev.replace('_',' ')}, complexity: {sig.get('project_complexity','unknown')}"
    if fem_evidence_items:
        fem_reason += f"; based on {', '.join(dict.fromkeys(fem_evidence_items[:4]))}"

    # Build breakdown
    sales_data["lead_score"] = lead_score
    sales_data["score_breakdown"] = {
        "structural_relevance": {"score": rel, "reason": structural_reason},
        "fem_need": {"score": fem, "reason": fem_reason},
        "buying_signals": {"score": buy, "reason": f"Hiring structural: {sig.get('hiring_structural',False)}, projects: {pc}, expanding: {sig.get('expanding_offices',False)}"},
        "accessibility": {"score": acc, "reason": f"{ppl} people, decision makers: {sig.get('decision_makers_found',False)}, size: {sz.replace('_',' ')}"},
        "competitive_landscape": {"score": cmp, "reason": f"Software: {sig.get('competitor_software','unknown').replace('_',' ')}" + (f" ({', '.join(sig.get('competitor_names',[]))})" if sig.get('competitor_names') else "")}
    }
    sales_data["score_evidence"] = list(dict.fromkeys(fem_evidence_items or detected_labels))
    sales_data["signal_corrections"] = signal_corrections
    sales_data["overall_score"] = overall
    sales_data["score_reason"] = f"Score {lead_score}/100 ({overall}). {core.replace('_',' ').title()} with {fem_ev.replace('_',' ')}. Evidence: {evidence_summary}. {ppl} people found; software: {sig.get('competitor_software','unknown').replace('_',' ')}."

    if lead_score < 30:
        sales_data["recommended_products"] = []
        sales_data["fem_opportunities"] = ["No direct FEM/FEA opportunities identified"]

    sales_data.pop("signals", None)
    return json.dumps(sales_data)


def generate_email_text(company_data, sales_data):
    return ask_deepseek(
        "You are a B2B sales expert writing cold outreach emails for MIDAS IT (FEA/FEM structural analysis software). Write natural, human-sounding emails.",
        f"""Write a cold outreach email to a key contact at this engineering company.

Company: {company_data.get('company_name', '')}
Entry point: {sales_data.get('entry_point', '')}
Opening line: {sales_data.get('opening_line', '')}
Value positioning: {sales_data.get('value_positioning', '')}
FEM opportunities: {', '.join(sales_data.get('fem_opportunities', [])[:2])}
Pre-meeting mentions: {', '.join(sales_data.get('pre_meeting_mention', [])[:2])}

Requirements:
- Subject line first (prefix with "Subject: ")
- 4-5 short paragraphs
- One clear call to action (15-min call)
- Professional but conversational tone
- Sign off as the MIDAS IT team

Return plain text only.""",
        max_tokens=800,
        temperature=0.4
    )


# ── ENRICHMENT LOOKUPS ───────────────────────────────────────────────────────

def search_people_via_serpapi(company_name, domain):
    try:
        all_text = ""
        queries = [
            f"site:{domain} (team OR people OR staff OR leadership OR directors)",
            f'site:linkedin.com/in "{company_name}" (engineer OR structural OR civil)',
            f'site:linkedin.com/in "{company_name}" (director OR principal OR associate)',
            f'"{company_name}" (ANSYS OR ABAQUS OR SAP2000 OR ETABS OR STAAD OR LUSAS OR MIDAS)',
            f'"{company_name}" (hiring OR careers OR jobs) (structural OR civil)',
            f'"{company_name}" (project OR bridge OR infrastructure) (engineering OR structural)'
        ]
        for q in queries:
            results = serpapi_search(q, num_results=8)
            all_text += "\n\n" + format_serpapi_results(results, max_chars=1500)
        return all_text[:8000]
    except:
        return ""


def lookup_companies_house(company_name, locations=None):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        uk_keywords = ["london","manchester","birmingham","leeds","bristol","edinburgh",
                        "glasgow","liverpool","uk","england","scotland","wales","united kingdom","britain"]
        eu_keywords = ["germany","berlin","munich","france","paris","netherlands","amsterdam",
                        "belgium","brussels","switzerland","zurich","austria","vienna",
                        "ireland","dublin","spain","madrid","barcelona","italy","rome","milan",
                        "poland","warsaw","czech","prague","sweden","stockholm","norway","oslo",
                        "denmark","copenhagen","finland","helsinki","romania","bucharest",
                        "hungary","budapest","portugal","lisbon","greece","athens"]

        location_str = " ".join(locations or []).lower()
        is_uk = any(kw in location_str for kw in uk_keywords)
        is_eu = any(kw in location_str for kw in eu_keywords)

        all_text = ""
        director_count = 0

        # UK — Companies House API
        if is_uk or (not is_uk and not is_eu):
            if COMPANIES_HOUSE_KEY:
                try:
                    search_resp = http.get(
                        f"https://api.company-information.service.gov.uk/search/companies?q={company_name.replace(' ', '+')}",
                        auth=(COMPANIES_HOUSE_KEY, ""), timeout=10
                    )
                    results = search_resp.json().get("items", [])
                    if results:
                        company_number = results[0].get("company_number", "")
                        officers_resp = http.get(
                            f"https://api.company-information.service.gov.uk/company/{company_number}/officers",
                            auth=(COMPANIES_HOUSE_KEY, ""), timeout=10
                        )
                        officers = officers_resp.json().get("items", [])
                        officer_text = "\n".join([
                            f"{o.get('name','')} — {o.get('officer_role','')} (appointed {o.get('appointed_on','')})"
                            for o in officers if o.get('resigned_on') is None
                        ])
                        company_info = results[0]
                        text = f"""Company: {company_info.get('title','')}
Status: {company_info.get('company_status','')}
Incorporated: {company_info.get('date_of_creation','')}

Active Officers:
{officer_text}"""
                        director_count = len([o for o in officers if 'director' in o.get('officer_role','').lower()])
                        all_text += f"[Companies House UK]\n{text}"
                except:
                    pass

        # EU — OpenCorporates
        if is_eu or (not is_uk and not is_eu):
            try:
                oc_url = f"https://opencorporates.com/companies?q={company_name.replace(' ', '+')}"
                resp = http.get(oc_url, headers=headers, timeout=10)
                soup = BeautifulSoup(resp.text, "html.parser")
                result = soup.find("a", class_="company_search_result")
                if result:
                    company_url = "https://opencorporates.com" + result["href"]
                    resp2 = http.get(company_url, headers=headers, timeout=10)
                    soup2 = BeautifulSoup(resp2.text, "html.parser")
                    text = soup2.get_text(separator="\n", strip=True)
                    director_count += text.lower().count("director")
                    all_text += f"\n\n[OpenCorporates EU]\n{text[:3000]}"
            except:
                pass

        # EU Tenders — TED
        try:
            ted_url = f"https://ted.europa.eu/en/search?scope=NOTICE&query={company_name.replace(' ', '+')}"
            resp = http.get(ted_url, headers=headers, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 200:
                all_text += f"\n\n[EU Tenders TED]\n{text[:2000]}"
        except:
            pass

        return all_text[:6000], director_count
    except:
        return "", 0


def lookup_linkedin_company(company_name, domain=""):
    try:
        results = []
        queries = [
            f'site:linkedin.com/company "{company_name}"',
            f'linkedin.com "{company_name}" "{domain}"',
        ]
        for query in queries:
            results.extend(serpapi_search(query, num_results=6))

        text = format_serpapi_results(results, max_chars=5000)

        # Smart employee extraction:
        # 1. First try to find a LinkedIn result that mentions the domain — that's the right company
        # 2. If no domain match, look for results with the EXACT company name in the link/title
        # 3. If multiple employee counts found, prefer the smallest (small firms get drowned by big ones)
        employee_signal = ""

        # Pass 1: results that contain the domain — strongest signal
        domain_short = domain.replace(".co.uk", "").replace(".com", "").replace(".org", "")
        for r in results:
            r_text = f"{r.get('title','')} {r.get('snippet','')} {r.get('link','')}".lower()
            if domain in r_text or (domain_short and domain_short in r_text):
                emp = extract_employee_count_from_text(r_text)
                if emp:
                    employee_signal = emp
                    break

        # Pass 2: if no domain-matched result, look for the LinkedIn company page specifically
        if not employee_signal:
            for r in results:
                link = r.get("link", "").lower()
                # Only trust linkedin.com/company/ pages, not random mentions
                if "linkedin.com/company/" in link:
                    snippet = r.get("snippet", "")
                    emp = extract_employee_count_from_text(snippet)
                    if emp:
                        # Sanity check: if the website we crawled only has a few pages of content
                        # and the LinkedIn says 1000+ employees, it's probably the wrong company
                        employee_signal = emp
                        break

        return text, employee_signal
    except:
        return "", ""


def lookup_glassdoor(company_name, domain):
    try:
        # Include domain to anchor to the right company
        domain_short = domain.replace(".co.uk", "").replace(".com", "").replace(".org", "")
        all_text = format_serpapi_results(
            serpapi_search(f'glassdoor "{company_name}" {domain_short} reviews', num_results=10),
            max_chars=2000
        )
        all_text += "\n\n" + format_serpapi_results(
            serpapi_search(f'"{company_name}" {domain_short} employees size', num_results=10),
            max_chars=2000
        )

        # Fallback without domain if nothing found
        if len(all_text.strip()) < 100:
            all_text = format_serpapi_results(
                serpapi_search(f'glassdoor "{company_name}" reviews engineers', num_results=10),
                max_chars=2000
            )
            all_text += "\n\n" + format_serpapi_results(
                serpapi_search(f'"{company_name}" employees size glassdoor linkedin indeed', num_results=10),
                max_chars=2000
            )

        return all_text[:5000], all_text.lower().count("glassdoor")
    except:
        return "", 0


def lookup_planning_portal(company_name):
    try:
        text = format_serpapi_results(
            serpapi_search(f'"{company_name}" planning application structural engineer', num_results=10),
            max_chars=3000
        )
        return text, text.lower().count("planning")
    except:
        return "", 0


# ── FAST SUPPLEMENT ANALYSIS (replaces full re-analysis) ─────────

def analyze_supplement(extra_corpus, existing_people_count, existing_projects_count):
    """Small targeted AI call — only extracts people/projects from enrichment sources.
    Much faster than re-running the full analyze_company on the entire corpus."""
    if not extra_corpus.strip():
        return "{}"
    return ask_deepseek(
        "You are a B2B sales analyst. Extract ONLY new people and projects from these supplementary sources. Respond in pure JSON, no markdown. Keep names exactly as written.",
        f"""These are supplementary data sources (LinkedIn, Companies House, Glassdoor, planning records).
I already have {existing_people_count} people and {existing_projects_count} projects from the main website.
Extract ONLY additional people and projects NOT already covered.

Return ONLY valid JSON:
{{
  "people": [{{"name": "Full Name", "role": "Job Title", "tier": "Owner|Founder|Director|Principal|Senior|Engineer|Graduate|Technician|Other"}}],
  "projects": [{{"name": "Project name", "type": "Bridge|Building|Metro|Infrastructure|Residential|Industrial|Geotechnical|Tunnel|Foundation|Slope|Dam|Retaining Wall|Other", "location": "City or null", "client": "Client name or null", "description": "One sentence summary", "fem_relevant": true}}],
  "locations": ["additional office cities found"],
  "founded": "year if found, else null",
  "employee_count": "string if found, else null"
}}

Supplementary sources:
{extra_corpus[:12000]}""",
        max_tokens=4000
    )


def quick_extract_company_name(pages, domain):
    """Fast extraction of company name from page title/content without AI.
    Used to start enrichment lookups immediately while AI analysis runs."""
    for p in pages[:3]:
        md = p.get("markdown", "")
        # Try first heading
        for line in md.split("\n")[:20]:
            line = line.strip().strip("#").strip()
            if 10 < len(line) < 80 and not line.startswith("[") and not line.startswith("http"):
                # Clean up common suffixes
                for suffix in [" - Home", " | Home", " – Home", " - Welcome", " | Welcome"]:
                    if line.endswith(suffix):
                        line = line[:-len(suffix)].strip()
                if line:
                    return line
    # Fallback: capitalize the domain name
    name = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return name.title()


# ── FULL ANALYSIS PIPELINE ───────────────────────────────────────────────────

def analyse_single_url(website_url, firecrawl_key, status_callback=None, should_save=None):
    """Run full analysis pipeline for one URL. Returns (entry_dict, error_str).
    
    Optimised pipeline (parallel where possible):
      1. Crawl website
      2. IN PARALLEL: AI analysis + enrichment lookups (enrichment uses quick name extraction)
      3. Small supplement AI call for enrichment gaps (NOT a full re-analysis)
      4. Sales strategy AI call
    """
    try:
        if not website_url.startswith("http"):
            website_url = "https://" + website_url

        _domain = extract_domain(website_url)

        if status_callback:
            status_callback("crawling", f"Crawling {_domain}...", 5)

        # ── STEP 1: Crawl ──
        pages = firecrawl_crawl(website_url, firecrawl_key, status_callback=status_callback)

        def _is_thin(pl):
            if not pl: return True
            if all(len(p.get("markdown","")) < 500 for p in pl): return True
            if len([p for p in pl if len(p.get("markdown","")) > 500]) < 3: return True
            return False

        if _is_thin(pages):
            if status_callback:
                status_callback("crawling", f"Trying ScrapingBee for {_domain}...", 10)
            sb_pages = scrape_with_scrapingbee(website_url)
            if sb_pages and any(len(p.get("markdown","")) > 500 for p in sb_pages):
                pages = sb_pages
            elif _is_thin(pages):
                if status_callback:
                    status_callback("crawling", f"Trying direct fetch for {_domain}...", 15)
                direct_pages = direct_fetch(website_url)
                if direct_pages and any(len(p.get("markdown","")) > 500 for p in direct_pages):
                    pages = direct_pages
                elif _is_thin(pages):
                    if status_callback:
                        status_callback("crawling", f"Trying SerpAPI for {_domain}...", 20)
                    serp_pages = fetch_serpapi_site_results(website_url)
                    if serp_pages:
                        pages = serp_pages

        if not pages:
            return None, f"Could not extract content from {_domain}"

        if status_callback:
            status_callback("analysing", f"Analysing {_domain}...", 30)

        # ── STEP 2: AI analysis + enrichment IN PARALLEL ──
        raw_site_text = "\n".join((p.get("markdown", "") or "") for p in pages)
        corpus = build_corpus(pages)
        _quick_name = quick_extract_company_name(pages, _domain)

        # Start enrichment lookups immediately using quick name (don't wait for DeepSeek)
        lookup_jobs = {
            "company_registry": (lookup_companies_house, (_quick_name,), {"locations": []}),
            "linkedin": (lookup_linkedin_company, (_quick_name,), {"domain": _domain}),
            "reviews": (lookup_glassdoor, (_quick_name, _domain), {}),
            "planning": (lookup_planning_portal, (_quick_name,), {}),
        }

        with ThreadPoolExecutor(max_workers=6) as executor:
            # Submit enrichment lookups
            enrichment_futures = {
                executor.submit(func, *args, **kwargs): name
                for name, (func, args, kwargs) in lookup_jobs.items()
            }

            # Submit AI analysis in the SAME pool — runs concurrently with enrichment
            ai_future = executor.submit(analyze_company, corpus)

            # Collect enrichment results as they complete
            _lookup_results = {}
            for future in as_completed(enrichment_futures):
                name = enrichment_futures[future]
                try:
                    _lookup_results[name] = future.result()
                except:
                    _lookup_results[name] = ("", 0)

            # Get AI result (may already be done by now)
            company_raw = ai_future.result()

        _company_data = safe_json(company_raw)

        if status_callback:
            status_callback("enriching", f"Merging enrichment data...", 60)

        # ── STEP 3: Merge enrichment + targeted supplement ──
        _extra_corpus = ""

        ch_text, ch_dirs = _lookup_results.get("company_registry", ("", 0))
        if ch_text:
            _extra_corpus += f"\n\n[SOURCE: Company Registry]\n{ch_text}"

        _emp_from_structured = False

        li_text, li_emp = _lookup_results.get("linkedin", ("", ""))
        if li_text:
            _extra_corpus += f"\n\n[SOURCE: LinkedIn]\n{li_text}"
            if li_emp:
                _company_data["employee_count"] = li_emp
                _emp_from_structured = True

        gd_text, gd_rev = _lookup_results.get("reviews", ("", 0))
        gd_emp = extract_employee_count_from_text(gd_text)
        if gd_text:
            _extra_corpus += f"\n\n[SOURCE: Glassdoor & Indeed]\n{gd_text}"
        if gd_emp and not _emp_from_structured:
            _company_data["employee_count"] = gd_emp
            _emp_from_structured = True

        rejected_employee_count = ""

        # Sanity check: if the website only has a handful of people listed
        # but the employee count says 500+, it's almost certainly a wrong-company match.
        # Small engineering consultancies don't have 500+ staff with only 3-5 people on their site.
        if _emp_from_structured:
            people_on_site = len(_company_data.get("people", []))
            emp_str = _company_data.get("employee_count", "")
            first_num = employee_count_floor(emp_str)
            if people_on_site <= 10 and first_num >= 200:
                # Very likely wrong company — clear the count, let DeepSeek decide from site content
                rejected_employee_count = _company_data.get("employee_count", "")
                _company_data["employee_count"] = ""
                _emp_from_structured = False

        pp_text, pp_proj = _lookup_results.get("planning", ("", 0))
        if pp_text:
            _extra_corpus += f"\n\n[SOURCE: Planning Portal]\n{pp_text}"

        # People fallback via SerpAPI (only if website had none)
        if len(_company_data.get("people", [])) == 0:
            people_text = search_people_via_serpapi(
                _company_data.get("company_name", _quick_name), _domain
            )
            if people_text:
                _extra_corpus += f"\n\n[SOURCE: People Search]\n{people_text}"

        # Instead of full re-analysis, do a SMALL targeted supplement call
        # This is ~3x faster than running analyze_company again on the full corpus
        if _extra_corpus:
            existing_people = len(_company_data.get("people", []))
            existing_projects = len(_company_data.get("projects", []))

            # Only run supplement if there are actual gaps to fill
            needs_supplement = (existing_people < 3) or (existing_projects < 2)

            if needs_supplement:
                if status_callback:
                    status_callback("enriching", f"Extracting additional data...", 70)

                supplement_raw = analyze_supplement(
                    _extra_corpus, existing_people, existing_projects
                )
                supplement = safe_json(supplement_raw)

                # Merge supplement people (deduplicate by name)
                if supplement.get("people"):
                    existing_names = {p.get("name", "").lower() for p in _company_data.get("people", [])}
                    for p in supplement["people"]:
                        if p.get("name", "").lower() not in existing_names:
                            _company_data.setdefault("people", []).append(p)
                            existing_names.add(p["name"].lower())

                # Merge supplement projects (deduplicate by name)
                if supplement.get("projects"):
                    existing_proj_names = {p.get("name", "").lower() for p in _company_data.get("projects", [])}
                    for p in supplement["projects"]:
                        if p.get("name", "").lower() not in existing_proj_names:
                            _company_data.setdefault("projects", []).append(p)

                # Fill gaps only
                if supplement.get("locations") and not _company_data.get("locations", []):
                    _company_data["locations"] = supplement["locations"]
                if supplement.get("founded") and not _company_data.get("founded"):
                    _company_data["founded"] = supplement["founded"]
                if not _emp_from_structured and supplement.get("employee_count"):
                    _company_data["employee_count"] = supplement["employee_count"]

        if not _company_data.get("employee_count"):
            fb_emp = extract_employee_count_from_text(_extra_corpus)
            if fb_emp and fb_emp != rejected_employee_count:
                _company_data["employee_count"] = fb_emp
        final_emp_floor = employee_count_floor(_company_data.get("employee_count"))
        if len(_company_data.get("people", [])) <= 10 and final_emp_floor >= 200:
            _company_data["employee_count"] = ""

        _company_data["locations"] = clean_locations(_company_data.get("locations", []))
        site_locations = clean_locations(extract_locations_from_text(raw_site_text or corpus))
        if not site_locations:
            site_locations = clean_locations(extract_locations_from_text(direct_homepage_text(website_url)))
        if site_locations:
            _company_data["locations"] = site_locations
        else:
            _company_data["locations"] = clean_locations(_company_data.get("locations", []))
            if not _company_data.get("locations"):
                fallback_locations = clean_locations(extract_locations_from_text(_extra_corpus))
                if fallback_locations:
                    _company_data["locations"] = fallback_locations

        # ── STEP 4: Sales strategy ──
        if status_callback:
            status_callback("strategy", f"Building sales strategy...", 85)

        sales_raw = analyze_sales(corpus + _extra_corpus[:5000], json.dumps(_company_data))
        _sales_data = safe_json(sales_raw)

        if status_callback:
            status_callback("saving", f"Saving report...", 95)

        _entry = {
            "domain":       _domain,
            "company":      _company_data.get("company_name", website_url),
            "score":        _sales_data.get("overall_score", "Cold"),
            "lead_score":   _sales_data.get("lead_score", 0),
            "date":         now_gmt2().strftime("%d %b %Y %H:%M"),
            "pages_count":  len(pages),
            "company_data": _company_data,
            "sales_data":   _sales_data,
        }
        if should_save is None or should_save():
            save_history(_entry)

        if status_callback:
            status_callback("complete", "Done!", 100)

        return _entry, None

    except Exception as e:
        return None, f"Error processing {website_url}: {str(e)}"


# ── PDF EXPORT ───────────────────────────────────────────────────────────────

def export_pdf(company, cd, sd):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    from reportlab.lib.enums import TA_CENTER

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)

    INK    = colors.HexColor("#1a1a1a")
    MUTED  = colors.HexColor("#6b7280")
    LIGHT  = colors.HexColor("#f9fafb")
    BORDER = colors.HexColor("#e5e7eb")

    def style(name, **kw):
        base = dict(fontName="Helvetica", fontSize=10, textColor=INK, leading=16, spaceAfter=2)
        base.update(kw)
        return ParagraphStyle(name, **base)

    S_TITLE   = style("title",   fontName="Helvetica-Bold", fontSize=22, spaceAfter=10)
    S_SCORE   = style("score",   fontName="Helvetica-Bold", fontSize=11, textColor=MUTED, spaceAfter=4)
    S_META    = style("meta",    fontSize=9, textColor=MUTED, spaceAfter=8)
    S_SECTION = style("section", fontName="Helvetica-Bold", fontSize=9, textColor=INK, spaceBefore=14, spaceAfter=6)
    S_BODY    = style("body",    fontSize=10, leading=15, spaceAfter=4)
    S_BULLET  = style("bullet",  fontSize=10, leading=15, leftIndent=12, spaceAfter=3)
    S_LABEL   = style("label",   fontName="Helvetica-Bold", fontSize=9, textColor=MUTED, spaceAfter=2)
    S_ITALIC  = style("italic",  fontSize=10, leading=15, spaceAfter=4)

    story = []

    def section(title):
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(title.upper(), S_SECTION))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=4))

    def bullets(items):
        for item in items:
            story.append(Paragraph(f"→  {item}", S_BULLET))

    def label_value(label, value):
        story.append(Paragraph(label.upper(), S_LABEL))
        story.append(Paragraph(str(value), S_BODY))
        story.append(Spacer(1, 2*mm))

    score = sd.get("overall_score", "Warm")
    locs = ", ".join(cd.get("locations", [])) or "—"
    emp = cd.get("employee_count") or "—"
    conf = cd.get("confidence", "—")
    generated = now_gmt2().strftime("%d %b %Y %H:%M")

    story.append(Paragraph(company, S_TITLE))
    story.append(Paragraph(f"{score.upper()} LEAD", S_SCORE))
    story.append(Paragraph(f"Offices: {locs}  |  Employees: {emp}  |  Confidence: {conf}  |  Generated: {generated}", S_META))
    story.append(Paragraph(sd.get("score_reason", ""), S_BODY))
    story.append(HRFlowable(width="100%", thickness=1, color=INK, spaceAfter=6))

    section("Company Overview");          bullets(cd.get("overview", []))
    section("Engineering Capabilities");  bullets(cd.get("engineering_capabilities", []))

    projects = cd.get("projects", [])
    if projects:
        section("Delivered Projects")
        for proj in projects:
            name = proj.get("name", "Unknown")
            meta_parts = [p for p in [proj.get("type",""), proj.get("location",""),
                          f"Client: {proj['client']}" if proj.get("client") else "",
                          "FEM RELEVANT" if proj.get("fem_relevant") else ""] if p]
            story.append(Paragraph(f"<b>{name}</b>", S_BODY))
            if meta_parts:
                story.append(Paragraph("  ·  ".join(meta_parts), S_META))
            if proj.get("description"):
                story.append(Paragraph(proj["description"], S_BULLET))
            story.append(Spacer(1, 2*mm))

    sw = cd.get("software_mentioned", [])
    if sw:
        section("Software & Tools Detected")
        story.append(Paragraph("  ·  ".join(sw), S_BODY))
    else:
        section("Software & Tools")
        story.append(Paragraph("No competing software detected — clean FEA opportunity.", S_BODY))

    people = cd.get("people", [])
    if people:
        section("Key People")
        table_data = [["Name", "Role", "Tier"]]
        for p in people:
            table_data.append([p.get("name",""), p.get("role",""), p.get("tier","")])
        t = Table(table_data, colWidths=[55*mm, 80*mm, 30*mm])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,0), colors.HexColor("#f3f4f6")),
            ("FONTNAME",      (0,0),(-1,0), "Helvetica-Bold"),
            ("FONTSIZE",      (0,0),(-1,0), 8),
            ("FONTSIZE",      (0,1),(-1,-1), 9),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, LIGHT]),
            ("GRID",          (0,0),(-1,-1), 0.3, BORDER),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 6),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ]))
        story.append(t)

    section("FEM / FEA Opportunities");   bullets(sd.get("fem_opportunities", []))
    section("Key Sales Signals")
    for sig in sd.get("hiring_signals", []) + sd.get("expansion_signals", []):
        story.append(Paragraph(f"▲  {sig}", S_BULLET))

    section("Sales Strategy")
    label_value("Entry Point", sd.get("entry_point", "—"))
    label_value("Value Positioning", sd.get("value_positioning", "—"))

    objs = sd.get("likely_objections", [])
    if objs:
        story.append(Paragraph("LIKELY OBJECTIONS", S_LABEL))
        bullets(objs)

    section("Pre-Meeting Cheat Sheet")
    story.append(Paragraph("3 THINGS TO MENTION", S_LABEL))
    for m in sd.get("pre_meeting_mention", []):
        story.append(Paragraph(f"✓  {m}", S_BULLET))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("3 SMART QUESTIONS TO ASK", S_LABEL))
    for q in sd.get("smart_questions", []):
        story.append(Paragraph(f"?  {q}", S_BULLET))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("OPENING LINE", S_LABEL))
    story.append(Paragraph(sd.get("opening_line", "—"), S_ITALIC))

    roles = cd.get("open_roles", [])
    if roles:
        section("Open Vacancies")
        for role in roles:
            title = role.get("title", "Unknown")
            skills = ", ".join(role.get("skills", [])) or "—"
            fem = " · FEM MENTIONED" if role.get("fem_mentioned") else ""
            story.append(Paragraph(f"<b>{title}</b>{fem}", S_BODY))
            story.append(Paragraph(f"Skills: {skills}", S_META))

    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Paragraph(
        f"Generated by MIDAS Sales Intelligence  |  {generated}  |  Confidential",
        style("footer", fontSize=8, textColor=MUTED, alignment=TA_CENTER)
    ))
    doc.build(story)
    buffer.seek(0)
    return buffer.read()


# ── FIRECRAWL CREDITS ────────────────────────────────────────────────────────

def extract_credit_value(data):
    if isinstance(data, dict):
        for key in ("credits","remaining","remainingCredits","remaining_credits","availableCredits"):
            value = data.get(key)
            if value not in (None, ""):
                return value
        for value in data.values():
            found = extract_credit_value(value)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = extract_credit_value(item)
            if found not in (None, ""):
                return found
    return None


# ── API ROUTES ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "app": "MIDAS Pre Sales Intel API", "version": "1.0.0"}


@app.get("/api/history")
def get_history(search: Optional[str] = None):
    history = load_history()
    if search:
        search_lower = search.lower()
        history = [h for h in history if search_lower in h.get("company","").lower() or search_lower in h.get("domain","").lower()]
    # Add days_ago to each entry
    for h in history:
        h["days_ago"] = days_ago(h.get("date", ""))
    return {"history": history}


@app.get("/api/history/{domain}")
def get_report(domain: str):
    entry = find_in_history(domain)
    if not entry:
        raise HTTPException(status_code=404, detail="Report not found")
    entry["days_ago"] = days_ago(entry.get("date", ""))
    return entry


@app.delete("/api/history/{domain}")
def delete_report(domain: str):
    delete_from_history(domain)
    return {"status": "deleted", "domain": domain}


@app.get("/api/notes/{domain}")
def get_notes(domain: str):
    return get_note(domain)


@app.post("/api/notes")
def save_notes(note: NoteUpdate):
    save_note_db(note.domain, note.note)
    return {"status": "saved", "domain": note.domain}


@app.post("/api/email")
def generate_email(req: EmailRequest):
    email_text = generate_email_text(req.company_data, req.sales_data)
    return {"email": email_text}


@app.get("/api/credits")
def get_credits():
    if not FIRECRAWL_KEY:
        return {"credits": None}
    try:
        headers = {"Authorization": f"Bearer {FIRECRAWL_KEY}"}
        resp = http.get("https://api.firecrawl.dev/v1/team/credit-usage", headers=headers, timeout=10)
        if resp.status_code == 404:
            resp = http.get("https://api.firecrawl.dev/v2/team/credit-usage", headers=headers, timeout=10)
        if resp.status_code >= 400:
            return {"credits": None}
        return {"credits": extract_credit_value(resp.json())}
    except:
        return {"credits": None}


@app.get("/api/export/pdf/{domain}")
def export_pdf_route(domain: str):
    entry = find_in_history(domain)
    if not entry:
        raise HTTPException(status_code=404, detail="Report not found")

    cd = entry.get("company_data", {}) or {}
    sd = entry.get("sales_data", {}) or {}
    company = entry.get("company", domain)

    pdf_bytes = export_pdf(company, cd, sd)
    fname = f"MIDAS_Intel_{company.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


@app.get("/api/export/csv")
def export_csv_route():
    all_history = load_history()
    if not all_history:
        raise HTTPException(status_code=404, detail="No data to export")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Company","Domain","Lead Score","Score Label","Score Reason",
        "Structural Relevance (/30)","FEM Need (/25)","Buying Signals (/20)","Accessibility (/15)","Competitive Landscape (/10)",
        "Relevance Reason","FEM Need Reason","Buying Signals Reason","Accessibility Reason","Competitive Reason",
        "Date Analysed","Pages Crawled",
        "Locations","Employee Count","Founded","Confidence","Confidence Reason",
        "Engineering Capabilities","Project Types","Software Mentioned",
        "FEM Opportunities","Pain Points","Entry Point","Value Positioning",
        "Likely Objections","Hiring Signals","Expansion Signals",
        "Recommended Products","Product Reason","Opening Line",
        "Pre-Meeting Mentions","Smart Questions","People Count","Projects Count","Open Roles Count"
    ])
    for h in all_history:
        cd = h.get("company_data", {}) or {}
        sd = h.get("sales_data", {}) or {}
        sb = sd.get("score_breakdown", {}) or {}
        # Handle both old format (number) and new format ({score, reason})
        def sb_score(key):
            val = sb.get(key, "")
            if isinstance(val, dict):
                return val.get("score", "")
            return val
        def sb_reason(key):
            val = sb.get(key, "")
            if isinstance(val, dict):
                return val.get("reason", "")
            return ""
        writer.writerow([
            h.get("company",""), h.get("domain",""),
            sd.get("lead_score", h.get("lead_score", "")),
            h.get("score",""),
            sd.get("score_reason",""),
            sb_score("structural_relevance"), sb_score("fem_need"), sb_score("buying_signals"), sb_score("accessibility"), sb_score("competitive_landscape"),
            sb_reason("structural_relevance"), sb_reason("fem_need"), sb_reason("buying_signals"), sb_reason("accessibility"), sb_reason("competitive_landscape"),
            h.get("date",""), h.get("pages_count",""),
            " | ".join(cd.get("locations",[])), cd.get("employee_count",""),
            cd.get("founded",""), cd.get("confidence",""), cd.get("confidence_reason",""),
            " | ".join(cd.get("engineering_capabilities",[])),
            " | ".join(cd.get("project_types",[])),
            " | ".join(cd.get("software_mentioned",[])),
            " | ".join(sd.get("fem_opportunities",[])),
            " | ".join(sd.get("pain_points",[])),
            sd.get("entry_point",""), sd.get("value_positioning",""),
            " | ".join(sd.get("likely_objections",[])),
            " | ".join(sd.get("hiring_signals",[])),
            " | ".join(sd.get("expansion_signals",[])),
            " | ".join(sd.get("recommended_products",[])),
            sd.get("product_reason",""), sd.get("opening_line",""),
            " | ".join(sd.get("pre_meeting_mention",[])),
            " | ".join(sd.get("smart_questions",[])),
            len(cd.get("people",[])), len(cd.get("projects",[])), len(cd.get("open_roles",[]))
        ])

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="MIDAS_Intel_All_{datetime.now().strftime("%Y%m%d")}.csv"'}
    )


# ── IN-MEMORY JOB TRACKER ────────────────────────────────────────────────────
# Tracks progress of running analysis jobs so the frontend can poll instead of WS

from threading import Lock

_jobs = {}  # domain -> {job_id, status, stage, message, progress, result, error}
_jobs_lock = Lock()

def _update_job(domain, job_id=None, **kwargs):
    with _jobs_lock:
        if domain not in _jobs:
            _jobs[domain] = {}
        current_job_id = _jobs[domain].get("job_id")
        if job_id is not None and current_job_id is not None and current_job_id != job_id:
            return False
        _jobs[domain].update(kwargs)
        return True

def _start_job(domain, job_id, **kwargs):
    with _jobs_lock:
        _jobs[domain] = {"job_id": job_id, **kwargs}

def _get_job(domain):
    with _jobs_lock:
        return _jobs.get(domain, {}).copy()

def _clear_job(domain):
    with _jobs_lock:
        _jobs.pop(domain, None)


@app.post("/api/analyse")
def start_analysis(req: AnalyseRequest):
    """Start analysis as a background job. Returns immediately.
    Frontend polls GET /api/jobs/{domain} for progress, then GET /api/history/{domain} for results."""
    url = req.url
    if not url.startswith("http"):
        url = "https://" + url
    domain = extract_domain(url)

    delete_from_history(domain)

    job_id = uuid.uuid4().hex
    _start_job(domain, job_id, status="running", stage="starting", message="Starting...", progress=0, result=None, error=None)

    def run_in_background():
        def status_callback(stage, message, progress):
            _update_job(domain, job_id=job_id, stage=stage, message=message, progress=progress)

        def is_current_job():
            return _get_job(domain).get("job_id") == job_id

        entry, err = analyse_single_url(url, FIRECRAWL_KEY, status_callback=status_callback, should_save=is_current_job)
        if not is_current_job():
            return
        if entry:
            entry["job_id"] = job_id
            _update_job(domain, job_id=job_id, status="complete", progress=100, message="Done!", result=entry, error=None)
        else:
            _update_job(domain, job_id=job_id, status="error", message=err or "Unknown error", error=err)

    import threading
    t = threading.Thread(target=run_in_background, daemon=True)
    t.start()

    return {"status": "started", "domain": domain, "job_id": job_id}


@app.get("/api/jobs/{domain}")
def get_job_status(domain: str):
    """Poll this endpoint for analysis progress. Returns the job result directly when complete."""
    job = _get_job(domain)
    if not job:
        return {"status": "not_found", "domain": domain}
    # When complete, include the full result so frontend doesn't need to fetch from history
    return {"domain": domain, **job}


# ── WEBSOCKET: SINGLE ANALYSIS WITH PROGRESS (kept for backward compat) ───

@app.websocket("/ws/analyse")
async def ws_analyse(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        url = data.get("url", "")

        if not url:
            await websocket.send_json({"type": "error", "message": "Missing url"})
            await websocket.close()
            return
        if not url.startswith("http"):
            url = "https://" + url
        delete_from_history(extract_domain(url))

        def status_callback(stage, message, progress):
            status_queue.append({"type": "progress", "stage": stage, "message": message, "progress": progress})

        status_queue = []

        loop = asyncio.get_event_loop()

        def run_analysis():
            return analyse_single_url(url, FIRECRAWL_KEY, status_callback=status_callback)

        # Start analysis in background
        future = loop.run_in_executor(None, run_analysis)

        # Send progress updates while waiting
        while not future.done():
            while status_queue:
                msg = status_queue.pop(0)
                await websocket.send_json(msg)
            await asyncio.sleep(0.5)

        # Send remaining progress messages
        while status_queue:
            msg = status_queue.pop(0)
            await websocket.send_json(msg)

        entry, error = future.result()
        if entry:
            await websocket.send_json({"type": "complete", "data": entry})
        else:
            await websocket.send_json({"type": "error", "message": error})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass


# ── WEBSOCKET: BATCH ANALYSIS ───────────────────────────────────────────────

@app.websocket("/ws/batch")
async def ws_batch(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        urls = data.get("urls", [])
        recrawl = data.get("recrawl", False)

        if not urls:
            await websocket.send_json({"type": "error", "message": "Missing urls"})
            await websocket.close()
            return

        # Deduplicate
        seen = set()
        unique_urls = []
        for u in urls:
            url = u if u.startswith("http") else "https://" + u
            d = extract_domain(url)
            if d and d not in seen:
                seen.add(d)
                unique_urls.append(url)

        total = len(unique_urls)
        completed = 0
        succeeded = 0
        skipped = 0
        failed = 0

        await websocket.send_json({"type": "batch_start", "total": total})

        for idx, url in enumerate(unique_urls):
            d = extract_domain(url)

            try:
                if not recrawl:
                    existing = find_in_history(d)
                    if existing:
                        skipped += 1
                        completed += 1
                        await websocket.send_json({
                            "type": "batch_item",
                            "index": idx, "domain": d,
                            "company": existing.get("company", d),
                            "status": "skipped",
                            "score": existing.get("score", "—"),
                            "progress": completed / total * 100
                        })
                        continue
                else:
                    delete_from_history(d)

                status_queue = []

                def make_callback(sq, _idx=idx, _d=d):
                    def status_callback(stage, message, progress):
                        sq.append({"type": "batch_progress", "index": _idx, "domain": _d, "stage": stage, "message": message, "item_progress": progress})
                    return status_callback

                loop = asyncio.get_event_loop()
                callback = make_callback(status_queue)
                future = loop.run_in_executor(None, lambda u=url, cb=callback: analyse_single_url(u, FIRECRAWL_KEY, status_callback=cb))

                while not future.done():
                    # Send queued progress messages
                    while status_queue:
                        await websocket.send_json(status_queue.pop(0))
                    # Send heartbeat ping to keep connection alive
                    await websocket.send_json({"type": "heartbeat"})
                    await asyncio.sleep(1)

                # Flush remaining progress messages
                while status_queue:
                    await websocket.send_json(status_queue.pop(0))

                entry, error = future.result()
                completed += 1

                if entry:
                    succeeded += 1
                    await websocket.send_json({
                        "type": "batch_item",
                        "index": idx, "domain": d,
                        "company": entry.get("company", d),
                        "status": "done",
                        "score": entry.get("score", "Cold"),
                        "progress": completed / total * 100
                    })
                else:
                    failed += 1
                    await websocket.send_json({
                        "type": "batch_item",
                        "index": idx, "domain": d,
                        "company": d,
                        "status": "failed",
                        "error": error,
                        "progress": completed / total * 100
                    })

            except Exception as company_err:
                # Single company failure shouldn't kill the entire batch
                failed += 1
                completed += 1
                try:
                    await websocket.send_json({
                        "type": "batch_item",
                        "index": idx, "domain": d,
                        "company": d,
                        "status": "failed",
                        "error": str(company_err),
                        "progress": completed / total * 100
                    })
                except:
                    break  # WS is dead, stop the batch

        await websocket.send_json({
            "type": "batch_complete",
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
            "total": total
        })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass
