import asyncio
import os
import json
from datetime import datetime
from playwright.async_api import async_playwright
import aiohttp

# Configuration
URL = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "last_seen.json"

async def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing. Skipping message.")
        print(f"Message would be: {message}")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                print(f"Failed to send Telegram message: {await response.text()}")

def load_last_seen():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_last_seen(data):
    with open(STATE_FILE, 'w') as f:
        json.dump(data, f)

async def crawl():
    print(f"Starting crawl at {datetime.now()}")
    
    last_seen = load_last_seen()
    last_titles = last_seen.get("titles", [])
    current_titles = []
    new_posts = []

    async with async_playwright() as p:
        browser = None
        for browser_type in [p.chromium, p.webkit, p.firefox]:
            try:
                print(f"Trying to launch {browser_type.name}...")
                browser = await browser_type.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
                )
                print(f"Successfully launched {browser_type.name}")
                break
            except Exception as e:
                print(f"Failed to launch {browser_type.name}: {e}")
        
        if not browser:
            print("Could not launch any browser. Exiting.")
            return

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        try:
            # Go to the list page
            print(f"Navigating to {URL}...")
            await page.goto(URL, wait_until="networkidle", timeout=60000)
            
            # Find all links with go_view
            links = page.locator('a[href^="javascript:go_view"]')
            count = await links.count()
            print(f"Found {count} items on list page")

            candidate_ids = []
            for i in range(count):
                href = await links.nth(i).get_attribute("href")
                # href format: javascript:go_view(123456);
                import re
                match = re.search(r"go_view\((\d+)\)", href)
                if match:
                    candidate_ids.append(match.group(1))
            
            # Filter new IDs
            new_ids = [id for id in candidate_ids if id not in last_titles]
            print(f"New items to check: {len(new_ids)}")

            for id in new_ids:
                detail_url = f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn={id}"
                print(f"Checking detail page: {detail_url}")
                
                try:
                    await page.goto(detail_url, wait_until="networkidle", timeout=60000)
                    
                    # Extract Data using XPath or specific selectors to find table rows
                    # Extract Data using verified selectors
                    try:
                        # Wait for title to ensure page load
                        title_locator = page.locator("div.view_tit h3")
                        await title_locator.wait_for(timeout=5000)
                        title = await title_locator.inner_text()
                    except:
                        print("  Title verification failed, trying fallback")
                        title = await page.locator("h3").first.inner_text()

                    # Period (ID verified)
                    period_locator = page.locator("#rcptPeriod")
                    if await period_locator.count() > 0:
                        period = await period_locator.inner_text()
                    else:
                        period = "정보 없음"

                    # Age (XPath verified)
                    # //li[contains(., "대상연령")]//p[@class="txt"]
                    age_locator = page.locator('//li[contains(., "대상연령")]//p[@class="txt"]')
                    if await age_locator.count() > 0:
                        age = await age_locator.inner_text()
                    else:
                        age = "정보 없음"
                    
                    print(f"  Title: {title}")
                    print(f"  Period: {period}")
                    print(f"  Age: {age}")

                    # Filtering Logic
                    # Filter IN if Age contains "전체" or "40세" (assuming "만 40세 이상" covers it)
                    is_target_age = "전체" in age or "40세" in age
                    
                    if is_target_age:
                        new_posts.append({
                            "title": title,
                            "link": detail_url,
                            "period": period, 
                            "age": age
                        })
                        current_titles.append(id) # Add to processed list
                    else:
                        print(f"  Skipping: Age '{age}' does not match criteria")
                        current_titles.append(id) # Mark as seen even if skipped to avoid re-checking
                        
                except Exception as e:
                    print(f"  Error processing {detail_url}: {e}")
            
        except Exception as e:
            print(f"Error during crawl: {e}")
            await page.screenshot(path="error_screenshot.png")
            # Save debug only on error
            content = await page.content()
            with open("error_debug_page.html", "w") as f:
                f.write(content)
        finally:
            await browser.close()
    
    # Process new posts
    if new_posts:
        print(f"Found {len(new_posts)} new valid posts")
        for post in new_posts:
            msg = (
                f"[새로운 공고]\n"
                f"제목: {post['title']}\n"
                f"기간: {post['period']}\n"
                f"대상: {post['age']}\n"
                f"{post['link']}"
            )
            await send_telegram_message(msg)
    else:
        print("No new valid posts found.")

    # Update state
    # We store IDs now, not whole titles
    # Merge current seen IDs with old ones, keep top 100
    all_ids = list(set(current_titles + last_titles))
    save_last_seen({"titles": all_ids[:100]})


if __name__ == "__main__":
    asyncio.run(crawl())
