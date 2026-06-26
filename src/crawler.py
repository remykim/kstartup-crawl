from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page


BASE_URL = "https://www.k-startup.go.kr"
LIST_URL = f"{BASE_URL}/web/contents/bizpbanc-ongoing.do"
STATE_FILE = Path(os.environ.get("STATE_FILE", "last_seen.json"))
STATE_LIMIT = int(os.environ.get("STATE_LIMIT", "100"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class PostSnapshot:
    id: str
    title: str
    link: str
    period: str
    age: str
    is_target: bool
    signature: str
    checked_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def post_signature(title: str, period: str, age: str) -> str:
    content = "\n".join(
        [
            normalize_text(title),
            normalize_text(period),
            normalize_text(age),
        ]
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def detail_url(post_id: str) -> str:
    return f"{LIST_URL}?schM=view&pbancSn={post_id}"


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"version": 2, "posts": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read state file. Starting fresh: {exc}")
        return {"version": 2, "posts": {}}

    if "posts" in state:
        return state

    legacy_ids = state.get("titles", [])
    print(f"Migrating legacy state with {len(legacy_ids)} seen IDs.")
    return {
        "version": 2,
        "posts": {
            str(post_id): {
                "id": str(post_id),
                "signature": None,
                "is_target": False,
                "checked_at": None,
            }
            for post_id in legacy_ids
        },
    }


def save_state(posts: dict[str, dict[str, Any]], ordered_ids: list[str]) -> None:
    retained_ids = list(dict.fromkeys(ordered_ids + list(posts.keys())))[:STATE_LIMIT]
    retained_posts = {post_id: posts[post_id] for post_id in retained_ids if post_id in posts}
    state = {
        "version": 2,
        "updated_at": now_iso(),
        "posts": retained_posts,
    }

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


async def send_telegram_message(message: str) -> bool:
    import aiohttp

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing. Skipping message.")
        print(f"Message would be: {message}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                response_text = await response.text()
                print(f"Failed to send Telegram message ({response.status}): {response_text[:500]}")
                return False

    return True


async def launch_browser(playwright: Any) -> Browser | None:
    for browser_type in [playwright.chromium, playwright.webkit, playwright.firefox]:
        try:
            print(f"Trying to launch {browser_type.name}...")
            browser = await browser_type.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            print(f"Successfully launched {browser_type.name}")
            return browser
        except Exception as exc:
            print(f"Failed to launch {browser_type.name}: {exc}")

    return None


async def extract_candidate_ids(page: Page) -> list[str]:
    await page.goto(LIST_URL, wait_until="networkidle", timeout=60_000)

    links = page.locator('a[href^="javascript:go_view"]')
    count = await links.count()
    print(f"Found {count} items on list page")

    candidate_ids: list[str] = []
    for index in range(count):
        href = await links.nth(index).get_attribute("href")
        if not href:
            continue

        match = re.search(r"go_view\((\d+)\)", href)
        if match:
            candidate_ids.append(match.group(1))

    return list(dict.fromkeys(candidate_ids))


async def optional_text(page: Page, selector: str, fallback: str = "정보 없음") -> str:
    locator = page.locator(selector)
    if await locator.count() == 0:
        return fallback

    return normalize_text(await locator.first.inner_text())


async def first_available_text(page: Page, selectors: tuple[str, ...], timeout_ms: int = 5_000) -> str:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(timeout=timeout_ms)
            return normalize_text(await locator.inner_text())
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Could not find text with selectors {selectors}") from last_error


async def extract_post_snapshot(page: Page, post_id: str) -> PostSnapshot:
    link = detail_url(post_id)
    print(f"Checking detail page: {link}")
    await page.goto(link, wait_until="networkidle", timeout=60_000)

    title = await first_available_text(page, ("div.view_tit h3", ".view_tit h3", "h3"))
    period = await optional_text(page, "#rcptPeriod")
    age = await optional_text(page, '//li[contains(., "대상연령")]//p[@class="txt"]')
    is_target = "전체" in age or "40세" in age

    print(f"  Title: {title}")
    print(f"  Period: {period}")
    print(f"  Age: {age}")
    if not is_target:
        print(f"  Skipping notification: Age '{age}' does not match criteria")

    return PostSnapshot(
        id=post_id,
        title=title,
        link=link,
        period=period,
        age=age,
        is_target=is_target,
        signature=post_signature(title, period, age),
        checked_at=now_iso(),
    )


def classify_snapshot(
    snapshot: PostSnapshot,
    previous_posts: dict[str, dict[str, Any]],
) -> str | None:
    if not snapshot.is_target:
        return None

    previous = previous_posts.get(snapshot.id)
    if previous is None:
        return "new"

    notified_signature = previous.get("notified_signature")

    if notified_signature is None:
        return "new"
    if notified_signature != snapshot.signature:
        return "updated"

    return None


def snapshot_record(
    snapshot: PostSnapshot,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = asdict(snapshot)
    if previous:
        for key in ("notified_at", "notified_signature"):
            if key in previous:
                record[key] = previous[key]

    return record


def mark_notified(posts: dict[str, dict[str, Any]], snapshot: PostSnapshot) -> None:
    post = posts.get(snapshot.id)
    if not post:
        return

    post["notified_at"] = now_iso()
    post["notified_signature"] = snapshot.signature


def format_message(snapshot: PostSnapshot, event_type: str) -> str:
    heading = "[새로운 공고]" if event_type == "new" else "[수정된 공고]"
    return (
        f"{heading}\n"
        f"제목: {snapshot.title}\n"
        f"기간: {snapshot.period}\n"
        f"대상: {snapshot.age}\n"
        f"{snapshot.link}"
    )


async def crawl() -> None:
    from playwright.async_api import async_playwright

    print(f"Starting crawl at {datetime.now()}")

    state = load_state()
    previous_posts: dict[str, dict[str, Any]] = state.get("posts", {})
    next_posts = dict(previous_posts)
    notifications: list[tuple[str, PostSnapshot]] = []
    candidate_ids: list[str] = []

    async with async_playwright() as playwright:
        browser = await launch_browser(playwright)
        if not browser:
            print("Could not launch any browser. Exiting.")
            return

        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        try:
            print(f"Navigating to {LIST_URL}...")
            candidate_ids = await extract_candidate_ids(page)
            print(f"Items to check: {len(candidate_ids)}")

            for post_id in candidate_ids:
                try:
                    snapshot = await extract_post_snapshot(page, post_id)
                except Exception as exc:
                    print(f"  Error processing {detail_url(post_id)}: {exc}")
                    continue

                event_type = classify_snapshot(snapshot, previous_posts)
                if event_type:
                    notifications.append((event_type, snapshot))

                next_posts[post_id] = snapshot_record(snapshot, previous_posts.get(post_id))
        except Exception as exc:
            print(f"Error during crawl: {exc}")
            await page.screenshot(path="error_screenshot.png")
            content = await page.content()
            Path("error_debug_page.html").write_text(content, encoding="utf-8")
        finally:
            await browser.close()

    if notifications:
        print(f"Found {len(notifications)} notifications")
        for event_type, snapshot in notifications:
            if await send_telegram_message(format_message(snapshot, event_type)):
                mark_notified(next_posts, snapshot)
    else:
        print("No new or updated valid posts found.")

    save_state(next_posts, candidate_ids)


if __name__ == "__main__":
    asyncio.run(crawl())
