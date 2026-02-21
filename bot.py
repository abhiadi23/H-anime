import asyncio
import re
import os
import json
import time
import glob
import uuid
from urllib.parse import unquote
from config import *
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

DOWNLOAD_DIR = "./downloads"
COOKIES_FILE = "./cookies.json"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def html_esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PM = enums.ParseMode.HTML
app = Client("hanime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def log(level: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level.ljust(5)}] {msg}")


# ─── URL VALIDATION ───────────────────────────────────────────────────────────

HANIME_CDN = re.compile(
    r'(hwcdn\.net|hanime\.tv|videodelivery\.net|mux\.com|'
    r'akamaized\.net|cloudfront\.net|fastly\.net|b-cdn\.net|hanime-cdn\.com)',
    re.IGNORECASE
)
VIDEO_EXT = re.compile(r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)', re.IGNORECASE)
PLAYER_DOMAIN = re.compile(r'player\.hanime\.tv', re.IGNORECASE)


def is_real_video_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if url.startswith("blob:"):
        return False
    if not HANIME_CDN.search(url):
        return False
    if not VIDEO_EXT.search(url):
        return False
    return True


# ─── DRIVER ───────────────────────────────────────────────────────────────────

def build_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-sync")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-software-rasterizer")
    return uc.Chrome(options=options)


# ─── WAY 2: XHR/FETCH RESPONSE INTERCEPTION ──────────────────────────────────
# Injected into player iframe via CDP before page loads.
# After play is clicked, the player sends a request to backend.
# This catches the CDN URL from the actual response that comes back.

RESPONSE_TRACKER_JS = r"""
if (!window.__cdn_urls) window.__cdn_urls = [];

// Intercept XHR — capture response URL when backend replies with video content
(function() {
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url) {
        this.__req_url = url;
        return origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(body) {
        var xhr = this;
        xhr.addEventListener('readystatechange', function() {
            if (xhr.readyState === 4 && xhr.status >= 200 && xhr.status < 300) {
                var url = xhr.__req_url || '';
                var ct  = xhr.getResponseHeader('Content-Type') || '';
                var isVideoUrl = /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url);
                var isVideoCt  = ct.includes('video') || ct.includes('mpegurl') || ct.includes('octet-stream');
                if (isVideoUrl || isVideoCt) {
                    window.__cdn_urls.push({url: url, via: 'xhr', status: xhr.status, ct: ct});
                }
            }
        });
        return origSend.apply(this, arguments);
    };
})();

// Intercept fetch — capture response URL when backend replies with video content
(function() {
    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        return origFetch.apply(this, arguments).then(function(resp) {
            try {
                var ct = resp.headers.get('Content-Type') || '';
                var isVideoUrl = /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url);
                var isVideoCt  = ct.includes('video') || ct.includes('mpegurl') || ct.includes('octet-stream');
                if (resp.ok && (isVideoUrl || isVideoCt)) {
                    window.__cdn_urls.push({url: url, via: 'fetch', status: resp.status, ct: ct});
                }
            } catch(e) {}
            return resp;
        });
    };
})();
"""


# ─── WAY 1: DECODE CDN URL FROM PLAYER IFRAME SRC ────────────────────────────
# From logs: iframe src = https://player.hanime.tv/?&#v2,3014,slug,https%3A%2F%2F...
# The CDN URL is URL-encoded inside the iframe src as the last parameter.
# Decode it and extract directly — works immediately after iframe appears,
# no need to wait for any network request.

def way1_extract_from_iframe_src(driver) -> str | None:
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            src = frame.get_attribute("src") or ""
            if not PLAYER_DOMAIN.search(src):
                continue

            log("INFO", f"Way1 — player iframe src: {src[:150]}")

            # Decode the full src URL
            decoded = unquote(src)
            log("INFO", f"Way1 — decoded: {decoded[:200]}")

            # Extract all http URLs embedded inside the decoded string
            candidates = re.findall(r'https?://[^\s,&#\'"<>]+', decoded)
            for url in candidates:
                url = url.rstrip(".,;)")
                log("INFO", f"Way1 — candidate: {url[:120]}")
                if is_real_video_url(url):
                    log("HIT", f"Way1 iframe src decode: {url[:120]}")
                    return url

    except Exception as e:
        log("WARN", f"Way1 error: {e}")
    return None


# ─── WAY 2: READ XHR/FETCH RESPONSE FROM INSIDE PLAYER IFRAME ────────────────
# After clicking play, switch into player iframe and read window.__cdn_urls
# which was populated by the response tracker injected via CDP.

