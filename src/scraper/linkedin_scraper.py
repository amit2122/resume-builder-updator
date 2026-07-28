"""LinkedIn job scraper using Playwright.

Exposes scrape_all_jobs(config) as the primary entry point.
Credentials are read from LINKED_IN_USERNAME and LINKED_IN_PASSWORD in .env.
"""

import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from src.core.logger import logger

load_dotenv()

LINKEDIN_EMAIL = os.getenv("LINKED_IN_USERNAME")
LINKEDIN_PASSWORD = os.getenv("LINKED_IN_PASSWORD")


def get_env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_list_env(name, default):
    """Parse a comma-separated env var into a list; returns [default] when unset."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return [default] if default else []
    return [item.strip() for item in value.split(",") if item.strip()]


SEARCH_CONFIG = {
    "job_titles": parse_list_env("JOB_TITLE_FILTER", "GenAI Engineer"),
    "keywords_list": parse_list_env("LINKEDIN_KEYWORDS", "GenAI Engineer"),
    "locations": parse_list_env("LINKEDIN_LOCATION", "India"),
    "geo_ids": parse_list_env("LINKEDIN_GEO_ID", "102713980"),
    "distance": get_env_int("LINKEDIN_DISTANCE", 25),
    "sort_by": os.getenv("LINKEDIN_SORT_BY", "DD").strip(),
    "posted_within_seconds": os.getenv("LINKEDIN_POSTED_WITHIN", "r86400").strip(),
    "max_jobs": get_env_int("MAX_JOBS", 20),
    "page_size": get_env_int("LINKEDIN_PAGE_SIZE", 25),
    "max_pages": get_env_int("LINKEDIN_MAX_PAGES", 20),
    "headless": get_env_bool("PLAYWRIGHT_HEADLESS", True),
}

_DATA_DIR = Path("data/Scraped Jobs")
_DATA_DIR.mkdir(parents=True, exist_ok=True)
Path("data/debug").mkdir(parents=True, exist_ok=True)

OUTPUT_JSON = os.getenv("OUTPUT_JSON", str(_DATA_DIR / "linkedin_jobs.json"))
OUTPUT_CSV = os.getenv("OUTPUT_CSV", str(_DATA_DIR / "linkedin_jobs.csv"))
DEBUG_HTML_FILE = os.getenv("DEBUG_HTML_FILE", "data/debug/debug_page.html")
DETAIL_DEBUG_HTML_FILE = os.getenv("DETAIL_DEBUG_HTML_FILE", "data/debug/debug_job_detail.html")

RESULTS_READY_SELECTOR = (
    "li.scaffold-layout__list-item[data-occludable-job-id], "
    "div.job-card-container[data-job-id], "
    "div.base-search-card, "
    "div.base-card.job-search-card, "
    ".jobs-search-results-list"
)

DETAIL_READY_SELECTOR = (
    ".jobs-description, "
    ".show-more-less-html__markup, "
    ".description__text, "
    ".job-details-jobs-unified-top-card__job-title, "
    ".top-card-layout__title"
)


def build_search_url(config, keywords=None, location=None, geo_id=None):
    params = {
        "keywords": keywords or (config["keywords_list"][0] if config["keywords_list"] else ""),
        "geoId": geo_id or (config["geo_ids"][0] if config["geo_ids"] else ""),
        "distance": config["distance"],
        "sortBy": config["sort_by"],
    }
    if config["posted_within_seconds"]:
        params["f_TPR"] = config["posted_within_seconds"]
    loc = location or (config["locations"][0] if config["locations"] else "")
    if loc:
        params["location"] = loc
    return f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"


JOBS_SEARCH_URL = build_search_url(SEARCH_CONFIG)


def validate_credentials():
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        raise ValueError("Set LINKED_IN_USERNAME and LINKED_IN_PASSWORD in .env")
    logger.info("[SCRAPER] LinkedIn credentials found for: %s", LINKEDIN_EMAIL)


def wait_for_jobs_shell(page, timeout=30000):
    try:
        page.wait_for_selector(
            "body, main, .application-outlet",
            timeout=timeout,
            state="attached",
        )
        page.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeout:
        with open(DEBUG_HTML_FILE, "w", encoding="utf-8") as file_handle:
            file_handle.write(page.content())
        print(f"Page shell wait timed out. Saved HTML -> {DEBUG_HTML_FILE}")


def login(page):
    logger.info("[SCRAPER] Opening LinkedIn...")
    page.goto(JOBS_SEARCH_URL, wait_until="domcontentloaded")
    time.sleep(3)

    if "linkedin.com/login" not in page.url and "checkpoint" not in page.url:
        logger.info("[SCRAPER] LinkedIn session already active.")
        wait_for_jobs_shell(page, timeout=30000)
        return

    logger.info("[SCRAPER] Logging in to LinkedIn...")
    username_selector = "#username, input[name='session_key']"
    password_selector = "#password, input[name='session_password']"

    try:
        page.wait_for_selector(username_selector, timeout=15000)
        page.fill(username_selector, LINKEDIN_EMAIL)
        page.fill(password_selector, LINKEDIN_PASSWORD)
        page.click("button[type='submit']")
        wait_for_jobs_shell(page, timeout=30000)
        logger.info("[SCRAPER] Login successful.")
    except PlaywrightTimeout:
        logger.warning("[SCRAPER] Security check detected. Waiting 60s for manual completion...")
        wait_for_jobs_shell(page, timeout=60000)
        logger.info("[SCRAPER] Login completed.")


def count_loaded_cards(page):
    return page.evaluate(
        """() => {
        return document.querySelectorAll(
            'li.scaffold-layout__list-item[data-occludable-job-id], ' +
            'div.job-card-container[data-job-id], ' +
            'div.base-search-card, ' +
            'div.base-card.job-search-card'
        ).length;
    }"""
    )


def scroll_to_load(page):
    panel = page.query_selector(".scaffold-layout__list")
    previous_count = -1
    stable_rounds = 0

    while stable_rounds < 3:
        if panel:
            page.evaluate("el => { el.scrollTop = el.scrollHeight; }", panel)
        else:
            page.mouse.wheel(0, 3000)
        time.sleep(1.5)

        current_count = count_loaded_cards(page)
        if current_count == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous_count = current_count


def clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(value):
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()


def title_matches_filter(job_title, required_titles):
    """Return True if any filter term appears anywhere in the job title (substring match).

    E.g. filter 'Gen AI Engineer' matches 'Senior Gen AI Engineer' and
    'Gen AI Engineer - Lead', but not 'AI / ML Engineer'.
    """
    if not required_titles:
        return True
    normalized_job = normalize_title(job_title)
    return any(normalize_title(t) in normalized_job for t in required_titles)


def extract_description_sections(description_text):
    if not description_text:
        return {}

    aliases = {
        "key responsibilities": "key_responsibilities",
        "responsibilities": "key_responsibilities",
        "roles and responsibilities": "key_responsibilities",
        "role and responsibilities": "key_responsibilities",
        "what you will do": "key_responsibilities",
        "what you'll do": "key_responsibilities",
        "duties": "key_responsibilities",
        "required skills": "required_skills",
        "skills": "required_skills",
        "requirements": "required_skills",
        "key requirements": "required_skills",
        "qualifications": "qualifications",
        "preferred qualifications": "preferred_qualifications",
        "experience": "experience",
        "about the role": "about_the_role",
        "job summary": "job_summary",
        "benefits": "benefits",
    }

    lines = [line.strip(" -:\t") for line in description_text.splitlines() if line.strip()]
    sections = {}
    current_key = None

    for line in lines:
        normalized = re.sub(r"\s+", " ", line.lower()).strip(" :")
        mapped = aliases.get(normalized)
        if mapped:
            current_key = mapped
            sections.setdefault(current_key, [])
            continue
        if current_key:
            sections[current_key].append(line)

    return {
        key: "\n".join(values).strip()
        for key, values in sections.items()
        if values
    }


def extract_bullet_points(description_text):
    if not description_text:
        return []

    bullet_lines = []
    for raw_line in description_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if raw_line.lstrip().startswith(("-", "*", ".", "•")):
            bullet_lines.append(clean_text(line.lstrip("-* .•")))
            continue
        if re.match(r"^[0-9]+[.)]\s+", line):
            bullet_lines.append(clean_text(re.sub(r"^[0-9]+[.)]\s+", "", line)))

    return [line for line in bullet_lines if line]


def scrape_job_details(context, job):
    detail = dict(job)
    detail_page = context.new_page()

    try:
        try:
            detail_page.goto(job["url"], wait_until="commit")
            detail_page.wait_for_selector(DETAIL_READY_SELECTOR, timeout=15000)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            try:
                with open(DETAIL_DEBUG_HTML_FILE, "w", encoding="utf-8") as file_handle:
                    file_handle.write(detail_page.content())
            except Exception:
                pass  # page may still be navigating — skip debug dump
            detail["detail_error"] = (
                f"Detail page load failed: {exc}. Saved HTML -> {DETAIL_DEBUG_HTML_FILE}"
            )
            return detail

        detail_page.evaluate(
            """() => {
            const selectors = [
                'button[aria-label*="more" i]',
                'button[aria-label*="description" i]',
                '.show-more-less-html__button',
                'button.inline-show-more-text__button'
            ];
            selectors.forEach((selector) => {
                document.querySelectorAll(selector).forEach((button) => {
                    if (button instanceof HTMLElement && button.offsetParent !== null) {
                        button.click();
                    }
                });
            });
        }"""
        )
        time.sleep(1)

        detail_payload = detail_page.evaluate(
            """() => {
            const pickText = (selectors) => {
                for (const selector of selectors) {
                    const element = document.querySelector(selector);
                    if (element && element.innerText.trim()) {
                        return element.innerText.trim();
                    }
                }
                return null;
            };

            const pickAttr = (selectors, attr) => {
                for (const selector of selectors) {
                    const element = document.querySelector(selector);
                    if (element && element.getAttribute(attr)) {
                        return element.getAttribute(attr).trim();
                    }
                }
                return null;
            };

            const descriptionRoot = document.querySelector('.jobs-description__content')
                || document.querySelector('.show-more-less-html__markup')
                || document.querySelector('.description__text')
                || document.querySelector('.jobs-box__html-content');

            const criteria = {};
            document.querySelectorAll(
                '.description__job-criteria-item, ' +
                '.job-details-jobs-unified-top-card__job-insight, ' +
                '.job-criteria__item'
            ).forEach((item) => {
                const label = item.querySelector('.description__job-criteria-subheader')
                    || item.querySelector('.job-details-jobs-unified-top-card__job-insight-text-button')
                    || item.querySelector('h3');
                const value = item.querySelector('.description__job-criteria-text')
                    || item.querySelector('.job-details-jobs-unified-top-card__job-insight-view-model-secondary')
                    || item.querySelector('span');
                if (label && value) {
                    criteria[label.innerText.trim()] = value.innerText.trim();
                }
            });

            return {
                detail_title: pickText([
                    '.job-details-jobs-unified-top-card__job-title',
                    '.top-card-layout__title',
                    '.t-24.job-details-jobs-unified-top-card__job-title'
                ]),
                detail_company: pickText([
                    '.job-details-jobs-unified-top-card__company-name a',
                    '.topcard__org-name-link',
                    '.topcard__flavor a'
                ]),
                detail_location: pickText([
                    '.job-details-jobs-unified-top-card__primary-description-container .tvm__text',
                    '.topcard__flavor--bullet',
                    '.topcard__flavor'
                ]),
                detail_posted: pickAttr(['time'], 'datetime') || pickText(['time', '.posted-time-ago__text']),
                applicants: pickText([
                    '.jobs-unified-top-card__applicant-count',
                    '.num-applicants__caption',
                    '.jobs-unified-top-card__subtitle-secondary-grouping'
                ]),
                workplace_type: pickText([
                    '.job-details-jobs-unified-top-card__workplace-type',
                    '.jobs-unified-top-card__workplace-type'
                ]),
                description_text: descriptionRoot ? descriptionRoot.innerText.trim() : null,
                description_html: descriptionRoot ? descriptionRoot.innerHTML : null,
                criteria: criteria,
                insight: pickText([
                    '.job-details-jobs-unified-top-card__tertiary-description-container',
                    '.job-posting-benefits__text'
                ]),
                apply_url: window.location.href
            };
        }"""
        )

        detail.update(
            {
                "title": clean_text(detail_payload.get("detail_title")) or detail.get("title"),
                "company": clean_text(detail_payload.get("detail_company")) or detail.get("company"),
                "location": clean_text(detail_payload.get("detail_location")) or detail.get("location"),
                "posted": clean_text(detail_payload.get("detail_posted")) or detail.get("posted"),
                "applicants": clean_text(detail_payload.get("applicants")),
                "workplace_type": clean_text(detail_payload.get("workplace_type")),
                "insight": clean_text(detail_payload.get("insight")),
                "description": detail_payload.get("description_text"),
                "description_html": detail_payload.get("description_html"),
                "criteria": detail_payload.get("criteria") or {},
                "apply_url": detail_payload.get("apply_url") or detail.get("url"),
            }
        )

        detail["sections"] = extract_description_sections(detail.get("description"))
        detail["bullet_points"] = extract_bullet_points(detail.get("description"))
        detail["key_responsibilities"] = detail["sections"].get("key_responsibilities")
        detail["required_skills"] = detail["sections"].get("required_skills")
        detail["qualifications"] = detail["sections"].get("qualifications")
        detail["experience"] = detail["sections"].get("experience")
        detail["benefits"] = detail["sections"].get("benefits")

        if not detail["key_responsibilities"] and detail["bullet_points"]:
            detail["key_responsibilities"] = "\n".join(detail["bullet_points"])

        return detail
    finally:
        detail_page.close()


def get_jobs_on_page(page):
    try:
        page.wait_for_selector(RESULTS_READY_SELECTOR, timeout=10000)
    except PlaywrightTimeout:
        print("   No job cards found after waiting. Saving debug HTML...")
        with open(DEBUG_HTML_FILE, "w", encoding="utf-8") as file_handle:
            file_handle.write(page.content())
        print(f"   Debug HTML saved -> {DEBUG_HTML_FILE}")
        return []

    scroll_to_load(page)

    jobs = page.evaluate(
        """() => {
        const results = [];
        const cards = [
            ...document.querySelectorAll('li.scaffold-layout__list-item[data-occludable-job-id]'),
            ...document.querySelectorAll('div.job-card-container[data-job-id]'),
            ...document.querySelectorAll('div.base-search-card'),
            ...document.querySelectorAll('div.base-card.job-search-card'),
        ];

        const seen = new Set();

        cards.forEach((card) => {
            const root = card.matches('div.job-card-container[data-job-id]')
                ? card
                : card.querySelector('div.job-card-container[data-job-id]') || card;

            const linkEl = root.querySelector('a.job-card-container__link.job-card-list__title--link')
                || root.querySelector('a.job-card-container__link')
                || root.querySelector('a.base-card__full-link')
                || root.querySelector('a[href*="/jobs/view/"]');
            const titleEl = root.querySelector('.job-card-list__title')
                || root.querySelector('h3.base-search-card__title')
                || linkEl
                || root.querySelector('h3');
            const compEl = root.querySelector('.artdeco-entity-lockup__subtitle span')
                || root.querySelector('.job-card-container__primary-description')
                || root.querySelector('.job-card-container__company-name')
                || root.querySelector('h4.base-search-card__subtitle a')
                || root.querySelector('h4.base-search-card__subtitle')
                || root.querySelector('a.hidden-nested-link');
            const locEl = root.querySelector('.job-card-container__metadata-item')
                || root.querySelector('.job-card-list__metadata-wrapper li')
                || root.querySelector('span.job-search-card__location');
            const timeEl = root.querySelector('time');
            const insightEl = root.querySelector('.job-card-container__footer-item--highlighted')
                || root.querySelector('.job-card-container__footer-item')
                || root.querySelector('.job-posting-benefits__text');

            const href = linkEl ? linkEl.getAttribute('href') : null;
            if (!href) {
                return;
            }

            const url = href.startsWith('http')
                ? href.split('?')[0]
                : 'https://www.linkedin.com' + href.split('?')[0];

            if (seen.has(url)) {
                return;
            }
            seen.add(url);

            results.push({
                title: titleEl ? titleEl.innerText.trim() : null,
                url: url,
                company: compEl ? compEl.innerText.trim() : null,
                location: locEl ? locEl.innerText.trim() : null,
                posted: timeEl ? timeEl.getAttribute('datetime') : (insightEl ? insightEl.innerText.trim() : null),
            });
        });

        return results;
    }"""
    )

    return jobs


def get_total_jobs_count(page):
    element = page.query_selector(
        "span.results-context-header__job-count, h1.jobs-search-results-list__title"
    )
    if element:
        text = element.inner_text().replace(",", "").replace("+", "").strip().split()[0]
        if text.isdigit():
            return int(text)
    return 0


def save_results(job_rows):
    with open(OUTPUT_JSON, "w", encoding="utf-8") as file_handle:
        json.dump(job_rows, file_handle, indent=4, ensure_ascii=False)
    logger.info("[SCRAPER] JSON saved -> %s", OUTPUT_JSON)

    if job_rows:
        flat_rows = []
        fieldnames = []
        seen_fields = set()

        for row in job_rows:
            flat_row = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    flat_row[key] = json.dumps(value, ensure_ascii=False)
                else:
                    flat_row[key] = value
                if key not in seen_fields:
                    seen_fields.add(key)
                    fieldnames.append(key)
            flat_rows.append(flat_row)

        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as file_handle:
            writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat_rows)
        logger.info("[SCRAPER] CSV saved -> %s", OUTPUT_CSV)


def scrape_all_jobs(config=None):
    validate_credentials()
    active_config = config or SEARCH_CONFIG
    all_jobs = []
    seen_urls = set()
    skipped_title_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=active_config["headless"],
            args=["--window-size=1440,900"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        login(page)

        # Pair each location with its geo_id by index; fall back to first geo_id if fewer ids than locations
        geo_ids = active_config["geo_ids"]
        location_pairs = [
            (loc, geo_ids[i] if i < len(geo_ids) else geo_ids[0])
            for i, loc in enumerate(active_config["locations"])
        ]

        for keyword in active_config["keywords_list"]:
            if len(all_jobs) >= active_config["max_jobs"]:
                break

            for location, geo_id in location_pairs:
                if len(all_jobs) >= active_config["max_jobs"]:
                    break

                search_url = build_search_url(active_config, keywords=keyword, location=location, geo_id=geo_id)
                logger.info("[SCRAPER] === Keyword: %s | Location: %s (geoId=%s) ===", keyword, location, geo_id)
                logger.info("[SCRAPER] Titles filter: %s", active_config['job_titles'])
                logger.info("[SCRAPER] URL: %s", search_url)

                page.goto(search_url, wait_until="domcontentloaded")
                time.sleep(4)

                total = get_total_jobs_count(page)
                if total:
                    logger.info("[SCRAPER] LinkedIn reports %s total jobs.", total)

                for page_num in range(active_config["max_pages"]):
                    if len(all_jobs) >= active_config["max_jobs"]:
                        break

                    start_offset = page_num * active_config["page_size"]
                    paginated_url = f"{search_url}&start={start_offset}"
                    logger.info("[SCRAPER] Page %s (start=%s)", page_num + 1, start_offset)

                    if page_num > 0:
                        page.goto(paginated_url, wait_until="domcontentloaded")
                        time.sleep(3)

                    jobs = get_jobs_on_page(page)
                    logger.info("[SCRAPER] Extracted %s cards from this page.", len(jobs))

                    new_count = 0
                    for job in jobs:
                        if len(all_jobs) >= active_config["max_jobs"]:
                            break

                        url = job.get("url")
                        if not url or url in seen_urls:
                            continue

                        if not title_matches_filter(job.get("title"), active_config["job_titles"]):
                            skipped_title_count += 1
                            logger.info("[SCRAPER] Skip (title mismatch): %s", job.get('title'))
                            continue

                        seen_urls.add(url)
                        logger.info(
                            "[SCRAPER] [%s/%s] Fetching: %s @ %s",
                            len(all_jobs) + 1, active_config['max_jobs'],
                            job.get('title'), job.get('company'),
                        )
                        detailed_job = scrape_job_details(context, job)
                        all_jobs.append(detailed_job)
                        new_count += 1

                    logger.info(
                        "[SCRAPER] +%s matched | skipped %s by title | total %s/%s",
                        new_count, skipped_title_count, len(all_jobs), active_config['max_jobs'],
                    )

                    if new_count:
                        save_results(all_jobs)

                    if len(all_jobs) >= active_config["max_jobs"]:
                        logger.info("[SCRAPER] Reached target of %s jobs.", active_config['max_jobs'])
                        break

                    if not jobs:
                        logger.info("[SCRAPER] No more results for this keyword+location.")
                        break

                    if page_num > 0 and new_count == 0:
                        logger.info("[SCRAPER] No new title matches on this page. Stopping pagination.")
                        break

        browser.close()

    logger.info("[SCRAPER] Scraping complete. Total jobs collected: %s", len(all_jobs))
    return all_jobs
