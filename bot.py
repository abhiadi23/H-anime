import asyncio
import re
import os
import time
import glob
import uuid
import random
from config import *
from pyrogram import Client, filters
from pyrogram.types import Message
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from seleniumwire import webdriver as wire_webdriver
from selenium_stealth import stealth
from python_ghost_cursor import create_cursor # pip install python-ghost-cursor

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("hanime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ─── LOGGER ───────────────────────────────────────────────────────────────────

def log(level: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level.ljust(5)}] {msg}")


# ─── URL FILTERS ──────────────────────────────────────────────────────────────

VIDEO_EXT = re.compile(r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)', re.IGNORECASE)

CDN_DOMAINS = re.compile(
    r'(cdn\d*\.hanime\.tv|hwcdn\.net|storage\.googleapis\.com|'
    r'cloudfront\.net|akamaized\.net|fastly\.net|b-cdn\.net|'
    r'videodelivery\.net|stream\.cloudflare\.com|stream\.mux\.com)',
    re.IGNORECASE
)

BLACKLIST = re.compile(
    r'(performance\.radar\.cloudflare\.com|cdnjs\.cloudflare\.com|'
    r'cdn\.jsdelivr\.net|google-analytics\.com|googletagmanager\.com|'
    r'doubleclick\.net|sentry\.io|newrelic\.com|analytics|tracking|'
    r'telemetry|metrics|beacon|\.js(\?|$))',
    re.IGNORECASE
)


def is_cdn_video(req) -> bool:
    url = req.url
    if not url.startswith("http") or BLACKLIST.search(url):
        return False
    if not (VIDEO_EXT.search(url) or CDN_DOMAINS.search(url)):
        return False
    if req.response and req.response.status_code not in (200, 206):
        return False
    try:
        cl = req.response.headers.get("Content-Length") if req.response else None
        if cl and int(cl) < 100_000:
            return False
    except (ValueError, TypeError):
        pass
    return True


# ─── DRIVER ───────────────────────────────────────────────────────────────────

def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    driver = wire_webdriver.Chrome(
        options=options,
        seleniumwire_options={"disable_encoding": True, "verify_ssl": False},
    )

    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    return driver


# ─── PLAY SELECTORS ───────────────────────────────────────────────────────────

PLAY_SELECTORS = [
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    "button.play-button",
    "[class*='play-button']",
    "[class*='PlayButton']",
    ".ytp-large-play-button",
    "[aria-label='Play']",
    "video",
]


def click_play(driver, cursor: GhostCursor, label: str = "main") -> bool:
    for sel in PLAY_SELECTORS:
        try:
            el = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            cursor.move_to(el)
            cursor.click(el)
            log("HIT", f"Play clicked [{label}] via {sel!r}")
            return True
        except Exception:
            continue
    return False


def force_play(driver, label: str = "main") -> None:
    try:
        driver.execute_script(
            "document.querySelectorAll('video').forEach(v => { v.muted=false; v.volume=1; v.play(); });"
        )
        log("INFO", f"JS force-play [{label}]")
    except Exception as e:
        log("WARN", f"JS force-play failed [{label}]: {e}")


# ─── CDN WAIT CONDITION ───────────────────────────────────────────────────────

class CDNRequestFound:
    def __call__(self, driver):
        return any(is_cdn_video(r) for r in driver.requests)


# ─── SCRAPER ──────────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")

    result = {"title": "Unknown", "stream_url": None, "download_urls": [], "error": None}
    driver = build_driver()
    cursor = GhostCursor(driver)

    try:
        driver.header_overrides = {
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://hanime.tv/",
        }

        # Load page
        driver.get(page_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        log("INFO", f"Page loaded: {driver.title!r}")

        # Wait for player element
        for sel in ["video", ".video-js", ".plyr", "[class*='player']", "iframe[src*='embed']"]:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                log("INFO", f"Player found: {sel!r}")
                break
            except Exception:
                continue

        # Title
        try:
            el = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "h1, .video-title, [class*='title']")
                )
            )
            result["title"] = el.text.strip() or driver.title
        except Exception:
            result["title"] = driver.title

        # Human scroll + ghost cursor random move
        cursor.move_to_random_point()
        time.sleep(random.uniform(1.0, 2.0))
        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.6);")
        time.sleep(random.uniform(0.8, 1.5))

        # Click play on main page
        click_play(driver, cursor, label="main")
        force_play(driver, label="main")
        time.sleep(random.uniform(1.0, 2.0))

        # iframes
        for idx, iframe in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
            try:
                WebDriverWait(driver, 5).until(
                    EC.frame_to_be_available_and_switch_to_it(iframe)
                )
                WebDriverWait(driver, 8).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                click_play(driver, cursor, label=f"iframe#{idx+1}")
                force_play(driver, label=f"iframe#{idx+1}")
                driver.switch_to.default_content()
            except Exception:
                driver.switch_to.default_content()

        driver.switch_to.default_content()

        # Wait for CDN URL via WebDriverWait
        log("INFO", "Waiting for CDN URL in network traffic (up to 30s)...")
        try:
            WebDriverWait(driver, 30, poll_frequency=0.5).until(CDNRequestFound())
            log("HIT", "CDN URL detected!")
        except Exception:
            log("WARN", "WebDriverWait timed out — collecting whatever was captured")

        # Extra settle for HLS segments
        time.sleep(5)

        # Collect all valid CDN URLs
        found = []
        for req in driver.requests:
            if is_cdn_video(req) and req.url not in found:
                found.append(req.url)
                log("HIT", f"CDN URL: {req.url}")

        # Prioritise m3u8 > mp4 > mkv > other
        ordered = (
            [u for u in found if ".m3u8" in u.lower()] +
            [u for u in found if ".mp4"  in u.lower()] +
            [u for u in found if ".mkv"  in u.lower()] +
            [u for u in found if not any(x in u.lower() for x in (".m3u8", ".mp4", ".mkv"))]
        )

        result["stream_url"]    = ordered[0] if ordered else None
        result["download_urls"] = ordered
        log("INFO", f"Done — {len(ordered)} CDN URL(s) | Title: {result['title']!r}")

    except Exception as e:
        result["error"] = str(e)
        log("ERROR", f"Scraper crashed: {e}")
    finally:
        driver.quit()

    return result