def way2_read_response_from_iframe(driver) -> str | None:
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            src = frame.get_attribute("src") or ""
            if not PLAYER_DOMAIN.search(src):
                continue

            driver.switch_to.frame(frame)
            log("INFO", "Way2 — switched into player iframe")

            try:
                raw = driver.execute_script("return window.__cdn_urls || [];")
                for item in (raw or []):
                    url = item.get("url", "") if isinstance(item, dict) else str(item)
                    via = item.get("via", "?") if isinstance(item, dict) else "?"
                    ct  = item.get("ct",  "") if isinstance(item, dict) else ""
                    st  = item.get("status", 0) if isinstance(item, dict) else 0
                    log("INFO", f"Way2 — response [{st}][{via}] ct={ct[:30]} url={url[:80]}")
                    if is_real_video_url(url):
                        log("HIT", f"Way2 XHR/fetch response: {url[:120]}")
                        driver.switch_to.default_content()
                        return url
            finally:
                driver.switch_to.default_content()

    except Exception as e:
        log("WARN", f"Way2 error: {e}")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return None


# ─── COOKIE LOADER ────────────────────────────────────────────────────────────

def load_cookies(driver, cookies_file: str) -> bool:
    if not os.path.exists(cookies_file):
        log("WARN", f"No cookies file at {cookies_file!r} — no login")
        return False
    try:
        with open(cookies_file) as f:
            cookies = json.load(f)
        driver.get("https://hanime.tv")
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        loaded = 0
        for c in cookies:
            try:
                clean = {"name": c["name"], "value": c["value"],
                         "domain": c.get("domain", ".hanime.tv"),
                         "path": c.get("path", "/")}
                if "secure" in c:
                    clean["secure"] = c["secure"]
                if "expirationDate" in c:
                    clean["expiry"] = int(c["expirationDate"])
                driver.add_cookie(clean)
                loaded += 1
            except Exception as e:
                log("WARN", f"Skipped cookie {c.get('name','?')}: {e}")
        log("INFO", f"Loaded {loaded}/{len(cookies)} cookies")
        return loaded > 0
    except Exception as e:
        log("ERROR", f"Cookie load failed: {e}")
        return False


# ─── PLAY BUTTON ──────────────────────────────────────────────────────────────

PLAY_SELECTORS = [
    "div.play-btn",
    ".play-btn",
    ".htv-video-player .play-btn",
    "[class*='play-btn']",
    ".vjs-big-play-button",
    "button[class*='play']",
    "[aria-label='Play Video']",
]


def click_play(driver) -> bool:
    for sel in PLAY_SELECTORS:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            log("HIT", f"Clicked play: {sel!r}")
            return True
        except Exception:
            continue
    return False


