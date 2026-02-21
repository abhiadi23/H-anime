import asyncio
import re
import os
import json
import time
import glob
import uuid
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

AD_BLACKLIST = re.compile(
    r'(blankmp4s\.pages\.dev|adtng\.com|adnxs\.com|adsrvr\.org|advertising\.com|'
    r'ads\.yahoo\.com|moatads\.com|amazon-adsystem\.com|exoclick\.com|'
    r'trafficjunky\.net|traffichaus\.com|juicyads\.com|plugrush\.com|'
    r'tsyndicate\.com|etahub\.com|realsrv\.com|doubleclick\.net|'
    r'googletagmanager\.com|google-analytics\.com|creatives\.|ad-delivery\.)',
    re.IGNORECASE
)

HANIME_CDN = re.compile(
    r'(hwcdn\.net|hanime\.tv|videodelivery\.net|mux\.com|'
    r'akamaized\.net|cloudfront\.net|fastly\.net|b-cdn\.net)',
    re.IGNORECASE
)

VIDEO_EXT = re.compile(r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)', re.IGNORECASE)


def is_real_video_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if AD_BLACKLIST.search(url):
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
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-software-rasterizer")
    return uc.Chrome(options=options)


# ─── CDP NETWORK LISTENER ─────────────────────────────────────────────────────
# Enable CDP Network domain so we get responseReceived events for every request.
# This is far better than polling — we catch the URL the instant the backend
# responds to the player's stream request.

def enable_network_capture(driver) -> None:
    driver.execute_cdp_cmd("Network.enable", {})
    log("INFO", "CDP Network capture enabled")


def get_network_video_urls(driver) -> list[str]:
    """
    Read all network responses captured by CDP since Network.enable was called.
    Filters for real CDN video URLs from actual backend responses.
    """
    try:
        # CDP logs are accessible via driver's internal log buffer
        logs = driver.get_log("performance")
    except Exception as e:
        log("WARN", f"get_log error: {e}")
        return []

    found = []
    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            method = msg.get("method", "")

            # We care about:
            # Network.responseReceived  — server responded (has the URL)
            # Network.requestWillBeSent — request was made (also has URL)
            if method in ("Network.responseReceived", "Network.requestWillBeSent"):
                params = msg.get("params", {})
                url = (
                    params.get("response", {}).get("url") or   # responseReceived
                    params.get("request",  {}).get("url") or   # requestWillBeSent
                    params.get("redirectResponse", {}).get("url") or
                    ""
                )
                if url and is_real_video_url(url) and url not in found:
                    found.append(url)
                    log("HIT", f"Network [{method.split('.')[-1]}]: {url[:100]}")

        except Exception:
            continue

    return found


# ─── COOKIE LOADER ────────────────────────────────────────────────────────────

def load_cookies(driver, cookies_file: str) -> bool:
    if not os.path.exists(cookies_file):
        log("WARN", f"No cookies file at {cookies_file!r} — proceeding without login")
        return False
    try:
        with open(cookies_file, "r") as f:
            cookies = json.load(f)

        driver.get("https://hanime.tv")
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        loaded = 0
        for cookie in cookies:
            try:
                clean = {
                    "name":   cookie["name"],
                    "value":  cookie["value"],
                    "domain": cookie.get("domain", ".hanime.tv"),
                    "path":   cookie.get("path", "/"),
                }
                if "secure" in cookie:
                    clean["secure"] = cookie["secure"]
                if "expirationDate" in cookie:
                    clean["expiry"] = int(cookie["expirationDate"])
                elif "expiry" in cookie:
                    clean["expiry"] = int(cookie["expiry"])
                driver.add_cookie(clean)
                loaded += 1
            except Exception as e:
                log("WARN", f"Skipped cookie {cookie.get('name','?')}: {e}")

        log("INFO", f"Loaded {loaded}/{len(cookies)} cookies")
        return loaded > 0
    except Exception as e:
        log("ERROR", f"Cookie load failed: {e}")
        return False


# ─── JS TRACKER (backup) ─────────────────────────────────────────────────────
# Still inject this as a secondary net — catches src= assignments that CDP misses

TRACKER_JS = r"""
window.__vid_urls = [];
(function() {
    const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    if (desc && desc.set) {
        Object.defineProperty(HTMLMediaElement.prototype, 'src', {
            set: function(val) {
                if (val && typeof val === 'string' && val.startsWith('http'))
                    window.__vid_urls.push(val);
                return desc.set.call(this, val);
            },
            get: desc.get, configurable: true
        });
    }
})();
(function() {
    const orig = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        if (url && /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url))
            window.__vid_urls.push(url);
        return orig.apply(this, arguments);
    };
})();
(function() {
    const orig = window.fetch;
    window.fetch = function(input, init) {
        try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url && /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url))
                window.__vid_urls.push(url);
        } catch(e) {}
        return orig.apply(this, arguments);
    };
})();
"""


