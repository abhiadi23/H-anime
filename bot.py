import asyncio
import re
import os
import time
import glob
import uuid
import random
from config import *
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import undetected_chromedriver as uc

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def html_esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PM = enums.ParseMode.HTML
app = Client("hanime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def log(level: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level.ljust(5)}] {msg}")


# ─── URL VALIDATION ───────────────────────────────────────────────────────────

AD_BLACKLIST = re.compile(
    r'(blankmp4s\.pages\.dev|'
    r'adtng\.com|adnxs\.com|adsrvr\.org|advertising\.com|'
    r'ads\.yahoo\.com|moatads\.com|amazon-adsystem\.com|'
    r'exoclick\.com|trafficjunky\.net|traffichaus\.com|juicyads\.com|'
    r'plugrush\.com|tsyndicate\.com|etahub\.com|realsrv\.com|'
    r'doubleclick\.net|googletagmanager\.com|google-analytics\.com|'
    r'creatives\.|ad-delivery\.)',
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
    options.add_argument("--js-flags=--max-old-space-size=256")
    return uc.Chrome(options=options)


# ─── JS TRACKER ───────────────────────────────────────────────────────────────

TRACKER_JS = r"""
window.__vid_urls = [];

(function() {
    const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    if (desc && desc.set) {
        Object.defineProperty(HTMLMediaElement.prototype, 'src', {
            set: function(val) {
                if (val && typeof val === 'string' && val.startsWith('http')) {
                    window.__vid_urls.push(val);
                }
                return desc.set.call(this, val);
            },
            get: desc.get,
            configurable: true
        });
    }
})();

(function() {
    const orig = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        if (url && typeof url === 'string' &&
            /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url)) {
            window.__vid_urls.push(url);
        }
        return orig.apply(this, arguments);
    };
})();

(function() {
    const orig = window.fetch;
    window.fetch = function(input, init) {
        try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url && /\.(m3u8|mp4|ts|m4v|mkv)(\?|#|$)/i.test(url)) {
                window.__vid_urls.push(url);
            }
        } catch(e) {}
        return orig.apply(this, arguments);
    };
})();
"""


# ─── PLAY SELECTORS — hanime-specific first, then generic fallbacks ───────────
# Hanime uses a custom Vue player. These selectors target it directly.

PLAY_SELECTORS = [
    # Hanime's own Vue player (most likely matches)
    ".play-button",
    ".player-play-button",
    "div.play-button",
    "[class*='play-button']",
    "[class*='PlayButton']",
    # VideoJS (sometimes used as fallback player on hanime)
    ".vjs-big-play-button",
    # Generic
    ".plyr__control--overlaid",
    "[class*='BigPlayButton']",
    "[class*='big-play-button']",
    "button[class*='play']",
    "[aria-label='Play Video']",
    "[aria-label='Play']",
    # Very broad last resort — any clickable with 'play' in class
    "[class*='play']:not(script):not(style):not(a)",
]

# ─── JS-BASED PLAY — directly fires click on every candidate element ──────────

FAST_CLICK_JS = """
var selectors = [
    '.play-button', '.player-play-button', '[class*="play-button"]',
    '[class*="PlayButton"]', '.vjs-big-play-button', '.plyr__control--overlaid',
    'button[class*="play"]', '[aria-label="Play Video"]', '[aria-label="Play"]'
];
var clicked = [];
for (var s of selectors) {
    var els = document.querySelectorAll(s);
    els.forEach(function(el) {
        if (el && el.offsetParent !== null) {  // visible
            try { el.click(); clicked.push(s); } catch(e) {}
        }
    });
}
return clicked;
"""


def click_play_button(driver, label: str = "main") -> bool:
    """
    Two-phase approach:
      1. Fast JS click on all known selectors simultaneously (< 0.5s)
      2. Selenium WebDriverWait fallback per selector (up to 5s each, max 3 selectors)
    """
    # Phase 1: instant JS click — fires immediately, no waiting
    try:
        clicked = driver.execute_script(FAST_CLICK_JS)
        if clicked:
            log("HIT", f"JS fast-click [{label}] matched: {clicked}")
            return True
    except Exception as e:
        log("WARN", f"JS fast-click error: {e}")

    # Phase 2: Selenium wait — only try the most likely selectors, short timeout
    priority_selectors = [
        ".play-button",
        "[class*='play-button']",
        ".vjs-big-play-button",
        ".plyr__control--overlaid",
        "button[class*='play']",
        "[aria-label='Play Video']",
    ]
    for sel in priority_selectors:
        try:
            el = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            if not el.is_displayed():
                continue
            ActionChains(driver)\
                .move_to_element_with_offset(el, random.randint(-3, 3), random.randint(-2, 2))\
                .pause(random.uniform(0.05, 0.15))\
                .click()\
                .perform()
            log("HIT", f"Selenium click [{label}] via {sel!r}")
            return True
        except Exception:
            continue

    return False


def js_force_play(driver, label: str = "main") -> None:
    """Force all video elements to play via JS. Also tries to trigger Vue event handlers."""
    try:
        driver.execute_script("""
            // Force HTML5 video play
            document.querySelectorAll('video').forEach(function(v) {
                v.muted = false;
                v.volume = 1;
                v.play().catch(function(){});
            });

            // Attempt to trigger Vue click event on play buttons
            var selectors = [
                '.play-button', '[class*="play-button"]',
                '.vjs-big-play-button', 'button[class*="play"]'
            ];
            selectors.forEach(function(s) {
                document.querySelectorAll(s).forEach(function(el) {
                    try {
                        // Fire both native click and synthetic MouseEvent
                        el.click();
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    } catch(e) {}
                });
            });
        """)
        log("INFO", f"JS force-play [{label}]")
    except Exception as e:
        log("WARN", f"JS force-play failed: {e}")


# ─── DOM DUMP for debugging when nothing works ───────────────────────────────

def dump_player_dom(driver) -> None:
    """Log all elements that might be a play button — helps identify correct selector."""
    try:
        elements = driver.execute_script("""
            var results = [];
            var all = document.querySelectorAll('*');
            for (var el of all) {
                var cls = el.className || '';
                var tag = el.tagName || '';
                var label = el.getAttribute('aria-label') || '';
                if (typeof cls === 'string' &&
                    (cls.toLowerCase().includes('play') ||
                     label.toLowerCase().includes('play') ||
                     tag === 'VIDEO')) {
                    results.push({
                        tag: tag,
                        cls: cls.substring(0, 80),
                        label: label,
                        visible: el.offsetParent !== null
                    });
                }
                if (results.length >= 20) break;
            }
            return results;
        """)
        log("INFO", f"DOM dump — {len(elements)} play-related elements:")
        for el in (elements or []):
            log("INFO", f"  <{el['tag']}> class={el['cls']!r} aria-label={el['label']!r} visible={el['visible']}")
    except Exception as e:
        log("WARN", f"DOM dump failed: {e}")


# ─── READ PLAYING VIDEO ───────────────────────────────────────────────────────

def read_playing_video(driver) -> str | None:
    try:
        videos = driver.execute_script("""
            var results = [];
            document.querySelectorAll('video').forEach(function(v) {
                results.push({
                    currentSrc: v.currentSrc || '',
                    src: v.src || '',
                    paused: v.paused,
                    readyState: v.readyState,
                    duration: isNaN(v.duration) ? 0 : v.duration,
                    networkState: v.networkState,
                    ended: v.ended
                });
            });
            return results;
        """)
    except Exception as e:
        log("WARN", f"read_playing_video JS error: {e}")
        return None

    if not videos:
        return None

    for v in videos:
        url = v.get("currentSrc") or v.get("src") or ""
        dur = v.get("duration", 0)
        log("INFO", f"  <video> paused={v.get('paused')} readyState={v.get('readyState')} "
                    f"dur={dur:.1f}s src={url[:80]}")

    for v in videos:
        url = v.get("currentSrc") or v.get("src") or ""
        if (not v.get("paused") and v.get("duration", 0) > 10 and is_real_video_url(url)):
            log("HIT", f"Playing video (dur={v['duration']:.1f}s): {url[:100]}")
            return url

    for v in videos:
        url = v.get("currentSrc") or v.get("src") or ""
        if (v.get("readyState", 0) >= 2 and v.get("duration", 0) > 10 and is_real_video_url(url)):
            log("HIT", f"Loaded video (dur={v['duration']:.1f}s): {url[:100]}")
            return url

    for v in videos:
        url = v.get("currentSrc") or v.get("src") or ""
        if v.get("duration", 0) > 0 and is_real_video_url(url):
            log("HIT", f"Video with duration (dur={v['duration']:.1f}s): {url[:100]}")
            return url

    return None


def read_tracker(driver) -> list[str]:
    try:
        raw = driver.execute_script("return window.__vid_urls || [];")
        urls = []
        for u in (raw or []):
            if isinstance(u, str) and is_real_video_url(u) and u not in urls:
                urls.append(u)
                log("HIT", f"Tracker: {u[:100]}")
        return urls
    except Exception as e:
        log("WARN", f"read_tracker error: {e}")
        return []


# ─── MAIN SCRAPER ─────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")
    result = {"title": "Unknown", "stream_url": None, "download_urls": [], "error": None}
    driver = build_driver()

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": TRACKER_JS
        })
        log("INFO", "Tracker pre-injected via CDP")

        driver.get(page_url)

        # Wait for DOM ready — but DON'T wait for full "complete" since hanime is a SPA
        # and the player loads asynchronously. We just need DOMContentLoaded.
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
        log("INFO", f"Page loaded: {driver.title!r}")

        # ── Title ──────────────────────────────────────────────────────────────
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

        # ── Scroll down a bit so player is in viewport ────────────────────────
        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.3);")
        time.sleep(0.5)

        # ── FAST: Try JS click immediately, then check tracker (no long waits) ─
        log("INFO", "Attempting fast JS click...")
        clicked = click_play_button(driver, label="fast")

        # Check tracker immediately after click — SPA players load fast
        time.sleep(1.5)
        tracked = read_tracker(driver)
        if tracked:
            log("HIT", f"Got URL from tracker after fast click: {tracked[0][:100]}")
            stream_url = tracked[0]
        else:
            stream_url = read_playing_video(driver)

        # ── If still nothing, dump DOM to diagnose and try force-play ─────────
        if not stream_url:
            if not clicked:
                log("WARN", "No play button clicked — dumping DOM for diagnosis")
                dump_player_dom(driver)

            log("INFO", "Trying JS force-play...")
            js_force_play(driver, label="main")
            time.sleep(2.0)

            tracked = read_tracker(driver)
            stream_url = tracked[0] if tracked else read_playing_video(driver)

        # ── Poll loop (max 10s more, only if still nothing) ───────────────────
        if not stream_url:
            log("INFO", "Polling for CDN URL (max 10s)...")
            deadline = time.time() + 10
            while time.time() < deadline:
                url = read_playing_video(driver)
                if url:
                    stream_url = url
                    break
                tracked = read_tracker(driver)
                if tracked:
                    stream_url = tracked[0]
                    break
                time.sleep(0.5)

        # ── Final fallback ────────────────────────────────────────────────────
        if not stream_url:
            log("INFO", "Final fallback: force-play + 4s wait")
            js_force_play(driver, label="fallback")
            time.sleep(4)
            stream_url = read_playing_video(driver)
            if not stream_url:
                tracked = read_tracker(driver)
                stream_url = tracked[0] if tracked else None

        # ── Collect and rank all URLs ──────────────────────────────────────────
        all_urls = []
        if stream_url:
            all_urls.append(stream_url)
        for u in read_tracker(driver):
            if u not in all_urls:
                all_urls.append(u)

        ordered = (
            [u for u in all_urls if ".m3u8" in u.lower()] +
            [u for u in all_urls if ".mp4"  in u.lower()] +
            [u for u in all_urls if not any(x in u.lower() for x in (".m3u8", ".mp4"))]
        )

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

    status = await message.reply_text("🌐 Launching Chrome... (~20–35s)", parse_mode=PM)

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
        await status.edit_text(
            f"❌ File too large ({size_mb:.1f} MB).\n<code>{html_esc(stream_url)}</code>",
            parse_mode=PM,
        )
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
