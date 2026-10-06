"""
job_fetcher.py
Fetches real remote QA opportunities from multiple free public sources:
- Freelance Platforms: Freelancer.com active projects (Fixed-Price gigs, Hourly contracts)
- Remote Tech APIs: Remotive, Jobicy, RemoteOK, Himalayas, Arbeitnow
- India Tech Boards: Hasjob (Hasgeek Atom feed)
- Curated RSS Feeds: We Work Remotely, Custom RSS / Atom feeds
Pre-filters for QA/testing relevance and manages deduplication history.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

from job_store import JobHistoryManager, fingerprint_of  # noqa: E402  (re-exported for main.py)

# Ensure UTF-8 output on Windows console to avoid charmap errors with currencies (₹, €, etc.)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) qaJobHunterBot/1.0"

PROHIBITED_TITLE_PATTERNS = [
    r"\bfull[- ]?stack\b",
    r"\bfrontend\b",
    r"\bbackend\b",
    r"\bindependent developer\b",
    r"\bdata scientist\b",
    r"\bdata analyst\b",
    r"\bdata annotator\b",
    r"\bvideo annotator\b",
    r"\bannotator\b",
    r"\bengineering manager\b",
    r"\bhead of engineering\b",
    r"\bvp of engineering\b",
    r"\bdirector of engineering\b",
    r"\btechnical lead\b",
    r"\btech lead\b",
    r"\bteam lead\b",
    r"\bdevops\b",
    r"\bsecops\b",
    r"\bsecurity operations\b",
    r"\bcloud engineer\b",
    r"\bsystems engineer\b",
    r"\bsysadmin\b",
    r"\binfrastructure\b",
    r"\bsolution(s)? architect\b",
    r"\bmarketing\b",
    r"\bsales\b",
    r"\brecruiter\b",
    r"\bdesigner\b",
    r"\bcopywriter\b",
    r"\baccountant\b",
    r"\bfinancial\b",
    r"\bhr manager\b",
    r"\bscrum master\b",
    r"\bproduct owner\b",
    r"\bproduct manager\b",
    r"\bshopify\b",
    r"\bgolang developer\b",
    r"\bphp developer\b",
    r"\bai engineer\b",
]


QA_TITLE_PATTERNS = [
    r"\bqa automation\b",
    r"\bsdet\b",
    r"\bsoftware (development )?engineer in test\b",
    r"\bsoftware developer in test\b",
    r"\btest automation engineer\b",
    r"\bqa analyst\b",
    r"\bqa engineer\b",
    r"\bqa\b",
    r"\bquality assurance\b",
    r"\btester\b",
    r"\btest engineer\b",
    r"\btesting\b",
    r"\bautomation engineer\b",
    r"\bmanual tester\b",
    r"\btest analyst\b",
    r"\bqa specialist\b",
    r"\bsoftware tester\b",
    r"\bplaywright\b",
    r"\bcypress\b",
    r"\bselenium\b",
    r"\bappium\b",
    r"\bpruebas\b",
]

QA_STRONG_DESC_PATTERNS = [
    r"\bplaywright\b",
    r"\bcypress\b",
    r"\bselenium\b",
    r"\bappium\b",
    r"\btest automation\b",
    r"\bautomated testing\b",
    r"\bautomated tests\b",
    r"\bqa engineer\b",
    r"\bqa tester\b",
    r"\bquality assurance\b",
    r"\bmanual testing\b",
    r"\bexploratory testing\b",
    r"\btest cases\b",
    r"\bregression testing\b",
    r"\bsdet\b",
    r"\bpostman\b",
    r"\bbdd\b",
    r"\bgherkin\b",
    r"\btestrail\b",
    r"\bxray\b",
    r"\bjira\b",
    r"\bci/cd\b",
    r"\bcontinuous integration\b",
    r"\bapi testing\b",
    r"\bistqb\b",
]


def clean_html(text: str) -> str:
    """Strips HTML tags and normalizes whitespace."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def detect_india_region(text: str) -> bool:
    """Detects whether a job or project is based in India / APAC or denominated in INR."""
    patterns = [
        r"\bindia\b",
        r"\binr\b",
        r"₹",
        r"\bbangalore\b",
        r"\bbengaluru\b",
        r"\bhyderabad\b",
        r"\bpune\b",
        r"\bmumbai\b",
        r"\bgurgaon\b",
        r"\bnoida\b",
        r"\bdelhi\b",
        r"\bchennai\b",
        r"\bkolkata\b",
        r"\bapac\b",
    ]
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def is_qa_related(title: str, description: str, tags: Optional[list[str]] = None) -> bool:
    """
    Strictly checks whether the job is specifically a QA/Testing/SDET role.
    Enforces user validation rules:
    - Software Developer / Software Engineer is ONLY allowed if followed by 'in Test'.
    - Discards Full Stack, Frontend, Backend, Independent Developer, Management.
    """
    title_lower = title.lower()
    desc_lower = description.lower()
    combined_tags = " ".join(tags or []).lower()

    # Rule: If title contains "Software Developer" or "Software Engineer",
    # it can ONLY be approved if followed by "in test" (or "in testing", "sdet", "qa", "quality").
    has_sw_dev_or_eng = bool(re.search(r"\bsoftware developer\b|\bsoftware engineer\b", title_lower))
    if has_sw_dev_or_eng:
        has_in_test = bool(re.search(
            r"\b(software (development )?engineer in test|software developer in test|sdet|qa|quality)\b",
            title_lower
        ))
        if not has_in_test:
            return False

    # Discard prohibited titles unless there's an explicit "in test" / "sdet"
    for pat in PROHIBITED_TITLE_PATTERNS:
        if re.search(pat, title_lower) and not re.search(r"\b(in test|sdet|qa|tester)\b", title_lower):
            return False

    # Check for prohibited role scopes in description (e.g. building products 0 to 1 from scratch)
    if re.search(r"\b(build.*from scratch|from 0 to 1|0 to 1 product)\b", desc_lower):
        if not any(re.search(pat, title_lower) for pat in [r"\bqa\b", r"\bsdet\b", r"\btest", r"\bquality\b"]):
            return False

    # Explicit QA title match
    has_explicit_qa_in_title = any(re.search(pat, title_lower) for pat in QA_TITLE_PATTERNS)
    if has_explicit_qa_in_title:
        return True

    # If title contains engineer or developer, it MUST have matched a QA/test pattern above
    if re.search(r"\b(engineer|developer)\b", title_lower):
        return False

    # Tag match
    if any(re.search(pat, combined_tags) for pat in QA_TITLE_PATTERNS):
        return True


    # For ambiguous or freelance project titles, require strong QA indicators in description
    matches = sum(1 for pat in QA_STRONG_DESC_PATTERNS if re.search(pat, desc_lower))
    return matches >= 2



