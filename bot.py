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
    r'googletagmanager\.com|google-analytics\.com|creatives\.|ad-delivery\.|'
    r'ht-cdn2\.|adtng\.|adnxs\.)',
    re.IGNORECASE
)

HANIME_CDN = re.compile(
    r'(hwcdn\.net|hanime\.tv|videodelivery\.net|mux\.com|'
    r'akamaized\.net|cloudfront\.net|fastly\.net|b-cdn\.net|'
    r'hanime-cdn\.com)',
    re.IGNORECASE
)

VIDEO_EXT = re.compile(r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)', re.IGNORECASE)

# Only trust iframes from hanime's own player domain
PLAYER_DOMAIN = re.compile(r'player\.hanime\.tv', re.IGNORECASE)


def is_real_video_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if url.startswith("blob:"):
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


# ─── NETWORK TRACKER JS ───────────────────────────────────────────────────────
# Injected into EVERY frame (including iframes) via CDP.
# Captures XHR, fetch, and video.src assignments.
# Stores results in window.__vid_urls on each frame's own window.

TRACKER_JS = r"""
if (!window.__vid_urls) window.__vid_urls = [];

// 1. video.src setter
(function() {
    var desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    if (desc && desc.set) {
        Object.defineProperty(HTMLMediaElement.prototype, 'src', {
            set: function(val) {
                if (val && typeof val === 'string' && val.startsWith('http'))
                    window.__vid_urls.push({url: val, type: 'src'});
                return desc.set.call(this, val);
            },
            get: desc.get, configurable: true
        });
    }
})();

// 2. XHR — only capture video file requests
(function() {
    var orig = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        if (url && typeof url === 'string' &&
            /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url))
            window.__vid_urls.push({url: url, type: 'xhr'});
        return orig.apply(this, arguments);
    };
})();

// 3. fetch — only capture video file requests
(function() {
    var orig = window.fetch;
    window.fetch = function(input, init) {
        try {
            var url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url && /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url))
                window.__vid_urls.push({url: url, type: 'fetch'});
        } catch(e) {}
        return orig.apply(this, arguments);
    };
})();
"""


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
            document.querySelectorAll('video').forEach(function(v) {
                v.muted = false; v.volume = 1;
                v.play().catch(function(){});
            });
            document.querySelectorAll('.play-btn,[class*="play-btn"]').forEach(function(el) {
                el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
            });
        """)
        log("INFO", f"JS force-play [{label}]")
    except Exception as e:
        log("WARN", f"force-play failed: {e}")


# ─── READ TRACKER FROM A SPECIFIC FRAME ───────────────────────────────────────

def read_tracker_in_current_frame(driver) -> list[str]:
    """Read window.__vid_urls from whatever frame the driver is currently in."""
    try:
        raw = driver.execute_script("return window.__vid_urls || [];")
        urls = []
        for item in (raw or []):
            url = item.get("url", "") if isinstance(item, dict) else str(item)
            typ = item.get("type", "?") if isinstance(item, dict) else "?"
            if url and is_real_video_url(url) and url not in urls:
                urls.append(url)
                log("HIT", f"Tracker [{typ}]: {url[:100]}")
        return urls
    except Exception as e:
        log("WARN", f"Tracker read error: {e}")
        return []


# ─── COLLECT URLS — SCOPED TO PLAYER IFRAME ONLY ─────────────────────────────

def collect_urls(driver) -> list[str]:
    """
    Three sources, all scoped to player.hanime.tv iframe only:

    1. JS tracker inside player iframe  — catches XHR/fetch/src for the real stream
    2. <video>.currentSrc inside iframe — what's actually loaded (skip blob: URLs)
    3. Main page tracker                — fallback if player is not in an iframe

    Ad iframes (adtng.com etc.) are completely ignored.
    """
    all_urls = []

    # ── Find the hanime player iframe ─────────────────────────────────────────
    player_frame_index = None
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for i, frame in enumerate(iframes):
            src = frame.get_attribute("src") or ""
            log("INFO", f"  iframe[{i}] src={src[:80]}")
            if PLAYER_DOMAIN.search(src):
                player_frame_index = i
                log("INFO", f"  → Player iframe found at index {i}")
                break
    except Exception as e:
        log("WARN", f"iframe scan error: {e}")

    # ── Switch into player iframe and extract ─────────────────────────────────
    if player_frame_index is not None:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            driver.switch_to.frame(iframes[player_frame_index])
            log("INFO", "Switched into player iframe")

            # Source 1: JS tracker (XHR/fetch/src inside the player)
            for u in read_tracker_in_current_frame(driver):
                if u not in all_urls:
                    all_urls.append(u)

            # Source 2: <video>.currentSrc inside player iframe
            try:
                videos = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('video').forEach(function(v) {
                        var u = v.currentSrc || v.src || '';
                        var d = isNaN(v.duration) ? 0 : v.duration;
                        r.push({url: u, duration: d, readyState: v.readyState, paused: v.paused});
                    });
                    return r;
                """)
                for v in (videos or []):
                    url = v.get("url", "")
                    log("INFO", f"  player<video> paused={v.get('paused')} "
                                f"rs={v.get('readyState')} dur={v.get('duration',0):.1f}s "
                                f"src={url[:80]}")
                    if is_real_video_url(url) and url not in all_urls:
                        all_urls.append(url)
                        log("HIT", f"Player video src: {url[:100]}")
            except Exception as e:
                log("WARN", f"player video read error: {e}")

            # Source 3: nested iframes inside player (some players double-wrap)
            try:
                nested = driver.find_elements(By.TAG_NAME, "iframe")
                for j, nframe in enumerate(nested):
                    try:
                        driver.switch_to.frame(nframe)
                        for u in read_tracker_in_current_frame(driver):
                            if u not in all_urls:
                                all_urls.append(u)
                        driver.switch_to.parent_frame()
                    except Exception:
                        driver.switch_to.parent_frame()
            except Exception:
                pass

        except Exception as e:
            log("WARN", f"Player iframe switch error: {e}")
        finally:
            driver.switch_to.default_content()

    # ── Fallback: read tracker on main page (if no player iframe found) ───────
    if not all_urls:
        log("INFO", "No player iframe found — reading main page tracker")
        for u in read_tracker_in_current_frame(driver):
            if u not in all_urls:
                all_urls.append(u)

    # ── Rank: m3u8 > mp4 > other ──────────────────────────────────────────────
    ordered = (
        [u for u in all_urls if ".m3u8" in u.lower()] +
        [u for u in all_urls if ".mp4"  in u.lower()] +
        [u for u in all_urls if not any(x in u.lower() for x in (".m3u8", ".mp4"))]
    )
    return ordered


