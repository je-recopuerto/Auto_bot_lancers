#!/usr/bin/env python3
"""
Fetch Lancers job search results with browser-like HTTP requests.

Monitors LANCERS_URL then optional LANCERS_URL1 sequentially in a continuous
loop (real-time job watch). Seen job URLs are stored permanently in MongoDB
(lancers.post) so reappearing listings are not treated as new.

  LANCERS_URL  + TELEGRAM_BOT_TOKEN
    -> HTML dump + Telegram bot 1
  LANCERS_URL1 + TELEGRAM_BOT_TOKEN1
    -> HTML dump + Telegram bot 2
  Shared MongoDB: MONGODB_DB / MONGODB_COLLECTION (default lancers / post)

When new jobs are detected, each job detail page is fetched and parsed so
Telegram messages include client verification, ratings, schedule, and the
fuller job description — not only search-list fields.

Example:
  python lancers_fetch.py
  python lancers_fetch.py --once
  python lancers_fetch.py --interval 120
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError


DEFAULT_URL = (
    "https://www.lancers.jp/work/search/system?open=1&ref=header_menu"
)
DEFAULT_CHAT_CACHE_FILE = "telegram_chat_ids.json"
DEFAULT_POLL_INTERVAL_SEC = 60
# Only the top N search results are checked each cycle. Lifetime dedupe is
# handled by MongoDB (unique url), so reappearing older jobs stay suppressed.
JOB_COMPARE_LIMIT = 10
DETAIL_HREF_RE = re.compile(r"/work/detail/(\d+)")
BUDGET_RE = re.compile(
    r"(?:プロジェクト|コンペ|タスク|時間単価)?\s*"
    r"([\d,]+)\s*(?:円)?\s*[~〜～\-–—]\s*([\d,]+)\s*円"
    r"(?:\s*/\s*(固定|時間))?",
    re.UNICODE,
)
BUDGET_SINGLE_RE = re.compile(
    r"(?:プロジェクト|コンペ|タスク|時間単価)?\s*"
    r"([\d,]+)\s*円(?:\s*/\s*(固定|時間))?",
    re.UNICODE,
)
DEADLINE_RE = re.compile(r"あと\s*\d+\s*日|募集終了|募集中")
APPLICANTS_RE = re.compile(
    r"当選者数\s*([\d,]+)\s*人\s*/\s*募集人数\s*([\d,]+)\s*人"
)
SNAPSHOT_FIELDS = (
    "title",
    "url",
    "budget",
    "applicants",
    "deadline",
    "summary",
    "skills",
)


@dataclass
class Job:
    title: str
    url: str
    budget: Optional[str]
    applicants: Optional[str]
    deadline: Optional[str]
    summary: Optional[str]
    skills: List[str]
    # Fields filled from the job detail page (not stored in search snapshots).
    job_id: Optional[str] = None
    work_type: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    schedule: Dict[str, str] = field(default_factory=dict)
    proposals_count: Optional[str] = None
    favorites_count: Optional[str] = None
    views_count: Optional[str] = None
    in_progress_count: Optional[str] = None
    completed_count: Optional[str] = None
    winners: Optional[str] = None
    capacity: Optional[str] = None
    client_name: Optional[str] = None
    client_url: Optional[str] = None
    client_verifications: Dict[str, bool] = field(default_factory=dict)
    client_rating_good: Optional[str] = None
    client_rating_bad: Optional[str] = None
    client_order_rate: Optional[str] = None
    client_order_counts: Optional[str] = None
    description: Optional[str] = None
    propose_url: Optional[str] = None
    detail_fetched: bool = False


def job_to_snapshot(job: Job) -> Dict[str, object]:
    return {name: getattr(job, name) for name in SNAPSHOT_FIELDS}


def parse_cookie_header(raw_cookie: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for chunk in raw_cookie.split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def build_headers(referer: str) -> Dict[str, str]:
    return {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": "ja,en-US;q=0.9,en;q=0.8",
        "cache-control": "max-age=0",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "referer": referer,
    }


def abs_url(base_url: str, maybe_relative: str) -> str:
    if maybe_relative.startswith("http://") or maybe_relative.startswith("https://"):
        return maybe_relative
    root = "{u.scheme}://{u.netloc}".format(u=urlparse(base_url))
    if maybe_relative.startswith("/"):
        return root + maybe_relative
    return root + "/" + maybe_relative


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


TITLE_BADGE_TOKENS = (
    "NEW",
    "PR",
    "初回",
    "2回目",
    "3回目",
    "注目",
    "募集中",
)


def clean_job_title(value: Optional[str]) -> str:
    """Strip Lancers badge tokens (NEW / 初回 / 注目 / …) from a job title."""
    title = clean_text(value) or ""
    if not title:
        return ""

    # Remove leading glued badges: "NEW初回注目 Title" / "NEW Title"
    title = re.sub(
        r"^(?:NEW|PR|初回|2回目|3回目|注目)+\s*",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    # Remove trailing badges: "Title NEW"
    title = re.sub(
        r"\s*(?:NEW|PR|初回|2回目|3回目|注目)+$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()

    # Remove badge tokens anywhere they appear as whole words.
    for token in TITLE_BADGE_TOKENS:
        title = re.sub(
            rf"(?<!\w){re.escape(token)}(?!\w)",
            " ",
            title,
            flags=re.IGNORECASE,
        )
    title = re.sub(r"\s+", " ", title).strip(" -|/、,")
    return title


def is_waf_challenge(page_html: str) -> bool:
    markers = (
        "Human Verification",
        "awsWafCookieDomainList",
        "CaptchaScript.renderCaptcha",
        "AwsWafIntegration",
    )
    return any(marker in page_html for marker in markers)


class WafChallengeError(RuntimeError):
    """Raised when Lancers returns an AWS WAF challenge page."""


def extract_budget(text: str) -> Optional[str]:
    match = BUDGET_RE.search(text)
    if match:
        low, high = match.group(1), match.group(2)
        kind = match.group(3) or "固定"
        return f"{low} - {high} JPY / {kind}"
    match = BUDGET_SINGLE_RE.search(text)
    if match:
        amount = match.group(1)
        kind = match.group(2) or "固定"
        return f"{amount} JPY / {kind}"
    return None


def extract_deadline(text: str) -> Optional[str]:
    match = DEADLINE_RE.search(text)
    return clean_text(match.group(0)) if match else None


def extract_applicants(text: str) -> Optional[str]:
    match = APPLICANTS_RE.search(text)
    if not match:
        return None
    return f"Applicants: {match.group(1)} / Capacity: {match.group(2)}"


def extract_skills_from_card(card) -> List[str]:
    skills: List[str] = []
    for el in card.select(
        ".c-skill-list a, .c-tag, .c-badge, [class*='skill'] a, [class*='tag'] a"
    ):
        text = clean_text(el.get_text(" ", strip=True))
        if not text:
            continue
        if text in {"NEW", "初回", "注目", "募集中", "プロジェクト", "コンペ", "タスク"}:
            continue
        if text not in skills:
            skills.append(text)
    return skills[:12]


def pick_summary(card_text: str, title: str) -> Optional[str]:
    # Prefer a longer prose-like chunk after the title.
    chunks = [clean_text(part) for part in re.split(r"[\n\r]+", card_text)]
    chunks = [c for c in chunks if c and c != title]
    for chunk in chunks:
        if len(chunk) < 40:
            continue
        if chunk.startswith("当選者数") or "円" in chunk[:20]:
            continue
        if DEADLINE_RE.fullmatch(chunk):
            continue
        return chunk[:240]
    # Fallback: longest leftover line.
    leftovers = sorted(
        (c for c in chunks if c and "円" not in c[:12] and not c.startswith("当選者数")),
        key=len,
        reverse=True,
    )
    return leftovers[0][:240] if leftovers else None


def job_from_card(card, base_url: str) -> Optional[Job]:
    link = card.select_one(
        'a.c-media__title[href*="/work/detail/"], '
        'a[href*="/work/detail/"]'
    )
    if not link:
        return None

    href = (link.get("href") or "").strip()
    if not DETAIL_HREF_RE.search(href):
        return None

    title_el = card.select_one(".c-media__title-inner, .c-media__title, h2, h3")
    title = clean_text(
        (title_el or link).get_text(" ", strip=True)
    )
    if not title:
        return None

    title = clean_job_title(title)
    if not title:
        return None

    budget_el = card.select_one(
        ".c-media__job-price, [class*='job-price'], [class*='price']"
    )
    card_text = clean_text(card.get_text("\n", strip=True)) or ""

    budget = clean_text(budget_el.get_text(" ", strip=True)) if budget_el else None
    if budget:
        budget = extract_budget(budget) or budget
    else:
        budget = extract_budget(card_text)

    deadline_el = card.select_one(
        ".c-media__time, [class*='remaining'], [class*='deadline'], time"
    )
    deadline = (
        clean_text(deadline_el.get_text(" ", strip=True)) if deadline_el else None
    ) or extract_deadline(card_text)

    applicants = extract_applicants(card_text)
    skills = extract_skills_from_card(card)
    summary = pick_summary(card.get_text("\n", strip=True), title)

    return Job(
        title=title,
        url=abs_url(base_url, href.split("?")[0]),
        budget=budget,
        applicants=applicants,
        deadline=deadline,
        summary=summary,
        skills=skills,
    )


def extract_jobs_from_media_list(soup: BeautifulSoup, base_url: str) -> List[Job]:
    cards = soup.select(
        ".c-media-list.c-media-list--forClient > div, "
        ".c-media-list > .c-media, "
        ".c-media, "
        "[class*='search-result'] [class*='media'], "
        "li.c-media, "
        "div.p-search-job-cassette, "
        "[class*='JobCard'], "
        "[class*='job-card']"
    )
    jobs: List[Job] = []
    seen: Set[str] = set()
    for card in cards:
        job = job_from_card(card, base_url)
        if not job or job.url in seen:
            continue
        seen.add(job.url)
        jobs.append(job)
    return jobs


def extract_jobs_from_detail_links(soup: BeautifulSoup, base_url: str) -> List[Job]:
    jobs: List[Job] = []
    seen: Set[str] = set()

    for link in soup.select('a[href*="/work/detail/"]'):
        href = (link.get("href") or "").strip()
        match = DETAIL_HREF_RE.search(href)
        if not match:
            continue

        url = abs_url(base_url, href.split("?")[0])
        if url in seen:
            continue

        title = clean_text(link.get_text(" ", strip=True))
        if not title or len(title) < 4:
            # Title may live on a nested element; try parent card text later.
            parent_title = link.select_one("h2, h3, span, p")
            title = clean_text(parent_title.get_text(" ", strip=True)) if parent_title else None
        if not title or len(title) < 4:
            continue

        title = clean_job_title(title)
        if not title or len(title) < 4:
            continue

        card = link
        for _ in range(8):
            if card.parent is None:
                break
            card = card.parent
            classes = " ".join(card.get("class") or [])
            if any(
                token in classes.lower()
                for token in ("media", "job", "work", "cassette", "card", "result")
            ):
                break

        card_text = clean_text(card.get_text("\n", strip=True)) or ""
        jobs.append(
            Job(
                title=title,
                url=url,
                budget=extract_budget(card_text),
                applicants=extract_applicants(card_text),
                deadline=extract_deadline(card_text),
                summary=pick_summary(card.get_text("\n", strip=True), title),
                skills=extract_skills_from_card(card),
            )
        )
        seen.add(url)

    return jobs


def extract_jobs(page_html: str, base_url: str) -> List[Job]:
    soup = BeautifulSoup(page_html, "html.parser")

    jobs = extract_jobs_from_media_list(soup, base_url)
    if jobs:
        return jobs

    return extract_jobs_from_detail_links(soup, base_url)


def escape_html(value: Optional[str]) -> str:
    if not value:
        return ""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def extract_job_id(url: str) -> Optional[str]:
    match = DETAIL_HREF_RE.search(url or "")
    return match.group(1) if match else None


def _definition_list_status(dt) -> bool:
    """True when the client verification item shows a check icon."""
    if dt is None:
        return False
    if dt.select_one("i.c-icon--circle-check, .c-icon--circle-check"):
        return True
    text = clean_text(dt.get_text(" ", strip=True)) or ""
    return bool(text) and text not in {"--", "-", "×", "✗", "未"}


def _col_heading_text(soup: BeautifulSoup, col_class: str) -> Optional[str]:
    col = soup.select_one(f".tableSummary__col{col_class}")
    if not col:
        return None
    heading = col.select_one(".c-heading--lv5, .c-heading")
    return clean_text(heading.get_text(" ", strip=True)) if heading else None


def extract_detail_budget(soup: BeautifulSoup) -> Optional[str]:
    compensation = soup.select_one(".tableSummary__col--compensation")
    if compensation:
        text = clean_text(compensation.get_text(" ", strip=True)) or ""
        # Drop work-type badge text prefix when present.
        parsed = extract_budget(text)
        if parsed:
            return parsed
        numbers = [
            clean_text(el.get_text(" ", strip=True))
            for el in compensation.select(".price-number")
        ]
        numbers = [n for n in numbers if n and n not in {"~", "〜"}]
        units = compensation.select_one(".price-unit")
        unit = clean_text(units.get_text(" ", strip=True)) if units else "円"
        money = [n for n in numbers if any(ch.isdigit() for ch in n)]
        if len(money) >= 2:
            return f"{money[0]} - {money[1]} {unit}"
        if len(money) == 1:
            return f"{money[0]} {unit}"
    return None


def extract_client_verifications(box) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    if box is None:
        return result
    for dl in box.select(".p-work-detail-client-box-work-status__content-list"):
        label_el = dl.select_one(
            ".p-work-detail-client-box-work-status__content-list-item, dd"
        )
        status_el = dl.select_one(
            ".p-work-detail-client-box-work-status__content-list-title, dt"
        )
        label = clean_text(label_el.get_text(" ", strip=True)) if label_el else None
        if not label:
            continue
        result[label] = _definition_list_status(status_el)
    return result


def extract_proposal_status_map(soup: BeautifulSoup) -> Dict[str, str]:
    status: Dict[str, str] = {}
    for paragraph in soup.select(".work_detail__proposal-status p"):
        number_el = paragraph.select_one(".work_detail__proposal-status-number")
        if not number_el:
            continue
        number = clean_text(number_el.get_text(" ", strip=True))
        label = clean_text(
            paragraph.get_text(" ", strip=True).replace(number or "", "", 1)
        )
        if label and number:
            # Strip trailing unit words like 件/人/回 for cleaner labels.
            label = re.sub(r"(件|人|回)$", "", label).strip() or label
            status[label] = number
    return status


def parse_job_detail_html(page_html: str, job: Job) -> Job:
    """Enrich a search-list Job with fields from its detail page HTML."""
    soup = BeautifulSoup(page_html, "html.parser")
    job.detail_fetched = True
    job.job_id = extract_job_id(job.url) or job.job_id

    h1 = soup.select_one("h1.c-heading--lv1, h1.c-heading")
    if h1:
        # Clone-ish: prefer direct text nodes, fall back to full text without sub.
        sub = h1.select_one(".c-heading__sub")
        if sub:
            job.category = clean_text(sub.get_text(" ", strip=True))
            sub.extract()
        title = clean_text(h1.get_text(" ", strip=True))
        title = clean_job_title(title)
        if title:
            job.title = title

    work_type = soup.select_one(
        ".tableSummary__col--compensation .c-badge__text, .c-badge__text"
    )
    if work_type:
        job.work_type = clean_text(work_type.get_text(" ", strip=True))

    detail_budget = extract_detail_budget(soup)
    if detail_budget:
        job.budget = detail_budget

    deadline_left = _col_heading_text(soup, "--time")
    if deadline_left:
        job.deadline = f"あと{deadline_left}" if not deadline_left.startswith("あと") else deadline_left

    proposals_heading = _col_heading_text(soup, "--worksNum")
    if proposals_heading:
        job.proposals_count = proposals_heading.replace("件", "").strip()

    winners_col = soup.select_one(".tableSummary__col--WinnerNum")
    if winners_col:
        winners_heading = winners_col.select_one(".c-heading--lv5, .c-heading")
        if winners_heading:
            job.winners = clean_text(
                winners_heading.get_text(" ", strip=True).replace("件", "")
            )
        capacity_match = re.search(
            r"募集人数\s*([\d,]+)\s*人",
            winners_col.get_text(" ", strip=True),
        )
        if capacity_match:
            job.capacity = capacity_match.group(1)
        if job.winners and job.capacity:
            job.applicants = (
                f"Applicants: {job.winners} / Capacity: {job.capacity}"
            )

    tags: List[str] = []
    for li in soup.select("ul.c-lists li.c-tag"):
        text = clean_text(li.get_text(" ", strip=True))
        if text and text not in tags:
            tags.append(text)
    job.tags = tags[:12]

    schedule: Dict[str, str] = {}
    for item in soup.select(".p-work-detail-schedule__item"):
        title_el = item.select_one(".p-work-detail-schedule__item__title")
        value_el = item.select_one(".p-work-detail-schedule__text")
        key = clean_text(title_el.get_text(" ", strip=True)) if title_el else None
        value = clean_text(value_el.get_text(" ", strip=True)) if value_el else None
        if key and value:
            schedule[key.rstrip("：:")] = value
    job.schedule = schedule

    status_map = extract_proposal_status_map(soup)
    job.proposals_count = status_map.get("提案数", job.proposals_count)
    job.favorites_count = status_map.get("お気に入り", job.favorites_count)
    job.views_count = status_map.get("閲覧数", job.views_count)
    job.in_progress_count = status_map.get("進行中", job.in_progress_count)
    job.completed_count = status_map.get("完了数", job.completed_count)

    box = soup.select_one(".p-work-detail-client-box")
    if box:
        name_el = box.select_one(".client_name a.c-link, a.c-link")
        if name_el:
            job.client_name = clean_text(name_el.get_text(" ", strip=True))
            href = (name_el.get("href") or "").strip()
            if href:
                job.client_url = abs_url(job.url, href.split("?")[0])
        job.client_verifications = extract_client_verifications(box)

        good = box.select_one(
            ".p-work-detail-client-box-feedback-info__number-good"
        )
        bad = box.select_one(
            ".p-work-detail-client-box-feedback-info__number-bad"
        )
        if good:
            job.client_rating_good = clean_text(good.get_text(" ", strip=True))
        if bad:
            job.client_rating_bad = clean_text(bad.get_text(" ", strip=True))

        rate = box.select_one(".p-work-detail-client-box-feedback-info__percent")
        if rate:
            job.client_order_rate = clean_text(rate.get_text(" ", strip=True))
        awarded = box.select_one(".result_summary_awarded")
        posted = box.select_one(".result_summary_posted")
        if awarded and posted:
            job.client_order_counts = (
                f"{clean_text(awarded.get_text(' ', strip=True))} / "
                f"{clean_text(posted.get_text(' ', strip=True))}"
            )

    desc_el = soup.select_one(
        ".p-work-detail-lancer__postscript-description, "
        "dd.p-work-detail-lancer__postscript-description"
    )
    if desc_el:
        # Preserve light paragraph breaks from <br>.
        for br in desc_el.find_all("br"):
            br.replace_with("\n")
        description = desc_el.get_text("\n", strip=True)
        description = re.sub(r"\n{3,}", "\n\n", description).strip()
        if description:
            job.description = description
            if not job.summary:
                job.summary = clean_text(description[:240])

    if job.job_id:
        job.propose_url = abs_url(job.url, f"/work/propose_start/{job.job_id}")

    return job


def detail_url_for_lancer(url: str) -> str:
    """
    Force the lancer-facing detail page.

    Bare /work/detail/{id} can return a public/client summary without the
    client sidebar. purpose=lancer loads the full posting used for enrichment.
    """
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["purpose"] = "lancer"
    return urlunparse(parsed._replace(query=urlencode(query)))


def enrich_job_from_detail(
    job: Job,
    cookie_header: Optional[str],
    timeout: int,
    referer: str,
) -> Tuple[Job, Optional[str]]:
    """
    Fetch the job detail page and enrich the Job.

    Returns (job, error_message). error_message is set when enrichment failed
    but the original search-list fields are still usable.
    """
    if not job.url:
        return job, "missing job URL"

    detail_url = detail_url_for_lancer(job.url)
    try:
        response = fetch_page(
            detail_url, cookie_header, timeout, referer=referer or job.url
        )
    except requests.RequestException as exc:
        return job, f"detail fetch failed: {exc}"

    html = response.text
    if is_waf_challenge(html):
        return job, "AWS WAF challenge on detail page"

    # Logged-out / wrong-purpose pages lack the client box; treat as soft failure.
    if "p-work-detail-client-box" not in html and "p-work-detail-lancer" not in html:
        return job, "detail page missing lancer layout (try refreshing LANCERS_COOKIE)"

    try:
        parse_job_detail_html(html, job)
    except Exception as exc:  # noqa: BLE001 - keep notifying with search data
        return job, f"detail parse failed: {exc}"

    return job, None


def fetch_page(
    url: str,
    cookie_header: Optional[str],
    timeout: int,
    referer: Optional[str] = None,
) -> requests.Response:
    session = requests.Session()
    session.headers.update(build_headers(referer=referer or url))

    if cookie_header:
        for name, value in parse_cookie_header(cookie_header).items():
            session.cookies.set(name, value, domain=".lancers.jp")

    response = session.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response


def split_telegram_messages(text: str, max_length: int = 4000) -> List[str]:
    if len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= max_length:
            current += line
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        if len(line) <= max_length:
            current = line
            continue

        start = 0
        while start < len(line):
            end = start + max_length
            chunks.append(line[start:end].rstrip())
            start = end

    if current:
        chunks.append(current.rstrip())
    return chunks


def _verification_label_icon(label: str) -> str:
    mapping = {
        "本人確認": "🆔 : ",
        "機密保持確認": "🔒 : ",
        "電話確認": "📞 : ",
        "ランサーズチェック": "🏅 : ",
    }
    return mapping.get(label, "• : ")


def render_telegram_job_html(job: Job) -> str:
    title = clean_job_title(job.title) or (job.title or "")
    lines: List[str] = [f"<b>{escape_html(title)}</b>", ""]

    if job.category:
        lines.append(f"📂 Category: {escape_html(job.category)}")
    if job.budget:
        lines.append(f"💰 Budget: {escape_html(job.budget)}")
    if job.deadline:
        lines.append(f"⏰ Deadline: {escape_html(job.deadline)}")

    status_bits: List[str] = []
    if job.proposals_count:
        status_bits.append(f"📨 {escape_html(job.proposals_count)}")
    if job.favorites_count:
        status_bits.append(f"❤️ {escape_html(job.favorites_count)}")
    if job.views_count:
        status_bits.append(f"👁 {escape_html(job.views_count)}")
    if job.in_progress_count:
        status_bits.append(f"🔄 {escape_html(job.in_progress_count)}")
    if job.completed_count:
        status_bits.append(f"✅ {escape_html(job.completed_count)}")
    if status_bits:
        lines.append("📊 Status: " + " | ".join(status_bits))

    if job.schedule:
        schedule_bits = [
            f"{escape_html(key)} {escape_html(value)}"
            for key, value in job.schedule.items()
        ]
        lines.append("📅 Schedule: " + " / ".join(schedule_bits))

    if job.skills:
        lines.append(f"🛠 Skills: {escape_html(', '.join(job.skills[:8]))}")

    if job.client_name or job.client_verifications:
        lines.append("")
        lines.append("<b>👤 Client</b>")
        if job.client_name:
            if job.client_url:
                lines.append(
                    f"🧑 Name: <a href=\"{job.client_url}\">"
                    f"{escape_html(job.client_name)}</a>"
                )
            else:
                lines.append(f"🧑 Name: {escape_html(job.client_name)}")
        if job.client_verifications:
            verify_bits = []
            for label, ok in job.client_verifications.items():
                icon = _verification_label_icon(label)
                mark = "✔" if ok else "❌"
                verify_bits.append(f"{icon}{mark}")
            lines.append("🛡 Verification: " + " | ".join(verify_bits))
        rating_bits: List[str] = []
        if job.client_rating_good is not None:
            rating_bits.append(f"👍 {escape_html(job.client_rating_good)}")
        if job.client_rating_bad is not None:
            rating_bits.append(f"👎 {escape_html(job.client_rating_bad)}")
        if rating_bits:
            lines.append("⭐ Rating: " + " / ".join(rating_bits))
        if job.client_order_rate:
            order = f"📈 Order rate: {escape_html(job.client_order_rate)}"
            if job.client_order_counts:
                order += f" ({escape_html(job.client_order_counts)})"
            lines.append(order)

    body = job.description or job.summary
    if body:
        snippet = body.strip()
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."
        lines.append(f"\n<b>📝 Details</b>\n{escape_html(snippet)}")

    return "\n".join(lines).strip()


def build_telegram_job_keyboard(job: Job) -> Dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": "View Details", "url": job.url}],
        ]
    }


def send_to_telegram(
    token: str,
    chat_id: str,
    message: str,
    timeout: int,
    reply_markup: Optional[Dict[str, object]] = None,
    parse_mode: Optional[str] = "HTML",
) -> int:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    for part in split_telegram_messages(message):
        data = {
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        resp = requests.post(api_url, data=data, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        sent += 1
    return sent


def get_mongo_collection(
    uri: str,
    db_name: str,
    collection_name: str,
) -> Collection:
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    # Fail fast if MongoDB is unreachable.
    client.admin.command("ping")
    collection = client[db_name][collection_name]
    collection.create_index([("url", ASCENDING)], unique=True)
    return collection


def filter_new_jobs(collection: Collection, jobs: List[Job]) -> List[Job]:
    """Return jobs whose detail URL is not yet stored in MongoDB."""
    urls = [job.url for job in jobs if job.url]
    if not urls:
        return []
    existing = {
        doc["url"]
        for doc in collection.find({"url": {"$in": urls}}, {"url": 1, "_id": 0})
    }
    return [job for job in jobs if job.url and job.url not in existing]


def job_to_mongo_doc(
    job: Job,
    *,
    source_label: str,
    source_search_url: str,
) -> Dict[str, object]:
    doc = job_to_snapshot(job)
    doc["source_label"] = source_label
    doc["source_search_url"] = source_search_url
    doc["created_at"] = datetime.now(timezone.utc)
    return doc


def insert_new_jobs(
    collection: Collection,
    jobs: List[Job],
    *,
    source_label: str,
    source_search_url: str,
) -> int:
    """Insert jobs by unique URL. Returns number of documents inserted."""
    inserted = 0
    for job in jobs:
        if not job.url:
            continue
        try:
            collection.insert_one(
                job_to_mongo_doc(
                    job,
                    source_label=source_label,
                    source_search_url=source_search_url,
                )
            )
            inserted += 1
        except DuplicateKeyError:
            continue
    return inserted


def seed_mongo_from_json_files(
    collection: Collection,
    json_paths: List[str],
) -> int:
    """
    One-time helper: if the collection is empty, import URLs from legacy
    JSON snapshot files so the first MongoDB run does not re-alert everything.
    """
    if collection.estimated_document_count() > 0:
        return 0

    inserted = 0
    for path in json_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", "")).strip()
            if not url:
                continue
            doc = {
                "title": str(row.get("title", "")),
                "url": url,
                "budget": row.get("budget"),
                "applicants": row.get("applicants"),
                "deadline": row.get("deadline"),
                "summary": row.get("summary"),
                "skills": row.get("skills")
                if isinstance(row.get("skills"), list)
                else [],
                "source_label": "seed",
                "source_search_url": "",
                "created_at": datetime.now(timezone.utc),
            }
            try:
                collection.insert_one(doc)
                inserted += 1
            except DuplicateKeyError:
                continue
    return inserted


def load_cached_chat_ids(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
        if not isinstance(payload, list):
            return []
        chat_ids: List[str] = []
        seen = set()
        for item in payload:
            value = str(item).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            chat_ids.append(value)
        return chat_ids
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def save_cached_chat_ids(path: str, chat_ids: List[str]) -> None:
    unique_ids: List[str] = []
    seen = set()
    for chat_id in chat_ids:
        value = str(chat_id).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique_ids.append(value)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(unique_ids, fp, ensure_ascii=False, indent=2)


def upsert_cached_chat_id(path: str, chat_id: str) -> None:
    current = load_cached_chat_ids(path)
    if chat_id in current:
        return
    current.append(chat_id)
    save_cached_chat_ids(path, current)


def telegram_get_updates(
    token: str, offset: Optional[int], timeout: int
) -> List[Dict[str, object]]:
    api_url = f"https://api.telegram.org/bot{token}/getUpdates"
    payload: Dict[str, object] = {"timeout": 25}
    if offset is not None:
        payload["offset"] = offset
    resp = requests.get(api_url, params=payload, timeout=timeout + 30)
    if resp.status_code == 409:
        return []
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {body}")
    return body.get("result", [])


def extract_chat_id_from_update(update: Dict[str, object]) -> Optional[str]:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        payload = update.get(key)
        if not isinstance(payload, dict):
            continue
        chat = payload.get("chat")
        if isinstance(chat, dict) and "id" in chat:
            return str(chat["id"])
    return None


def get_known_chat_ids(token: str, timeout: int) -> List[str]:
    updates = telegram_get_updates(token, None, timeout)
    chat_ids: List[str] = []
    seen = set()
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        chat_id = extract_chat_id_from_update(upd)
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        chat_ids.append(chat_id)
    return chat_ids


def get_startup_chat_ids(token: str, cache_file: str, timeout: int) -> List[str]:
    cached = load_cached_chat_ids(cache_file)
    if cached:
        return cached
    discovered = get_known_chat_ids(token, timeout)
    if discovered:
        save_cached_chat_ids(cache_file, discovered)
    return discovered


@dataclass
class WatchTarget:
    """One Lancers search URL with dedicated snapshot files and Telegram bot."""

    label: str
    url: str
    save_html: str
    save_json: str
    telegram_token: str
    chat_cache_file: str


def _parallel_path(path: str, suffix: str = "_1") -> str:
    """Insert a suffix before the file extension (e.g. a.json -> a_1.json)."""
    for ext in (".html", ".json"):
        if path.endswith(ext):
            return path[: -len(ext)] + suffix + ext
    return path + suffix


def collect_watch_targets(
    env_url: str,
    env_url1: str,
    cli_url: Optional[str],
    save_html: str,
    save_json: str,
    telegram_token: str,
    telegram_token1: str,
    chat_cache_file: str,
) -> List[WatchTarget]:
    """
    Build the ordered list of URLs to monitor.

    Defaults:
      LANCERS_URL  + TELEGRAM_BOT_TOKEN
        -> lancers_search.html / lancers_jobs.json / telegram_chat_ids.json
      LANCERS_URL1 + TELEGRAM_BOT_TOKEN1
        -> lancers_search_1.html / lancers_jobs_1.json / telegram_chat_ids_1.json

    If --url is passed explicitly, only that single URL is monitored (using
    the primary token and --save-html / --save-json / --chat-cache-file).
    """
    if cli_url is not None:
        return [
            WatchTarget(
                label="1",
                url=cli_url,
                save_html=save_html,
                save_json=save_json,
                telegram_token=telegram_token,
                chat_cache_file=chat_cache_file,
            )
        ]

    targets: List[WatchTarget] = []
    primary = (env_url or "").strip() or DEFAULT_URL
    targets.append(
        WatchTarget(
            label="1",
            url=primary,
            save_html=save_html,
            save_json=save_json,
            telegram_token=(telegram_token or "").strip(),
            chat_cache_file=chat_cache_file,
        )
    )

    secondary = (env_url1 or "").strip()
    if secondary:
        targets.append(
            WatchTarget(
                label="2",
                url=secondary,
                save_html=_parallel_path(save_html),
                save_json=_parallel_path(save_json),
                telegram_token=(telegram_token1 or "").strip(),
                chat_cache_file=_parallel_path(chat_cache_file),
            )
        )

    return targets


def process_watch_target(
    target: WatchTarget,
    cookie: Optional[str],
    timeout: int,
    posts: Collection,
) -> None:
    """Fetch, parse, diff against MongoDB, and notify for one search URL."""
    print(f"\n=== Watch target {target.label}: {target.url} ===")

    response = fetch_page(target.url, cookie, timeout)
    html = response.text

    with open(target.save_html, "w", encoding="utf-8") as fp:
        fp.write(html)

    if is_waf_challenge(html):
        raise WafChallengeError(
            f"AWS WAF challenge page received for target {target.label} "
            f"({target.url}). Refresh Lancers in Chrome, pass the captcha if "
            "shown, then copy a fresh Cookie header (must include "
            "aws-waf-token) into LANCERS_COOKIE."
        )

    parsed_jobs = extract_jobs(html, target.url)
    jobs = parsed_jobs[:JOB_COMPARE_LIMIT]
    new_jobs = filter_new_jobs(posts, jobs)
    saved = insert_new_jobs(
        posts,
        new_jobs,
        source_label=target.label,
        source_search_url=target.url,
    )

    print(f"HTTP {response.status_code} | final URL: {response.url}")
    print(f"Saved HTML: {target.save_html}")
    print(
        f"Parsed jobs: {len(parsed_jobs)} "
        f"(checking top {len(jobs)} against MongoDB)"
    )
    print(f"New jobs: {len(new_jobs)} (inserted {saved})")

    if not jobs:
        print(
            "Warning: 0 jobs parsed. HTML was saved — selectors may need updating, "
            "or the cookie may not include a valid logged-in / WAF session."
        )

    if not new_jobs:
        print("No new jobs detected. Telegram send skipped for this target.")
        return

    token_env = (
        "TELEGRAM_BOT_TOKEN" if target.label == "1" else "TELEGRAM_BOT_TOKEN1"
    )
    if not target.telegram_token:
        print(f"Telegram send skipped. {token_env} is not set.")
        return

    chat_ids = get_startup_chat_ids(
        target.telegram_token, target.chat_cache_file, timeout
    )
    if not chat_ids:
        print(
            f"No Telegram target chat found yet for bot {target.label}. "
            "Open that bot and press Start once, then run the script again "
            "for automatic delivery."
        )
        return

    total = len(new_jobs)
    enriched_jobs: List[Job] = []
    for idx, job in enumerate(new_jobs, start=1):
        enriched, detail_error = enrich_job_from_detail(
            job,
            cookie,
            timeout,
            referer=target.url,
        )
        if detail_error:
            print(
                f"Detail enrichment skipped for {job.url}: {detail_error}. "
                "Sending search-list fields only."
            )
        else:
            print(f"Enriched detail for {job.url}")
        enriched_jobs.append(enriched)
        if idx < total:
            time.sleep(0.8)

    for chat_id in chat_ids:
        upsert_cached_chat_id(target.chat_cache_file, chat_id)
        for job in enriched_jobs:
            message = render_telegram_job_html(job)
            keyboard = build_telegram_job_keyboard(job)
            send_to_telegram(
                target.telegram_token,
                chat_id,
                message,
                timeout,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    print(
        f"Auto-sent {total} new job message(s) via bot {target.label} "
        f"to {len(chat_ids)} chat(s): {', '.join(chat_ids)} "
        f"(cache: {target.chat_cache_file})"
    )


def main() -> None:
    load_dotenv()
    env_poll = os.getenv("POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL_SEC))

    parser = argparse.ArgumentParser(
        description=(
            "Continuously monitor Lancers search pages and push new jobs to "
            "Telegram. Monitors LANCERS_URL (TELEGRAM_BOT_TOKEN) then "
            "LANCERS_URL1 (TELEGRAM_BOT_TOKEN1) each cycle."
        )
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Monitor only this single search URL (skips LANCERS_URL1). "
            "Default: use LANCERS_URL and optional LANCERS_URL1 from .env"
        ),
    )
    parser.add_argument(
        "--cookie",
        default=None,
        help=(
            "Raw Cookie header value. Default: LANCERS_COOKIE from .env "
            "(reloaded every cycle)"
        ),
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="Request timeout (seconds)"
    )
    parser.add_argument(
        "--save-html",
        default="lancers_search.html",
        help=(
            "Path to save fetched HTML for the first URL "
            "(second URL uses a parallel *_1.html name)"
        ),
    )
    parser.add_argument(
        "--save-json",
        default="lancers_jobs.json",
        help=(
            "Legacy JSON snapshot path used only to seed MongoDB when the "
            "collection is empty (second file uses a parallel *_1.json name)"
        ),
    )
    parser.add_argument(
        "--telegram-token",
        default=None,
        help="Telegram bot token for URL 1 (default: TELEGRAM_BOT_TOKEN in .env)",
    )
    parser.add_argument(
        "--telegram-token1",
        default=None,
        help="Telegram bot token for URL 2 (default: TELEGRAM_BOT_TOKEN1 in .env)",
    )
    parser.add_argument(
        "--chat-cache-file",
        default=DEFAULT_CHAT_CACHE_FILE,
        help=(
            "Chat ID cache for bot 1 (second bot uses a parallel *_1.json name)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=(
            f"Seconds to wait between monitor cycles "
            f"(default: POLL_INTERVAL or {DEFAULT_POLL_INTERVAL_SEC})"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single monitor cycle and exit (disable continuous mode)",
    )
    args = parser.parse_args()

    try:
        poll_interval = (
            args.interval
            if args.interval is not None
            else int(str(env_poll).strip() or DEFAULT_POLL_INTERVAL_SEC)
        )
    except ValueError:
        poll_interval = DEFAULT_POLL_INTERVAL_SEC
    if poll_interval < 1:
        poll_interval = DEFAULT_POLL_INTERVAL_SEC

    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017").strip()
    mongo_db = os.getenv("MONGODB_DB", "lancers").strip() or "lancers"
    mongo_collection = (
        os.getenv("MONGODB_COLLECTION", "post").strip() or "post"
    )

    try:
        posts = get_mongo_collection(mongo_uri, mongo_db, mongo_collection)
    except PyMongoError as exc:
        raise SystemExit(
            f"MongoDB connection failed ({mongo_uri} → {mongo_db}.{mongo_collection}): "
            f"{exc}"
        ) from exc

    seeded = seed_mongo_from_json_files(
        posts,
        [args.save_json, _parallel_path(args.save_json)],
    )
    if seeded:
        print(
            f"Seeded {seeded} existing job URL(s) from legacy JSON into "
            f"{mongo_db}.{mongo_collection}."
        )

    mode = "once" if args.once else "continuous"
    print(
        f"Lancers monitor starting ({mode}). "
        f"MongoDB: {mongo_db}.{mongo_collection}. "
        f"Poll interval: {poll_interval}s. Ctrl+C to stop."
    )

    cycle = 0
    try:
        while True:
            cycle += 1
            load_dotenv(override=True)

            cookie = (args.cookie if args.cookie is not None else os.getenv("LANCERS_COOKIE", "")).strip()
            if cookie.startswith('"') and cookie.endswith('"'):
                cookie = cookie[1:-1].strip()

            if not cookie:
                print(
                    "LANCERS_COOKIE is missing. Waiting for .env update before "
                    f"retrying in {poll_interval}s..."
                )
                if args.once:
                    raise SystemExit(
                        "LANCERS_COOKIE is required. Lancers uses AWS WAF — copy "
                        "the full Cookie header from Chrome DevTools into .env."
                    )
                time.sleep(poll_interval)
                continue

            targets = collect_watch_targets(
                env_url=os.getenv("LANCERS_URL", DEFAULT_URL),
                env_url1=os.getenv("LANCERS_URL1", ""),
                cli_url=args.url,
                save_html=args.save_html,
                save_json=args.save_json,
                telegram_token=(
                    args.telegram_token
                    if args.telegram_token is not None
                    else os.getenv("TELEGRAM_BOT_TOKEN", "")
                ),
                telegram_token1=(
                    args.telegram_token1
                    if args.telegram_token1 is not None
                    else os.getenv("TELEGRAM_BOT_TOKEN1", "")
                ),
                chat_cache_file=args.chat_cache_file,
            )

            print(
                f"\n======== Cycle {cycle} | "
                f"monitoring {len(targets)} URL(s) ========"
            )

            waf_hit = False
            for target in targets:
                try:
                    process_watch_target(
                        target=target,
                        cookie=cookie or None,
                        timeout=args.timeout,
                        posts=posts,
                    )
                except WafChallengeError as exc:
                    print(f"WAF challenge: {exc}")
                    print(
                        "Update LANCERS_COOKIE in .env after refreshing the "
                        f"browser session. Retrying in {poll_interval}s..."
                    )
                    waf_hit = True
                    break
                except requests.RequestException as exc:
                    print(
                        f"Request error on target {target.label}: {exc}. "
                        "Continuing with remaining targets."
                    )
                except PyMongoError as exc:
                    print(
                        f"MongoDB error on target {target.label}: {exc}. "
                        "Continuing with remaining targets."
                    )

            if args.once:
                break

            wait_for = poll_interval
            if waf_hit:
                wait_for = max(poll_interval, 120)
            print(f"\nCycle {cycle} done. Next check in {wait_for}s...")
            time.sleep(wait_for)
    except KeyboardInterrupt:
        print("\nStopped by user. Exiting.")


if __name__ == "__main__":
    main()