@dataclass
class JobOpportunity:
    """Represents a standardized job post found on the web."""

    id: str
    title: str
    company: str
    url: str
    description: str
    source: str
    salary: str = ""
    location: str = "Remote"
    published_at: str = ""
    job_type: str = "Freelance / Contract"  # e.g., "Fixed-Price Gig", "Hourly Freelance Contract", "Full-Time Remote"
    is_india: bool = False

    def to_evaluation_text(self) -> str:
        """Formats the job opportunity into a plain-text prompt for Gemini evaluation."""
        region_note = "India / South Asia (APAC)" if self.is_india else "Global / USA / International"
        lines = [
            f"Job Title: {self.title}",
            f"Company / Client: {self.company}",
            f"Source Platform: {self.source}",
            f"Arrangement / Modality: {self.job_type}",
            f"Location / Market: {self.location} ({region_note})",
            f"Rate / Budget: {self.salary or 'Not specified (negotiable freelance rate)'}",
            f"Apply URL: {self.url}",
            f"Published: {self.published_at or 'Recent'}",
            "",
            "Job Description / Scope:",
            self.description[:4000],  # Limit to 4k characters to keep prompts efficient
        ]
        return "\n".join(lines)



def _generate_job_id(url: str, title: str) -> str:
    """Generates a stable unique hash identifier for a job post."""
    key = f"{url.strip().lower()}|{title.strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def fetch_remotive_jobs(search_query: str = "qa") -> list[JobOpportunity]:
    """Fetches remote QA jobs from Remotive API."""
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}

    urls = [
        f"https://remotive.com/api/remote-jobs?search={search_query}",
        "https://remotive.com/api/remote-jobs?category=qa",
    ]

    seen_ids = set()

    for endpoint in urls:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                jid_raw = str(item.get("id") or item.get("url"))
                if jid_raw in seen_ids:
                    continue
                seen_ids.add(jid_raw)

                title = str(item.get("title") or "").strip()
                company = str(item.get("company_name") or "Remote Client").strip()
                url = str(item.get("url") or "").strip()
                desc_html = str(item.get("description") or "")
                desc_clean = clean_html(desc_html)
                tags = item.get("tags") or []
                salary = str(item.get("salary") or "").strip()
                location = str(item.get("candidate_required_location") or "Worldwide Remote").strip()
                published = str(item.get("publication_date") or "").strip()
                job_type_raw = str(item.get("job_type") or "contract").replace("_", " ").title()

                if not is_qa_related(title, desc_clean, tags):
                    continue

                is_india = detect_india_region(f"{title} {location} {desc_clean}")
                job_id = f"remotive_{item.get('id')}" if item.get("id") else _generate_job_id(url, title)

                jobs.append(
                    JobOpportunity(
                        id=job_id,
                        title=title,
                        company=company,
                        url=url,
                        description=desc_clean,
                        source="Remotive",
                        salary=salary,
                        location=location,
                        published_at=published,
                        job_type=f"{job_type_raw} Remote",
                        is_india=is_india,
                    )
                )
        except Exception as exc:
            print(f"[WARN] Error fetching from Remotive ({endpoint}): {exc}", file=sys.stderr)

    return jobs