def read_js_tracker(driver) -> list[str]:
    try:
        raw = driver.execute_script("return window.__vid_urls || [];")
        urls = []
        for u in (raw or []):
            if isinstance(u, str) and is_real_video_url(u) and u not in urls:
                urls.append(u)
                log("HIT", f"JS tracker: {u[:100]}")
        return urls
    except Exception as e:
        log("WARN", f"JS tracker error: {e}")
        return []


# ─── PLAY BUTTON ──────────────────────────────────────────────────────────────

PLAY_SELECTORS = [
    "div.play-btn",           # confirmed from DOM dump
    ".play-btn",
    ".htv-video-player .play-btn",
    "[class*='play-btn']",
    ".vjs-big-play-button",
    "button[class*='play']",
    "[aria-label='Play Video']",
]


def click_play_fast(driver) -> bool:
    for sel in PLAY_SELECTORS:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            log("HIT", f"Clicked: {sel!r}")
            return True
        except Exception:
            continue
    return False


def js_force_play(driver, label="fallback") -> None:
    try:
        driver.execute_script("""
            document.querySelectorAll('video').forEach(v => {
                v.muted = false; v.volume = 1;
                v.play().catch(()=>{});
            });
            document.querySelectorAll('.play-btn,[class*="play-btn"]').forEach(el => {
                el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
            });
        """)
        log("INFO", f"JS force-play [{label}]")
    except Exception as e:
        log("WARN", f"JS force-play failed: {e}")


# ─── COLLECT ALL FOUND URLS ───────────────────────────────────────────────────

