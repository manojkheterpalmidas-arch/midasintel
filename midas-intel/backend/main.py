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
  "projects": [{{"name": "Project name", "type": "Bridge|Building|Metro|Infrastructure|Residential|Industrial|Other", "location": "City or null", "client": "Client name or null", "description": "One sentence summary", "fem_relevant": true}}],
  "confidence": "High|Medium|Low",
  "confidence_reason": "One sentence explaining why"
}}
Extract ALL people — engineering and technical staff only.
For locations: include any city mentioned as company headquarters, office, or base location — check footer addresses, contact pages, and about sections.
For employee_count: check ALL sources including Glassdoor, LinkedIn, Companies House.
For projects: extract ALL completed or ongoing projects mentioned anywhere.
Website content:
{corpus}""",
        max_tokens=8000
    )


MIDAS_PRODUCTS = """
MIDAS NX PRODUCT SUITE — FULL SALES KNOWLEDGE BASE

1. MIDAS CIVIL NX — Bridges & Civil Infrastructure
   Structural analysis and design for bridges and civil infrastructure.

2. MIDAS GEN NX — Building & General Structures
   Building structural design with automated workflows.

3. MIDAS FEA NX — Advanced Nonlinear Analysis
   Detailed local analysis — connections, joints, bearings, anchorages.

4. MIDAS GTS NX — Geotechnical Analysis
   Soil, rock, underground engineering — tunnels, foundations, slopes.

CROSS-SELL LOGIC:
- Bridge/infrastructure → CIVIL NX + FEA NX
- Building/structural → GEN NX + FEA NX
- Geotechnical/ground → GTS NX + CIVIL NX
- Mixed civil → CIVIL NX + GEN NX + FEA NX
- Full service → Full suite
- Metro/tunnelling → GTS NX + CIVIL NX

COMPETITIVE POSITIONING:
- vs LUSAS/STAAD/SAP2000 → Better automation, modern UI
- vs PLAXIS → Better BIM integration
- vs ETABS → More automation and parametric design
- vs ANSYS/ABAQUS → More accessible for civil engineers
"""


def analyze_sales(corpus, company_json):
    return ask_deepseek(
        f"You are a senior B2B sales strategist for MIDAS IT. Use the product knowledge below. Be specific and actionable. Respond in pure JSON, no markdown. Always respond in English.\n\n{MIDAS_PRODUCTS}",
        f"""Return ONLY valid JSON:
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
  "overall_score": "Hot|Warm|Cold",
  "score_reason": "2-3 sentence reason for the score",
  "recommended_products": ["CIVIL NX", "GEN NX", "FEA NX", "GTS NX"],
  "product_reason": "3-4 sentence explanation of why these products fit"
}}
Company data: {company_json}
Website excerpt: {corpus[:4000]}""",
        max_tokens=4000
    )


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
  "projects": [{{"name": "Project name", "type": "Bridge|Building|Metro|Infrastructure|Residential|Industrial|Other", "location": "City or null", "client": "Client name or null", "description": "One sentence summary", "fem_relevant": true}}],
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

def analyse_single_url(website_url, firecrawl_key, status_callback=None):
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

        # Sanity check: if the website only has a handful of people listed
        # but the employee count says 500+, it's almost certainly a wrong-company match.
        # Small engineering consultancies don't have 500+ staff with only 3-5 people on their site.
        if _emp_from_structured:
            people_on_site = len(_company_data.get("people", []))
            emp_str = _company_data.get("employee_count", "")
            # Extract the first number from the employee string for comparison
            emp_nums = re.findall(r'[\d,]+', emp_str.replace(",", ""))
            first_num = int(emp_nums[0]) if emp_nums else 0
            if people_on_site <= 10 and first_num >= 200:
                # Very likely wrong company — clear the count, let DeepSeek decide from site content
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
            if fb_emp:
                _company_data["employee_count"] = fb_emp

        # ── STEP 4: Sales strategy ──
        if status_callback:
            status_callback("strategy", f"Building sales strategy...", 85)

        sales_raw = analyze_sales(corpus + _extra_corpus[:5000], company_raw)
        _sales_data = safe_json(sales_raw)

        if status_callback:
            status_callback("saving", f"Saving report...", 95)

        _entry = {
            "domain":       _domain,
            "company":      _company_data.get("company_name", website_url),
            "score":        _sales_data.get("overall_score", "Cold"),
            "date":         now_gmt2().strftime("%d %b %Y %H:%M"),
            "pages_count":  len(pages),
            "company_data": _company_data,
            "sales_data":   _sales_data,
        }
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
        "Company","Domain","Score","Score Reason","Date Analysed","Pages Crawled",
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
        writer.writerow([
            h.get("company",""), h.get("domain",""), h.get("score",""),
            sd.get("score_reason",""), h.get("date",""), h.get("pages_count",""),
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

_jobs = {}  # domain -> {status, stage, message, progress, result, error}
_jobs_lock = Lock()

def _update_job(domain, **kwargs):
    with _jobs_lock:
        if domain not in _jobs:
            _jobs[domain] = {}
        _jobs[domain].update(kwargs)

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

    # Check if already running
    existing_job = _get_job(domain)
    if existing_job.get("status") == "running":
        return {"status": "already_running", "domain": domain}

    _update_job(domain, status="running", stage="starting", message="Starting...", progress=0, result=None, error=None)

    def run_in_background():
        def status_callback(stage, message, progress):
            _update_job(domain, stage=stage, message=message, progress=progress)

        entry, err = analyse_single_url(url, FIRECRAWL_KEY, status_callback=status_callback)
        if entry:
            _update_job(domain, status="complete", progress=100, message="Done!", result=entry, error=None)
        else:
            _update_job(domain, status="error", message=err or "Unknown error", error=err)

    import threading
    t = threading.Thread(target=run_in_background, daemon=True)
    t.start()

    return {"status": "started", "domain": domain}


@app.get("/api/jobs/{domain}")
def get_job_status(domain: str):
    """Poll this endpoint for analysis progress."""
    job = _get_job(domain)
    if not job:
        # No active job — check if result already exists in history
        existing = find_in_history(domain)
        if existing:
            return {"status": "complete", "domain": domain, "progress": 100}
        return {"status": "not_found", "domain": domain}
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