def fetch_weworkremotely_jobs() -> list[JobOpportunity]:
    """Fetches and parses QA-relevant jobs from We Work Remotely public RSS feeds."""
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}

    feed_urls = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    ]

    for feed_url in feed_urls:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items:
                title_elem = item.find("title")
                link_elem = item.find("link")
                desc_elem = item.find("description")
                pub_elem = item.find("pubDate")

                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                url = link_elem.text.strip() if link_elem is not None and link_elem.text else ""
                desc_raw = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""
                published = pub_elem.text.strip() if pub_elem is not None and pub_elem.text else ""

                desc_clean = clean_html(desc_raw)

                # WWR titles are usually "Company Name: Job Title"
                company = "Remote Client"
                if ":" in title:
                    parts = title.split(":", 1)
                    company = parts[0].strip()
                    job_title = parts[1].strip()
                else:
                    job_title = title

                if not is_qa_related(job_title, desc_clean):
                    continue

                is_india = detect_india_region(f"{job_title} {desc_clean}")
                job_id = _generate_job_id(url, job_title)

                jobs.append(
                    JobOpportunity(
                        id=job_id,
                        title=job_title,
                        company=company,
                        url=url,
                        description=desc_clean,
                        source="WeWorkRemotely",
                        salary="",
                        location="Remote",
                        published_at=published,
                        job_type="Remote Contract",
                        is_india=is_india,
                    )
                )
        except Exception as exc:
            print(f"[WARN] Error fetching WeWorkRemotely RSS ({feed_url}): {exc}", file=sys.stderr)

    return jobs


def fetch_arbeitnow_jobs() -> list[JobOpportunity]:
    """Fetches remote tech jobs from Arbeitnow API and filters for QA."""
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}
    endpoint = "https://www.arbeitnow.com/api/job-board-api"

    try:
        resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            raw_jobs = data.get("data", [])

            for item in raw_jobs:
                title = str(item.get("title") or "").strip()
                company = str(item.get("company_name") or "Remote Client").strip()
                url = str(item.get("url") or "").strip()
                desc_clean = clean_html(str(item.get("description") or ""))
                tags = item.get("tags") or []
                location = str(item.get("location") or "Remote").strip()

                if not item.get("remote", True):
                    continue

                if not is_qa_related(title, desc_clean, tags):
                    continue

                is_india = detect_india_region(f"{title} {location} {desc_clean}")
                job_id = f"arbeitnow_{item['slug']}" if item.get("slug") else _generate_job_id(url, title)

                jobs.append(
                    JobOpportunity(
                        id=job_id,
                        title=title,
                        company=company,
                        url=url,
                        description=desc_clean,
                        source="Arbeitnow",
                        salary="",
                        location=location,
                        job_type="Remote Contract",
                        is_india=is_india,
                    )
                )
    except Exception as exc:
        print(f"[WARN] Error fetching Arbeitnow API: {exc}", file=sys.stderr)

    return jobs