# ─── ALSO: inject tracker INTO player iframe after page load ─────────────────

def inject_tracker_into_player_iframe(driver) -> bool:
    """
    The CDP Page.addScriptToEvaluateOnNewDocument covers all frames,
    but as a belt-and-suspenders measure, also directly inject the tracker
    JS into the player iframe after it's loaded.
    """
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for i, frame in enumerate(iframes):
            src = frame.get_attribute("src") or ""
            if PLAYER_DOMAIN.search(src):
                driver.switch_to.frame(frame)
                driver.execute_script(TRACKER_JS)
                log("INFO", f"Tracker injected into player iframe[{i}]")
                driver.switch_to.default_content()
                return True
    except Exception as e:
        log("WARN", f"iframe tracker inject error: {e}")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return False


# ─── MAIN SCRAPER ─────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")
    result = {"title": "Unknown", "stream_url": None, "download_urls": [], "error": None}
    driver = build_driver()

    try:
        # Inject tracker into ALL frames before any page loads
        # (covers both main page and any iframes that load later)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": TRACKER_JS})
        log("INFO", "Tracker pre-injected via CDP (covers all frames)")

        # Load cookies first
        load_cookies(driver, COOKIES_FILE)

        # Re-inject after cookie navigation, then go to video page
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

        # ── Wait for player iframe to appear ──────────────────────────────────
        log("INFO", "Waiting for player iframe...")
        try:
            WebDriverWait(driver, 10).until(
                lambda d: any(
                    PLAYER_DOMAIN.search(f.get_attribute("src") or "")
                    for f in d.find_elements(By.TAG_NAME, "iframe")
                )
            )
            log("INFO", "Player iframe ready")
        except Exception:
            log("WARN", "Player iframe not found within 10s — continuing anyway")

        # ── Also inject tracker directly into player iframe ───────────────────
        inject_tracker_into_player_iframe(driver)

        # ── Click play ────────────────────────────────────────────────────────
        log("INFO", "Clicking play button...")
        clicked = click_play_fast(driver)
        if not clicked:
            log("WARN", "Play button not found — JS force-play")
            js_force_play(driver, label="main")

        # ── After click: give player iframe time to fire its stream request ───
        # The player inside player.hanime.tv will XHR/fetch the m3u8 manifest.
        # Our tracker catches it. 2s is enough for one network round-trip.
        time.sleep(2.0)

        ordered = collect_urls(driver)

        # ── Poll if not found yet (max 8s) ────────────────────────────────────
        if not ordered:
            log("INFO", "Polling for CDN URL (max 8s)...")
            deadline = time.time() + 8
            while time.time() < deadline:
                time.sleep(0.8)
                ordered = collect_urls(driver)
                if ordered:
                    log("HIT", f"Got URL after polling")
                    break

        # ── Final fallback ────────────────────────────────────────────────────
        if not ordered:
            log("INFO", "Final fallback: JS force-play inside player iframe + 4s")
            try:
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for frame in iframes:
                    if PLAYER_DOMAIN.search(frame.get_attribute("src") or ""):
                        driver.switch_to.frame(frame)
                        driver.execute_script("""
                            document.querySelectorAll('video').forEach(function(v) {
                                v.muted=false; v.volume=1; v.play().catch(function(){});
                            });
                        """)
                        driver.switch_to.default_content()
                        break
            except Exception:
                driver.switch_to.default_content()
                js_force_play(driver, label="fallback")

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