def read_iframe_src(driver) -> list[str]:
    """
    After clicking play, hanime loads the stream inside an <iframe>.
    The iframe's src changes from a placeholder to the real CDN URL.
    We check both the top-level page and switch into each iframe to
    also read any <video> or nested <iframe> inside it.
    """
    found = []
    try:
        # Step 1: grab all iframe srcs from the main page
        iframe_srcs = driver.execute_script("""
            var srcs = [];
            document.querySelectorAll('iframe').forEach(function(f) {
                var s = f.src || f.getAttribute('src') || '';
                if (s) srcs.push(s);
            });
            return srcs;
        """)
        for src in (iframe_srcs or []):
            log("INFO", f"  <iframe> src={src[:100]}")
            if is_real_video_url(src) and src not in found:
                found.append(src)
                log("HIT", f"iframe src: {src[:100]}")

        # Step 2: switch INTO each iframe and check for <video> + nested iframes
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for i, frame in enumerate(iframes):
            try:
                driver.switch_to.frame(frame)

                # Check <video> inside iframe
                inner_videos = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('video').forEach(function(v) {
                        var u = v.currentSrc || v.src || '';
                        if (u) r.push(u);
                    });
                    return r;
                """)
                for u in (inner_videos or []):
                    log("INFO", f"  <iframe[{i}]><video> src={u[:80]}")
                    if is_real_video_url(u) and u not in found:
                        found.append(u)
                        log("HIT", f"iframe[{i}] video: {u[:100]}")

                # Check nested iframes inside iframe
                nested_srcs = driver.execute_script("""
                    var srcs = [];
                    document.querySelectorAll('iframe').forEach(function(f) {
                        var s = f.src || '';
                        if (s) srcs.push(s);
                    });
                    return srcs;
                """)
                for u in (nested_srcs or []):
                    log("INFO", f"  <iframe[{i}]><iframe> src={u[:80]}")
                    if is_real_video_url(u) and u not in found:
                        found.append(u)
                        log("HIT", f"iframe[{i}] nested: {u[:100]}")

            except Exception as e:
                log("WARN", f"iframe[{i}] switch error: {e}")
            finally:
                driver.switch_to.default_content()  # always return to main page

    except Exception as e:
        log("WARN", f"read_iframe_src error: {e}")

    return found


def collect_urls(driver) -> list[str]:
    """
    Merge URLs from all four sources:
    1. CDP network log (real backend responses — most reliable)
    2. JS tracker (src= assignments / XHR / fetch)
    3. <video>.currentSrc (what's actually loaded in the player)
    4. <iframe> src (changes to CDN URL after play is clicked)
    Ranked: m3u8 > mp4 > other
    """
    all_urls = []

    # Source 1: CDP network responses
    for u in get_network_video_urls(driver):
        if u not in all_urls:
            all_urls.append(u)

    # Source 2: JS tracker
    for u in read_js_tracker(driver):
        if u not in all_urls:
            all_urls.append(u)

    # Source 3: live <video> element
    try:
        videos = driver.execute_script("""
            var r = [];
            document.querySelectorAll('video').forEach(function(v) {
                var u = v.currentSrc || v.src || '';
                var d = isNaN(v.duration) ? 0 : v.duration;
                if (u) r.push({url: u, duration: d, readyState: v.readyState});
            });
            return r;
        """)
        for v in (videos or []):
            url = v.get("url", "")
            dur = v.get("duration", 0)
            rs  = v.get("readyState", 0)
            log("INFO", f"  <video> rs={rs} dur={dur:.1f}s src={url[:80]}")
            if is_real_video_url(url) and url not in all_urls:
                all_urls.append(url)
    except Exception as e:
        log("WARN", f"video element read error: {e}")

    # Source 4: iframe src (changes after play click)
    for u in read_iframe_src(driver):
        if u not in all_urls:
            all_urls.append(u)

    # Rank: m3u8 first (HLS = best quality), then mp4, then rest
    ordered = (
        [u for u in all_urls if ".m3u8" in u.lower()] +
        [u for u in all_urls if ".mp4"  in u.lower()] +
        [u for u in all_urls if not any(x in u.lower() for x in (".m3u8", ".mp4"))]
    )
    return ordered


# ─── MAIN SCRAPER ─────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")
    result = {"title": "Unknown", "stream_url": None, "download_urls": [], "error": None}
    driver = build_driver()

    try:
        # Enable performance logging for CDP network events
        # NOTE: must be done via ChromeOptions desiredCapabilities for uc,
        # so we use get_log("performance") which uc enables by default
        driver.execute_cdp_cmd("Network.enable", {})

        # Inject JS tracker before page loads
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": TRACKER_JS})
        log("INFO", "CDP network capture + JS tracker ready")

        # Load cookies → navigate to video page (authenticated)
        has_cookies = load_cookies(driver, COOKIES_FILE)

        # Re-enable network capture after cookie navigation
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": TRACKER_JS})

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

        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.3);")
        time.sleep(0.3)

        # ── CLICK PLAY ────────────────────────────────────────────────────────
        log("INFO", "Clicking play button...")
        clicked = click_play_fast(driver)
        if not clicked:
            log("WARN", "Play button not found — JS force-play")
            js_force_play(driver, label="main")

        # ── IMMEDIATELY CHECK NETWORK RESPONSES ───────────────────────────────
        # After the play click, hanime's player fires an XHR/fetch to its backend
        # to get the stream manifest. CDP catches this the instant the response
        # comes back — no polling needed in the happy path.
        log("INFO", "Checking network responses after play click...")
        time.sleep(1.5)  # brief wait for the first network round-trip

        ordered = collect_urls(driver)

        # ── If nothing yet, wait for network activity (max 8s) ───────────────
        if not ordered:
            log("INFO", "Waiting for network video response (max 8s)...")
            deadline = time.time() + 8
            while time.time() < deadline:
                time.sleep(0.5)
                ordered = collect_urls(driver)
                if ordered:
                    log("HIT", f"Got URL after {8 - (deadline - time.time()):.1f}s wait")
                    break

        # ── Final fallback ────────────────────────────────────────────────────
        if not ordered:
            log("INFO", "Final fallback: JS force-play + 4s")
            js_force_play(driver)
            time.sleep(4)
            ordered = collect_urls(driver)

        result["stream_url"]    = ordered[0] if ordered else None
        result["download_urls"] = ordered
        log("INFO", f"Done — {len(ordered)} URL(s) | Title: {result['title']!r}")
        for u in ordered:
            log("INFO", f"  > {u[:120]}")

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

    status = await message.reply_text("🌐 Launching Chrome...", parse_mode=PM)

    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(
            loop.run_in_executor(None, scrape_video_url, url), timeout=180
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timed out after 3 minutes.", parse_mode=PM)
        return
    except Exception as e:
        await status.edit_text(f"❌ Scraper crashed:\n<code>{html_esc(e)}</code>", parse_mode=PM)
        return

    if data.get("error"):
        await status.edit_text(f"❌ Error:\n<code>{html_esc(data['error'])}</code>", parse_mode=PM)
        return

    stream_url = data["stream_url"]
    title      = data["title"]
    all_urls   = data["download_urls"]

    if not stream_url:
        await status.edit_text("❌ No video URL found. Login may be required.", parse_mode=PM)
        return

    await status.edit_text(
        f"✅ Found <b>{len(all_urls)}</b> URL(s)\n"
        f"<b>Title:</b> {html_esc(title)}\n\n⬇️ Downloading...",
        parse_mode=PM,
    )

    session_id = str(uuid.uuid4())
    file_path  = None

    for i, u in enumerate(all_urls, 1):
        file_path = await download_with_ytdlp(u, title, session_id, status)
        if file_path and os.path.exists(file_path):
            break
        if i < len(all_urls):
            await status.edit_text(f"⚠️ URL #{i} failed, trying #{i+1}...", parse_mode=PM)

    if not file_path or not os.path.exists(file_path):
        await status.edit_text(
            f"❌ Download failed.\n\nStream URL:\n<code>{html_esc(stream_url)}</code>",
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