def fetch_freelancer_jobs(limit: int = 20) -> list[JobOpportunity]:
    """
    Fetches active freelance QA/testing projects from Freelancer.com API.
    Targets project-based (fixed price) gigs and hourly freelance contracts.
    Covers global clients as well as Indian tech clients with INR/USD budgets.
    """
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}

    # QA / Testing skill IDs on Freelancer:
    # 67: Testing / QA, 167: Software Testing, 208: Test Automation, 317: Usability Testing,
    # 1112: Selenium Webdriver, 1679: Selenium, 3397: AI Quality Assurance
    skill_params = "jobs[]=67&jobs[]=167&jobs[]=208&jobs[]=317&jobs[]=1112&jobs[]=1679&jobs[]=3397"
    endpoint = f"https://www.freelancer.com/api/projects/0.1/projects/active/?{skill_params}&full_description=true&compact=false&limit={limit * 2}"

    try:
        resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"[WARN] Freelancer API returned HTTP {resp.status_code}", file=sys.stderr)
            return jobs

        data = resp.json()
        projects = data.get("result", {}).get("projects", [])

        for item in projects:
            title = str(item.get("title") or "").strip()
            desc_raw = str(item.get("description") or item.get("preview_description") or "")
            desc_clean = clean_html(desc_raw)

            if not is_qa_related(title, desc_clean):
                continue

            pid = str(item.get("id"))
            seo_url = item.get("seo_url")
            url = f"https://www.freelancer.com/projects/{seo_url}" if seo_url else f"https://www.freelancer.com/projects/{pid}"

            curr_info = item.get("currency") or {}
            currency_code = curr_info.get("code", "USD")
            currency_sign = curr_info.get("sign", "$")

            budget = item.get("budget") or {}
            b_min = budget.get("minimum", 0)
            b_max = budget.get("maximum", 0)
            ptype = item.get("type", "fixed")

            if ptype == "hourly":
                salary = f"{currency_sign}{b_min:g} - {currency_sign}{b_max:g} {currency_code}/hr (Contrato Freelance por Hora)"
                job_type = "Contrato Freelance por Hora (Sin relación de dependencia)"
            else:
                salary = f"{currency_sign}{b_min:g} - {currency_sign}{b_max:g} {currency_code} (Pago por Proyecto / Fixed Price)"
                job_type = "Pago por Proyecto / Fixed-Price Gig (Sin relación de dependencia)"

            # Detect India region from currency (INR) or text
            is_india = currency_code == "INR" or detect_india_region(f"{title} {desc_clean} {str(item.get('location'))}")
            location = "India / Remoto" if is_india else "Global Freelance (Remoto)"

            # Timestamp
            submit_ts = item.get("submitdate")
            pub_date = datetime.fromtimestamp(submit_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if submit_ts else "Reciente"

            jobs.append(
                JobOpportunity(
                    id=f"freelancer_{pid}",
                    title=title,
                    company="Cliente Freelance",
                    url=url,
                    description=desc_clean,
                    source="Freelancer.com",
                    salary=salary,
                    location=location,
                    published_at=pub_date,
                    job_type=job_type,
                    is_india=is_india,
                )
            )

            if len(jobs) >= limit:
                break

    except Exception as exc:
        print(f"[WARN] Error fetching Freelancer API: {exc}", file=sys.stderr)

    return jobs


def fetch_jobicy_jobs(limit: int = 20) -> list[JobOpportunity]:
    """
    Fetches remote QA and testing opportunities from Jobicy API.
    Supports global, APAC, and remote contract/freelance listings.
    """
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}

    # Query dedicated QA & Testing industry slug
    endpoints = [
        "https://jobicy.com/api/v2/remote-jobs?count=30&industry=qa-testing",
        "https://jobicy.com/api/v2/remote-jobs?count=20&geo=apac",
    ]

    seen_urls = set()

    for endpoint in endpoints:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            data = resp.json()
            raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                url = str(item.get("url") or "").strip()
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title = str(item.get("jobTitle") or "").strip()
                company = str(item.get("companyName") or "Remote Client").strip()
                desc_clean = clean_html(str(item.get("jobDescription") or ""))
                location = str(item.get("jobGeo") or "Anywhere Remote").strip()
                job_types = item.get("jobType") or []
                job_type_str = ", ".join(job_types) if isinstance(job_types, list) else str(job_types)

                if not is_qa_related(title, desc_clean):
                    continue

                min_sal = item.get("annualSalaryMin")
                max_sal = item.get("annualSalaryMax")
                curr = item.get("salaryCurrency", "USD")
                if min_sal and max_sal:
                    salary = f"${min_sal:,} - ${max_sal:,} {curr}/año"
                elif min_sal:
                    salary = f"Desde ${min_sal:,} {curr}/año"
                else:
                    salary = ""

                is_india = detect_india_region(f"{title} {location} {desc_clean}")
                jid = str(item.get("id") or _generate_job_id(url, title))

                jobs.append(
                    JobOpportunity(
                        id=f"jobicy_{jid}",
                        title=title,
                        company=company,
                        url=url,
                        description=desc_clean,
                        source="Jobicy",
                        salary=salary,
                        location=location,
                        published_at=str(item.get("pubDate") or "Reciente"),
                        job_type=f"{job_type_str} Remote" if job_type_str else "Remote Contract",
                        is_india=is_india,
                    )
                )

                if len(jobs) >= limit:
                    break

        except Exception as exc:
            print(f"[WARN] Error fetching Jobicy API ({endpoint}): {exc}", file=sys.stderr)

    return jobs