# ─── MAIN SCRAPER ─────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")
    result = {"title": "Unknown", "stream_url": None, "error": None}
    driver = build_driver()

    try:
        # Inject response tracker into ALL frames before any page JS runs
        # This covers the player iframe so it captures XHR/fetch responses from frame 0
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                {"source": RESPONSE_TRACKER_JS})
        log("INFO", "Response tracker injected via CDP")

        # Load cookies for login
        load_cookies(driver, COOKIES_FILE)

        # Re-inject after cookie navigation
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                {"source": RESPONSE_TRACKER_JS})

        # Navigate to video page
        driver.get(page_url)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
        log("INFO", f"Page loaded: {driver.title!r}")

        # Title
        try:
            el = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "h1, .video-title, [class*='title']")
                )
            )
            result["title"] = el.text.strip() or driver.title
        except Exception:
            result["title"] = driver.title
        result["title"] = re.sub(
            r'\s*[-|]\s*hanime\.tv.*$', '', result["title"], flags=re.IGNORECASE
        ).strip()

        # Scroll into view
        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.3);")

        # STEP 1: Wait for Vue to mount play button, then click
        log("INFO", "Waiting for play button (Vue mount)...")

        # Dump ALL clickable/interactive elements to find real play button selector
        time.sleep(3)  # let Vue hydrate
        try:
            dump = driver.execute_script("""
                var results = [];
                var all = document.querySelectorAll('button, [class*=play], [class*=Play], [role=button], video, .player, [class*=player], [class*=Player]');
                all.forEach(function(el) {
                    results.push({
                        tag: el.tagName,
                        cls: (el.className || '').substring(0, 80),
                        id: el.id || '',
                        role: el.getAttribute('role') || '',
                        visible: el.offsetParent !== null,
                        text: (el.innerText || '').substring(0, 30)
                    });
                });
                return results.slice(0, 30);
            """)
            for el in (dump or []):
                log("INFO", f"DOM: <{el['tag']}> cls={el['cls']!r} id={el['id']!r} visible={el['visible']} text={el['text']!r}")
        except Exception as e:
            log("WARN", f"DOM dump error: {e}")

        clicked = False
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.play-btn"))
            )
            log("INFO", "Play button found — clicking...")
            clicked = click_play(driver)
        except Exception:
            log("WARN", "Play button did not appear within 15s — trying anyway")
            clicked = click_play(driver)

        if not clicked:
            log("WARN", "Play button could not be clicked")

        # STEP 2: Wait for player iframe AFTER play click
        # iframe only appears in DOM after play is clicked
        log("INFO", "Waiting for player iframe to appear after click...")
        try:
            WebDriverWait(driver, 15).until(
                lambda d: any(
                    PLAYER_DOMAIN.search(f.get_attribute("src") or "")
                    for f in d.find_elements(By.TAG_NAME, "iframe")
                )
            )
            log("INFO", "Player iframe appeared")
        except Exception:
            log("WARN", "Player iframe did not appear within 15s")

        # ── STEP 3: WAY 1 — Extract CDN URL from iframe src (instant) ─────────
        # The iframe src itself contains the CDN URL encoded as a parameter.
        # This works immediately without waiting for any network response.
        cdn_url = way1_extract_from_iframe_src(driver)

        # ── STEP 4: WAY 2 — Read XHR/fetch response from player iframe ────────
        # After play click, player sends request to backend → gets CDN URL back.
        # We read it from window.__cdn_urls which our tracker populated.
        if not cdn_url:
            log("INFO", "Way1 found nothing — trying Way2 (XHR/fetch response)...")
            # Give player 2s to fire its backend request and get response
            time.sleep(2)
            cdn_url = way2_read_response_from_iframe(driver)

        result["stream_url"] = cdn_url
        log("INFO", f"Done — URL: {cdn_url[:100] if cdn_url else 'NOT FOUND'}")
        log("INFO", f"Title: {result['title']!r}")

    except Exception as e:
        result["error"] = str(e)
        log("ERROR", f"Scraper crashed: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return result


# ─── YT-DLP DOWNLOADER ────────────────────────────────────────────────────────
# yt-dlp is ONLY used for downloading the CDN URL we already found above.
# It does NOT do any URL extraction/scraping.

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
        "👋 <b>Hanime Downloader Bot</b>\n\n"
        "Usage: <code>/dl &lt;hanime.tv URL&gt;</code>",
        parse_mode=PM,
    )


@app.on_message(filters.command(["dl", "direct"]))
async def dl_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("❌ Usage: <code>/dl &lt;hanime.tv URL&gt;</code>", parse_mode=PM)
        return

    url = args[1].strip()
    if "hanime.tv" not in url:
        await message.reply_text("❌ Only hanime.tv URLs are supported.", parse_mode=PM)
        return

    status = await message.reply_text("🌐 Launching browser...", parse_mode=PM)

    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(
            loop.run_in_executor(None, scrape_video_url, url), timeout=120
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timed out.", parse_mode=PM)
        return
    except Exception as e:
        await status.edit_text(f"❌ Error:\n<code>{html_esc(e)}</code>", parse_mode=PM)
        return

    if data.get("error"):
        await status.edit_text(f"❌ Error:\n<code>{html_esc(data['error'])}</code>", parse_mode=PM)
        return

    cdn_url = data["stream_url"]
    title   = data["title"]

    if not cdn_url:
        await status.edit_text("❌ No CDN URL found. Login may be required.", parse_mode=PM)
        return

    await status.edit_text(
        f"✅ Found CDN URL\n<b>Title:</b> {html_esc(title)}\n\n⬇️ Downloading...",
        parse_mode=PM,
    )

    session_id = str(uuid.uuid4())
    file_path = await download_with_ytdlp(cdn_url, title, session_id, status)

    if not file_path or not os.path.exists(file_path):
        await status.edit_text(
            f"❌ Download failed.\n\nCDN URL:\n<code>{html_esc(cdn_url)}</code>",
            parse_mode=PM,
        )
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > 2000:
        await status.edit_text(f"❌ File too large ({size_mb:.1f} MB).", parse_mode=PM)
        return

    await status.edit_text(f"📤 Uploading {size_mb:.1f} MB...", parse_mode=PM)
    try:
        await client.send_video(
            chat_id=message.chat.id,
            video=file_path,
            caption=(
                f"🎬 <b>{html_esc(title)}</b>\n"
                f"📦 {size_mb:.1f} MB\n"
                f"🔗 {html_esc(url)}"
            ),
            parse_mode=PM,
            supports_streaming=True,
            reply_to_message_id=message.id,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed:\n<code>{html_esc(e)}</code>", parse_mode=PM)
    finally:
        try:
            import shutil
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    app.run()