# ─── YT-DLP DOWNLOADER ────────────────────────────────────────────────────────

async def download_with_ytdlp(
    cdn_url: str, title: str, session_id: str, status_msg: Message
) -> str | None:
    safe_title = re.sub(r'[^\w\s-]', '', title)[:60].strip() or "video"
    session_dir = os.path.join(DOWNLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    output_template = os.path.join(session_dir, f"{safe_title}.%(ext)s")

    cmd = [
        "yt-dlp", cdn_url,
        "--output", output_template,
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--retries", "5",
        "--fragment-retries", "10",
        "--concurrent-fragments", "4",
        "--newline", "--progress", "--no-warnings",
        "--add-header", "Referer:https://hanime.tv/",
        "--add-header",
        "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    last_update = time.time()
    async for raw in process.stdout:
        line = raw.decode("utf-8", errors="ignore").strip()
        if "[download]" in line and time.time() - last_update > 5:
            try:
                await status_msg.edit_text(f"⬇️ Downloading...\n\n{line}")
                last_update = time.time()
            except Exception:
                pass

    await process.wait()
    if process.returncode != 0:
        return None

    files = sorted(
        glob.glob(os.path.join(session_dir, f"{safe_title}.*")),
        key=os.path.getmtime, reverse=True
    )
    return files[0] if files else None


# ─── BOT COMMANDS ─────────────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 **Hanime Downloader Bot**\n\n"
        "Usage: `/dl <hanime.tv URL>`\n\n"
        "Stack: selenium-wire · selenium-stealth · python-ghost-cursor · yt-dlp"
    )


@app.on_message(filters.command(["dl", "direct"]))
async def dl_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("❌ Usage: `/dl <hanime.tv URL>`")
        return

    url = args[1].strip()
    if "hanime.tv" not in url:
        await message.reply_text("❌ Only hanime.tv URLs are supported.")
        return

    status = await message.reply_text("🌐 Launching stealth Chrome... (~30–60s)")

    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(
            loop.run_in_executor(None, scrape_video_url, url), timeout=180
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timed out after 3 minutes.")
        return
    except Exception as e:
        await status.edit_text(f"❌ Scraper crashed:\n`{e}`")
        return

    if data.get("error"):
        await status.edit_text(f"❌ Error:\n`{data['error']}`")
        return

    stream_url = data["stream_url"]
    title      = data["title"]
    all_urls   = data["download_urls"]

    if not stream_url:
        await status.edit_text("❌ No CDN video URL found. Login may be required.")
        return

    await status.edit_text(
        f"✅ Found **{len(all_urls)}** CDN URL(s)\n"
        f"**Title:** {title}\n\n⬇️ Downloading..."
    )

    session_id = str(uuid.uuid4())
    file_path  = None

    for i, u in enumerate(all_urls, 1):
        file_path = await download_with_ytdlp(u, title, session_id, status)
        if file_path and os.path.exists(file_path):
            break
        if i < len(all_urls):
            await status.edit_text(f"⚠️ URL #{i} failed, trying #{i+1}...")

    if not file_path or not os.path.exists(file_path):
        await status.edit_text(f"❌ Download failed.\n\nStream URL:\n`{stream_url}`")
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if size_mb > 2000:
        await status.edit_text(
            f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 2000 MB.\n\n`{stream_url}`"
        )
        return

    await status.edit_text(f"📤 Uploading {size_mb:.1f} MB...")
    try:
        await client.send_video(
            chat_id=message.chat.id,
            video=file_path,
            caption=f"🎬 **{title}**\n📦 {size_mb:.1f} MB\n🔗 {url}",
            supports_streaming=True,
            reply_to_message_id=message.id,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed:\n`{e}`")
    finally:
        try:
            import shutil
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        except Exception:
            pass


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run()