def fetch_remoteok_jobs(limit: int = 15) -> list[JobOpportunity]:
    """
    Fetches remote jobs from RemoteOK API and filters for QA/testing roles.
    Includes contract and worldwide remote opportunities.
    """
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}
    endpoint = "https://remoteok.com/api"

    try:
        resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            # First item in RemoteOK is a legal disclaimer dict
            raw_jobs = [x for x in data if isinstance(x, dict) and "position" in x]

            for item in raw_jobs:
                title = str(item.get("position") or "").strip()
                company = str(item.get("company") or "Remote Client").strip()
                url = str(item.get("url") or item.get("apply_url") or "").strip()
                desc_clean = clean_html(str(item.get("description") or ""))
                tags = item.get("tags") or []
                location = str(item.get("location") or "Worldwide Remote").strip()

                if not is_qa_related(title, desc_clean, tags):
                    continue

                min_sal = item.get("salary_min")
                max_sal = item.get("salary_max")
                salary = f"${min_sal:,} - ${max_sal:,} USD/año" if min_sal and max_sal else ""

                # Determine contract or freelance
                is_contract = any("contract" in t.lower() or "freelance" in t.lower() for t in tags)
                job_type = "Freelance / Contract Remote" if is_contract else "Remote Full-Time / Contract"
                is_india = detect_india_region(f"{title} {location} {desc_clean}")

                jid = str(item.get("id") or _generate_job_id(url, title))

                jobs.append(
                    JobOpportunity(
                        id=f"remoteok_{jid}",
                        title=title,
                        company=company,
                        url=url,
                        description=desc_clean,
                        source="RemoteOK",
                        salary=salary,
                        location=location,
                        published_at=str(item.get("date") or "Reciente"),
                        job_type=job_type,
                        is_india=is_india,
                    )
                )

                if len(jobs) >= limit:
                    break
    except Exception as exc:
        print(f"[WARN] Error fetching RemoteOK API: {exc}", file=sys.stderr)

    return jobs


def fetch_himalayas_jobs(limit: int = 15) -> list[JobOpportunity]:
    """
    Fetches remote QA job listings from Himalayas API.
    """
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}
    endpoint = "https://himalayas.app/jobs/api?search=qa&limit=40"

    try:
        resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                title = str(item.get("title") or "").strip()
                company = str(item.get("companyName") or "Remote Client").strip()
                url = str(item.get("applicationUrl") or f"https://himalayas.app/companies/{item.get('companySlug')}/jobs/{item.get('slug')}").strip()
                desc_clean = clean_html(str(item.get("description") or ""))
                loc_restrictions = item.get("locationRestrictions") or []
                location = ", ".join(loc_restrictions) if loc_restrictions else "Worldwide Remote"

                if not is_qa_related(title, desc_clean):
                    continue

                min_sal = item.get("minSalary")
                max_sal = item.get("maxSalary")
                salary = f"${min_sal:,} - ${max_sal:,} USD" if min_sal and max_sal else ""

                is_india = detect_india_region(f"{title} {location} {desc_clean}")
                slug = item.get("slug") or _generate_job_id(url, title)

                jobs.append(
                    JobOpportunity(
                        id=f"himalayas_{slug}",
                        title=title,
                        company=company,
                        url=url,
                        description=desc_clean,
                        source="Himalayas",
                        salary=salary,
                        location=location,
                        published_at=str(item.get("pubDate") or "Reciente"),
                        job_type="Remote Role",
                        is_india=is_india,
                    )
                )

                if len(jobs) >= limit:
                    break
    except Exception as exc:
        print(f"[WARN] Error fetching Himalayas API: {exc}", file=sys.stderr)

    return jobs


