#!/usr/bin/env python3
"""
Fetch Lancers job search results with browser-like HTTP requests.

Example:
  python lancers_fetch.py ^
    --url "https://www.lancers.jp/work/search/system?open=1&ref=header_menu" ^
    --cookie "aws-waf-token=...; ..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


DEFAULT_URL = (
    "https://www.lancers.jp/work/search/system?open=1&ref=header_menu"
)
DEFAULT_CHAT_CACHE_FILE = "telegram_chat_ids.json"
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


@dataclass
class Job:
    title: str
    url: str
    budget: Optional[str]
    applicants: Optional[str]
    deadline: Optional[str]
    summary: Optional[str]
    skills: List[str]


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


def is_waf_challenge(page_html: str) -> bool:
    markers = (
        "Human Verification",
        "awsWafCookieDomainList",
        "CaptchaScript.renderCaptcha",
        "AwsWafIntegration",
    )
    return any(marker in page_html for marker in markers)


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

    # Strip common badge prefixes glued into title text.
    title = re.sub(r"^(NEW|初回|注目|NEW初回|NEW初回注目)\s*", "", title).strip()

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

        title = re.sub(r"^(NEW|初回|注目|NEW初回|NEW初回注目)\s*", "", title).strip()

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


def fetch_page(url: str, cookie_header: Optional[str], timeout: int) -> requests.Response:
    session = requests.Session()
    session.headers.update(build_headers(referer=url))

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


def render_telegram_job_html(
    job: Job,
    source_url: str,
    index: Optional[int] = None,
    total: Optional[int] = None,
) -> str:
    title = job.title or ""
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )

    if total is not None and index is not None:
        header = f"Lancers job update ({index}/{total})"
    else:
        header = "Lancers job update"

    lines: List[str] = [f"<b>{header}</b>", f"<i>{source_url}</i>", ""]
    lines.append(f"<b>{safe_title}</b>")
    if job.url:
        lines.append(f"URL: {job.url}")
    if job.budget:
        lines.append(f"Budget: {job.budget}")
    if job.deadline:
        lines.append(f"Deadline: {job.deadline}")
    if job.applicants:
        lines.append(job.applicants)
    if job.skills:
        lines.append(f"Skills: {', '.join(job.skills[:8])}")

    if job.summary:
        snippet = job.summary.replace("\n", " ").strip()
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        safe_summary = (
            snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        lines.append(f"\n<b>Summary</b>\n{safe_summary}")

    return "\n".join(lines).strip()


def build_telegram_job_keyboard(job: Job) -> Dict[str, object]:
    # "Bid" is intentionally non-automated; user must act manually on the job page.
    return {
        "inline_keyboard": [
            [{"text": "View Details", "url": job.url}],
            [{"text": "Bid", "url": job.url}],
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


def load_existing_jobs(path: str) -> List[Job]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
        if not isinstance(payload, list):
            return []
        jobs: List[Job] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            jobs.append(
                Job(
                    title=str(row.get("title", "")),
                    url=str(row.get("url", "")),
                    budget=row.get("budget"),
                    applicants=row.get("applicants"),
                    deadline=row.get("deadline"),
                    summary=row.get("summary"),
                    skills=row.get("skills") if isinstance(row.get("skills"), list) else [],
                )
            )
        return jobs
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def get_new_jobs(previous_jobs: List[Job], latest_jobs: List[Job]) -> List[Job]:
    existing_urls = {job.url for job in previous_jobs if job.url}
    return [job for job in latest_jobs if job.url and job.url not in existing_urls]


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


def main() -> None:
    load_dotenv()
    env_url = os.getenv("LANCERS_URL", DEFAULT_URL)
    env_cookie = os.getenv("LANCERS_COOKIE", "")
    env_telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    parser = argparse.ArgumentParser(
        description="Fetch Lancers search result page like browser."
    )
    parser.add_argument("--url", default=env_url, help="Target search URL")
    parser.add_argument(
        "--cookie",
        default=env_cookie,
        help="Raw Cookie header value copied from browser DevTools (or .env)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="Request timeout (seconds)"
    )
    parser.add_argument(
        "--save-html",
        default="lancers_search.html",
        help="Path to save fetched HTML",
    )
    parser.add_argument(
        "--save-json",
        default="lancers_jobs.json",
        help="Path to save parsed jobs as JSON",
    )
    parser.add_argument(
        "--telegram-token",
        default=env_telegram_token,
        help="Telegram bot token (or TELEGRAM_BOT_TOKEN in .env)",
    )
    parser.add_argument(
        "--bot-mode",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--chat-cache-file",
        default=DEFAULT_CHAT_CACHE_FILE,
        help="Path to cache Telegram chat IDs for automatic startup push.",
    )
    args = parser.parse_args()

    if not (args.cookie or "").strip():
        raise SystemExit(
            "LANCERS_COOKIE is required. Lancers uses AWS WAF — copy the full "
            "Cookie header from Chrome DevTools (Network → document request → "
            "Request Headers → Cookie), paste it into .env, then retry."
        )

    response = fetch_page(args.url, args.cookie or None, args.timeout)
    html = response.text

    with open(args.save_html, "w", encoding="utf-8") as fp:
        fp.write(html)

    if is_waf_challenge(html):
        raise SystemExit(
            "AWS WAF challenge page received instead of job listings. "
            "Refresh Lancers in Chrome, pass the captcha if shown, then copy a "
            "fresh Cookie header (must include aws-waf-token) into LANCERS_COOKIE."
        )

    previous_jobs = load_existing_jobs(args.save_json)
    jobs = extract_jobs(html, args.url)
    new_jobs = get_new_jobs(previous_jobs, jobs)
    with open(args.save_json, "w", encoding="utf-8") as fp:
        json.dump([asdict(job) for job in jobs], fp, ensure_ascii=False, indent=2)

    print(f"HTTP {response.status_code} | final URL: {response.url}")
    print(f"Saved HTML: {args.save_html}")
    print(f"Parsed jobs: {len(jobs)}")
    print(f"New jobs: {len(new_jobs)}")
    print(f"Saved JSON: {args.save_json}")

    if not jobs:
        print(
            "Warning: 0 jobs parsed. HTML was saved — selectors may need updating, "
            "or the cookie may not include a valid logged-in / WAF session."
        )

    if not new_jobs:
        print("No new jobs detected. Telegram send skipped.")
        return

    if not args.telegram_token:
        print("Telegram send skipped. TELEGRAM_BOT_TOKEN is not set.")
        return

    chat_ids = get_startup_chat_ids(args.telegram_token, args.chat_cache_file, args.timeout)
    if not chat_ids:
        print(
            "No Telegram target chat found yet. Open your bot and press Start once, "
            "then run the script again for automatic delivery."
        )
        return

    total = len(new_jobs)
    for chat_id in chat_ids:
        upsert_cached_chat_id(args.chat_cache_file, chat_id)
        for idx, job in enumerate(new_jobs, start=1):
            message = render_telegram_job_html(job, args.url, index=idx, total=total)
            keyboard = build_telegram_job_keyboard(job)
            send_to_telegram(
                args.telegram_token,
                chat_id,
                message,
                args.timeout,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    print(
        f"Auto-sent {total} new job message(s) per chat to {len(chat_ids)} chat(s): "
        f"{', '.join(chat_ids)}"
    )


if __name__ == "__main__":
    main()