def fetch_hasjob_jobs(limit: int = 15) -> list[JobOpportunity]:
    """
    Fetches and filters tech jobs from Hasjob (Hasgeek) Atom feed.
    Hasjob is a leading community job board for the Indian tech startup ecosystem.
    """
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}
    endpoint = "https://hasjob.co/feed"

    try:
        resp = requests.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return jobs

        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns) or root.findall(".//{http://www.w3.org/2005/Atom}entry")

        for entry in entries:
            title_elem = entry.find("atom:title", ns)
            if title_elem is None:
                title_elem = entry.find("{http://www.w3.org/2005/Atom}title")

            link_elem = entry.find("atom:link", ns)
            if link_elem is None:
                link_elem = entry.find("{http://www.w3.org/2005/Atom}link")

            content_elem = entry.find("atom:content", ns)
            if content_elem is None:
                content_elem = entry.find("{http://www.w3.org/2005/Atom}content")

            summary_elem = entry.find("atom:summary", ns)
            if summary_elem is None:
                summary_elem = entry.find("{http://www.w3.org/2005/Atom}summary")

            pub_elem = entry.find("atom:published", ns)
            if pub_elem is None:
                pub_elem = entry.find("{http://www.w3.org/2005/Atom}published")

            title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
            url = link_elem.attrib.get("href", "").strip() if link_elem is not None else ""
            raw_body = (content_elem.text if content_elem is not None and content_elem.text else "") or (summary_elem.text if summary_elem is not None and summary_elem.text else "")
            desc_clean = clean_html(raw_body)
            published = pub_elem.text.strip() if pub_elem is not None and pub_elem.text else ""


            if not is_qa_related(title, desc_clean):
                continue

            job_id = _generate_job_id(url, title)
            jobs.append(
                JobOpportunity(
                    id=f"hasjob_{job_id}",
                    title=title,
                    company="Indian Startup (Hasjob)",
                    url=url,
                    description=desc_clean,
                    source="Hasjob (India)",
                    salary="",
                    location="India / Remoto",
                    published_at=published,
                    job_type="Contrato / Empleo India Remoto",
                    is_india=True,
                )
            )

            if len(jobs) >= limit:
                break

    except Exception as exc:
        print(f"[WARN] Error fetching Hasjob Atom feed: {exc}", file=sys.stderr)

    return jobs


def fetch_custom_rss(feed_url: str, source_name: str = "CustomFeed") -> list[JobOpportunity]:
    """
    Parses any standard RSS 2.0 (<item>) or Atom (<entry>) feed.
    Useful for custom Upwork feeds, regional job boards, or specialized tech feeds.
    """
    jobs: list[JobOpportunity] = []
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(feed_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"[WARN] Feed personalizado devolvió HTTP {resp.status_code}: {feed_url}", file=sys.stderr)
            return jobs

        root = ET.fromstring(resp.content)

        # Check if Atom feed
        is_atom = "feed" in root.tag.lower()
        if is_atom:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall("atom:entry", ns) or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for item in items:
                title_elem = item.find("atom:title", ns)
                if title_elem is None:
                    title_elem = item.find("{http://www.w3.org/2005/Atom}title")

                link_elem = item.find("atom:link", ns)
                if link_elem is None:
                    link_elem = item.find("{http://www.w3.org/2005/Atom}link")

                desc_elem = item.find("atom:content", ns)
                if desc_elem is None:
                    desc_elem = item.find("atom:summary", ns)

                pub_elem = item.find("atom:published", ns)
                if pub_elem is None:
                    pub_elem = item.find("atom:updated", ns)

                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                url = link_elem.attrib.get("href", "").strip() if link_elem is not None else ""
                desc_clean = clean_html(desc_elem.text if desc_elem is not None and desc_elem.text else "")
                published = pub_elem.text.strip() if pub_elem is not None and pub_elem.text else ""


                if not is_qa_related(title, desc_clean):
                    continue

                is_india = detect_india_region(f"{title} {desc_clean}")
                job_id = _generate_job_id(url, title)
                jobs.append(
                    JobOpportunity(
                        id=job_id,
                        title=title,
                        company=source_name,
                        url=url,
                        description=desc_clean,
                        source=source_name,
                        published_at=published,
                        job_type="Freelance / Contract",
                        is_india=is_india,
                    )
                )
        else:
            # RSS 2.0
            items = root.findall(".//item")
            for item in items:
                title_elem = item.find("title")
                link_elem = item.find("link")
                desc_elem = item.find("description")
                pub_elem = item.find("pubDate")

                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                url = link_elem.text.strip() if link_elem is not None and link_elem.text else ""
                desc_clean = clean_html(desc_elem.text if desc_elem is not None and desc_elem.text else "")
                published = pub_elem.text.strip() if pub_elem is not None and pub_elem.text else ""

                if not is_qa_related(title, desc_clean):
                    continue

                is_india = detect_india_region(f"{title} {desc_clean}")
                job_id = _generate_job_id(url, title)
                jobs.append(
                    JobOpportunity(
                        id=job_id,
                        title=title,
                        company=source_name,
                        url=url,
                        description=desc_clean,
                        source=source_name,
                        published_at=published,
                        job_type="Freelance / Contract",
                        is_india=is_india,
                    )
                )
    except Exception as exc:
        print(f"[WARN] Error parseando feed {feed_url}: {exc}", file=sys.stderr)

    return jobs


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobSource:
    """
    A pluggable job source.

    name:              key used in ENABLED_SOURCES / --sources.
    fetch:             callable(limit) -> list[JobOpportunity]. It must catch its own network errors.
    freelance_platform: True for 100% freelance marketplaces (kept when --freelance-only is used).
    india_only:        True for boards that only make sense when ALLOW_INDIA_JOBS is enabled.
    label:             human-readable name for logs.
    """

    name: str
    fetch: Callable[[int], list[JobOpportunity]]
    freelance_platform: bool = False
    india_only: bool = False
    label: str = ""


SOURCE_REGISTRY: dict[str, JobSource] = {}


def register_source(source: JobSource) -> JobSource:
    """Registers (or replaces) a job source. Add new sources without touching the aggregator."""
    SOURCE_REGISTRY[source.name.lower()] = source
    return source


register_source(JobSource("freelancer", lambda n: fetch_freelancer_jobs(limit=n), freelance_platform=True, label="Freelancer.com"))
register_source(JobSource("remotive", lambda n: fetch_remotive_jobs(search_query="qa"), label="Remotive"))
register_source(JobSource("jobicy", lambda n: fetch_jobicy_jobs(limit=n), label="Jobicy"))
register_source(JobSource("remoteok", lambda n: fetch_remoteok_jobs(limit=n), label="RemoteOK"))
register_source(JobSource("himalayas", lambda n: fetch_himalayas_jobs(limit=n), label="Himalayas"))
register_source(JobSource("weworkremotely", lambda n: fetch_weworkremotely_jobs(), label="We Work Remotely"))
register_source(JobSource("hasjob", lambda n: fetch_hasjob_jobs(limit=n), india_only=True, label="Hasjob (India)"))
register_source(JobSource("arbeitnow", lambda n: fetch_arbeitnow_jobs(), label="Arbeitnow"))

DEFAULT_ENABLED_SOURCES = "freelancer,remotive,jobicy,remoteok,himalayas,weworkremotely,arbeitnow"
SOURCE_TIMEOUT_SECONDS = int(os.getenv("SOURCE_TIMEOUT_SECONDS", "60"))
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "6"))


def _custom_feed_sources() -> list[JobSource]:
    """Builds one JobSource per URL in CUSTOM_JOB_FEEDS (comma separated RSS/Atom feeds)."""
    sources: list[JobSource] = []
    raw = os.getenv("CUSTOM_JOB_FEEDS", "").strip()
    for idx, feed in enumerate([f.strip() for f in raw.split(",") if f.strip()], start=1):
        sources.append(
            JobSource(
                name=f"feed_{idx}",
                fetch=lambda n, feed=feed, idx=idx: fetch_custom_rss(feed, source_name=f"Feed_{idx}"),
                label=f"Feed #{idx} ({feed[:40]})",
            )
        )
    return sources


def _resolve_sources(
    requested: Optional[list[str]],
    allow_india: bool,
    freelance_only: bool,
) -> list[JobSource]:
    """Turns requested names (or .env defaults) into the list of sources to run, in a stable order."""
    names = [s.strip().lower() for s in (requested or os.getenv("ENABLED_SOURCES", DEFAULT_ENABLED_SOURCES).split(",")) if s.strip()]

    selected: list[JobSource] = []
    for name in names:
        source = SOURCE_REGISTRY.get(name)
        if source is None:
            print(f"[WARN] Fuente desconocida '{name}'. Disponibles: {', '.join(sorted(SOURCE_REGISTRY))}", file=sys.stderr)
            continue
        if freelance_only and not source.freelance_platform:
            continue
        if source.india_only and not allow_india:
            continue
        if source not in selected:
            selected.append(source)

    selected.extend(_custom_feed_sources())
    return selected


def _run_source(source: JobSource, limit: int) -> tuple[list[JobOpportunity], float]:
    started = time.monotonic()
    jobs = source.fetch(limit) or []
    return jobs[:limit], time.monotonic() - started


def fetch_from_sources(selected: list[JobSource], limit_per_source: int) -> list[JobOpportunity]:
    """
    Runs the selected sources concurrently (I/O bound). A failing or slow source never blocks or
    breaks the others. Results are returned in registry order for deterministic output.
    """
    if not selected:
        return []

    results: dict[str, list[JobOpportunity]] = {}
    executor = ThreadPoolExecutor(max_workers=max(1, min(FETCH_WORKERS, len(selected))))
    futures = {executor.submit(_run_source, src, limit_per_source): src for src in selected}
    try:
        for future, src in futures.items():
            label = src.label or src.name
            try:
                jobs, elapsed = future.result(timeout=SOURCE_TIMEOUT_SECONDS)
                results[src.name] = jobs
                print(f"    [{label}] {len(jobs)} ofertas QA pre-filtradas ({elapsed:.1f}s)")
            except FutureTimeout:
                print(f"    [{label}] [WARN] timeout tras {SOURCE_TIMEOUT_SECONDS}s, se omite.", file=sys.stderr)
            except Exception as exc:
                print(f"    [{label}] [WARN] fallo la fuente: {exc}", file=sys.stderr)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    ordered: list[JobOpportunity] = []
    for src in selected:
        ordered.extend(results.get(src.name, []))
    return ordered


def fetch_all_new_qa_jobs(
    history_manager: Optional[JobHistoryManager] = None,
    limit_per_source: int = 15,
    sources: Optional[list[str]] = None,
    allow_india: Optional[bool] = None,
    freelance_only: bool = False,
    india_only: bool = False,
) -> list[JobOpportunity]:
    """
    Main aggregator: fetches QA jobs concurrently from every enabled source in SOURCE_REGISTRY
    (plus CUSTOM_JOB_FEEDS), applies region/freelance filters and returns only NEW opportunities
    (not in history, deduplicated by id and by cross-source fingerprint).
    """
    history = history_manager or JobHistoryManager()
    seen_in_run: set[str] = set()

    if allow_india is None:
        allow_india = os.getenv("ALLOW_INDIA_JOBS", "false").strip().lower() in ("true", "1", "yes")

    selected = _resolve_sources(sources, allow_india=allow_india or india_only, freelance_only=freelance_only)

    print("\n" + "=" * 65)
    print(">> INICIANDO AGREGADOR DE OFERTAS QA EN VIVO")
    print(f"   Fuentes activas: {', '.join(s.name for s in selected) or '(ninguna)'}")
    print(f"   Filtros: freelance_only={freelance_only} | india_only={india_only} | allow_india={allow_india}")
    print("=" * 65)

    all_jobs = fetch_from_sources(selected, limit_per_source)

    new_jobs: list[JobOpportunity] = []
    for job in all_jobs:
        if freelance_only and "freelance" not in job.source.lower() and "fixed" not in job.job_type.lower():
            continue
        if india_only and not job.is_india:
            continue
        if not allow_india and not india_only and job.is_india:
            continue

        fp = fingerprint_of(job)
        if job.id in seen_in_run or fp in seen_in_run:
            continue
        seen_in_run.add(job.id)
        seen_in_run.add(fp)

        if history.is_known(job):
            continue

        new_jobs.append(job)

    print(f"\n[+] Total de ofertas QA NUEVAS encontradas y listas para evaluar: {len(new_jobs)}")
    return new_jobs


if __name__ == "__main__":
    print("=" * 60)
    print(">> TEST DE OBTENCION DE OFERTAS LABORALES REALES")
    print("=" * 60)

    mgr = JobHistoryManager()
    jobs = fetch_all_new_qa_jobs(history_manager=mgr, limit_per_source=3)

    for i, j in enumerate(jobs[:8], start=1):
        ind_badge = " [🇮🇳 INDIA]" if j.is_india else ""
        print(f"\n[{i}] {j.title}{ind_badge}")
        print(f"    Empresa: {j.company} | Fuente: {j.source} | Tipo: {j.job_type}")
        print(f"    URL: {j.url}")
        print(f"    Tarifa/Loc: {j.salary or 'N/A'} | {j.location}")
        print(f"    Snippet: {j.description[:120]}...")

