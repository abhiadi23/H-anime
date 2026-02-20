import asyncio
import re
import os
import time
import glob
import uuid
import random
import json
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
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


PM = enums.ParseMode.HTML

app = Client("hanime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ─── LOGGER ───────────────────────────────────────────────────────────────────

def log(level: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level.ljust(5)}] {msg}")


# ─── URL FILTERS ──────────────────────────────────────────────────────────────

VIDEO_EXT = re.compile(r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)', re.IGNORECASE)

CDN_DOMAINS = re.compile(
    r'(cdn\d*\.hanime\.tv|hwcdn\.net|'
    r'cloudfront\.net|akamaized\.net|fastly\.net|b-cdn\.net|'
    r'videodelivery\.net|stream\.cloudflare\.com|stream\.mux\.com)',
    re.IGNORECASE
)

HANIME_CDN = re.compile(
    r'(hanime\.tv|hwcdn\.net|videodelivery\.net|mux\.com)',
    re.IGNORECASE
)

BLACKLIST = re.compile(
    r'(performance\.radar\.cloudflare\.com|cdnjs\.cloudflare\.com|'
    r'cdn\.jsdelivr\.net|google-analytics\.com|googletagmanager\.com|'
    r'doubleclick\.net|sentry\.io|newrelic\.com|analytics|tracking|'
    r'telemetry|metrics|beacon|\.js(\?|$)|'
    r'adtng\.com|adnxs\.com|adsrvr\.org|advertising\.com|'
    r'ads\.yahoo\.com|moatads\.com|amazon-adsystem\.com|'
    r'creatives\.|ad-delivery\.|adform\.net|rubiconproject\.com|'
    r'openx\.net|pubmatic\.com|taboola\.com|outbrain\.com|'
    r'exoclick\.com|trafficjunky\.net|traffichaus\.com|juicyads\.com|'
    r'plugrush\.com|tsyndicate\.com|etahub\.com|realsrv\.com)',
    re.IGNORECASE
)


def is_cdn_video_url(url: str, resource_type: str = "", status: int = 200) -> bool:
    """Filter for valid CDN video URLs (works on URL strings, no request object needed)."""
    if not url.startswith("http"):
        return False
    if BLACKLIST.search(url):
        return False
    has_video_ext = bool(VIDEO_EXT.search(url))
    from_cdn      = bool(CDN_DOMAINS.search(url))
    if not (has_video_ext or from_cdn):
        return False
    if not HANIME_CDN.search(url) and not has_video_ext:
        return False
    if re.search(r'/(creatives|banners?|ads?|promo)/', url, re.IGNORECASE):
        return False
    if status not in (0, 200, 206):   # 0 = unknown (CDP sometimes omits it)
        return False
    return True


# ─── CDP NETWORK INTERCEPTOR ──────────────────────────────────────────────────

class CDPNetworkInterceptor:
    """
    Uses Chrome DevTools Protocol via Selenium's execute_cdp_cmd to intercept
    all network requests/responses without seleniumwire.
    Dramatically lower memory footprint.
    """

    def __init__(self, driver):
        self.driver = driver
        self.found_urls: list[str] = []
        self._enabled = False

    def enable(self):
        """Enable CDP Network domain and attach JS listener via window.__cdp_urls."""
        self.driver.execute_cdp_cmd("Network.enable", {})
        # Inject a JS hook that collects XHR and fetch URLs into a global list
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                window.__cdp_urls = [];
                (function() {
                    // Intercept XHR
                    const origOpen = XMLHttpRequest.prototype.open;
                    XMLHttpRequest.prototype.open = function(method, url) {
                        if (url) window.__cdp_urls.push(url);
                        return origOpen.apply(this, arguments);
                    };
                    // Intercept fetch
                    const origFetch = window.fetch;
                    window.fetch = function(input, init) {
                        try {
                            const url = typeof input === 'string' ? input : input.url;
                            if (url) window.__cdp_urls.push(url);
                        } catch(e) {}
                        return origFetch.apply(this, arguments);
                    };
                    // Intercept video src
                    const origSrcDesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
                    if (origSrcDesc) {
                        Object.defineProperty(HTMLMediaElement.prototype, 'src', {
                            set: function(val) {
                                if (val) window.__cdp_urls.push(val);
                                return origSrcDesc.set.call(this, val);
                            },
                            get: origSrcDesc.get
                        });
                    }
                })();
            """
        })
        self._enabled = True
        log("INFO", "CDP network interceptor enabled")

    def collect(self) -> list[str]:
        """
        Poll the JS-side URL list AND scan the CDP request log via
        Performance.getMetrics + direct DOM inspection.
        Returns deduplicated list of candidate CDN video URLs.
        """
        urls = set()

        # 1. JS-injected XHR/fetch/src collector
        try:
            js_urls = self.driver.execute_script("return window.__cdp_urls || [];")
            for u in js_urls:
                if isinstance(u, str):
                    urls.add(u)
        except Exception as e:
            log("WARN", f"JS collect failed: {e}")

        # 2. Inspect all <video> and <source> tags on the page (incl. shadow DOM)
        try:
            media_urls = self.driver.execute_script("""
                const urls = [];
                document.querySelectorAll('video, video source').forEach(el => {
                    if (el.src) urls.push(el.src);
                    if (el.currentSrc) urls.push(el.currentSrc);
                });
                // Also check any blob/object URLs stored on video elements
                document.querySelectorAll('video').forEach(v => {
                    if (v.currentSrc) urls.push(v.currentSrc);
                });
                return urls;
            """)
            for u in (media_urls or []):
                if isinstance(u, str):
                    urls.add(u)
        except Exception as e:
            log("WARN", f"DOM media scan failed: {e}")

        # 3. Use CDP Network.getAllCookies isn't useful, but we can read
        #    performance entries which include resource URLs
        try:
            perf_entries = self.driver.execute_script("""
                return performance.getEntriesByType('resource').map(e => e.name);
            """)
            for u in (perf_entries or []):
                if isinstance(u, str):
                    urls.add(u)
        except Exception as e:
            log("WARN", f"Performance entries failed: {e}")

        # Filter
        valid = [u for u in urls if is_cdn_video_url(u)]
        for u in valid:
            if u not in self.found_urls:
                self.found_urls.append(u)
                log("HIT", f"CDN URL intercepted: {u}")

        return self.found_urls

    def collect_in_iframe(self) -> list[str]:
        """Same collection but runs inside the currently active iframe context."""
        urls = set()
        try:
            js_urls = self.driver.execute_script("return window.__cdp_urls || [];")
            for u in (js_urls or []):
                if isinstance(u, str):
                    urls.add(u)
        except Exception:
            pass
        try:
            media_urls = self.driver.execute_script("""
                const urls = [];
                document.querySelectorAll('video, video source').forEach(el => {
                    if (el.src) urls.push(el.src);
                    if (el.currentSrc) urls.push(el.currentSrc);
                });
                return urls;
            """)
            for u in (media_urls or []):
                if isinstance(u, str):
                    urls.add(u)
        except Exception:
            pass
        try:
            perf = self.driver.execute_script(
                "return performance.getEntriesByType('resource').map(e => e.name);"
            )
            for u in (perf or []):
                if isinstance(u, str):
                    urls.add(u)
        except Exception:
            pass

        valid = [u for u in urls if is_cdn_video_url(u)]
        for u in valid:
            if u not in self.found_urls:
                self.found_urls.append(u)
                log("HIT", f"CDN URL (iframe): {u}")

        return self.found_urls


# ─── DRIVER ───────────────────────────────────────────────────────────────────

def build_driver():
    """
    Lightweight driver: plain undetected_chromedriver + CDP.
    No seleniumwire proxy — saves ~200 MB RAM per session.
    """
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--allow-insecure-localhost")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    # Extra memory savings
    options.add_argument("--disable-images")          # skip image downloads
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--js-flags=--max-old-space-size=256")

    driver = uc.Chrome(options=options)
    return driver


# ─── HUMAN-LIKE MOUSE HELPERS ─────────────────────────────────────────────────

def human_move_and_click(driver, element) -> None:
    actions = ActionChains(driver)
    offset_x = random.randint(-5, 5)
    offset_y = random.randint(-3, 3)
    actions.move_to_element_with_offset(element, offset_x, offset_y)
    actions.pause(random.uniform(0.1, 0.3))
    actions.click()
    actions.perform()


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


def click_play(driver, label: str = "main") -> bool:
    for sel in PLAY_SELECTORS:
        try:
            el = WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            human_move_and_click(driver, el)
            log("HIT", f"Play clicked [{label}] via {sel!r}")
            return True
        except Exception:
            continue
    return False


def force_play(driver, label: str = "main") -> None:
    try:
        driver.execute_script(
            "document.querySelectorAll('video').forEach(v => {"
            "  v.muted = false; v.volume = 1; v.play();"
            "});"
        )
        log("INFO", f"JS force-play [{label}]")
    except Exception as e:
        log("WARN", f"JS force-play failed [{label}]: {e}")


# ─── SCRAPER ──────────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")

    result = {"title": "Unknown", "stream_url": None, "download_urls": [], "error": None}
    driver = build_driver()
    interceptor = CDPNetworkInterceptor(driver)

    try:
        # Enable CDP before loading page so injected JS runs from the start
        interceptor.enable()

        driver.get(page_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
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

        # ── Wait for player ────────────────────────────────────────────────────
        for sel in ["video", ".video-js", ".plyr", "[class*='player']"]:
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                log("INFO", f"Player found: {sel!r}")
                break
            except Exception:
                continue

        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.4);")
        time.sleep(0.4)

        # ── Click play + collect ───────────────────────────────────────────────
        click_play(driver, label="main")
        force_play(driver, label="main")

        # ── Fast poll — bail as soon as URL found (max 15s) ───────────────────
        log("INFO", "Polling for CDN URL (up to 15s)...")
        deadline = time.time() + 15
        found_early = False
        while time.time() < deadline:
            if interceptor.collect():
                elapsed = 15 - (deadline - time.time())
                log("HIT", f"CDN URL found after {elapsed:.1f}s")
                found_early = True
                break
            time.sleep(0.3)

        # ── Check iframes if nothing found ────────────────────────────────────
        if not found_early:
            log("INFO", "No CDN on main page — checking iframes...")
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            log("INFO", f"Found {len(iframes)} iframe(s)")

            for idx, iframe in enumerate(iframes):
                try:
                    driver.switch_to.frame(iframe)
                    log("INFO", f"Switched to iframe#{idx+1}")

                    # Re-inject collector into iframe context
                    driver.execute_script("""
                        if (!window.__cdp_urls) {
                            window.__cdp_urls = [];
                            const origOpen = XMLHttpRequest.prototype.open;
                            XMLHttpRequest.prototype.open = function(method, url) {
                                if (url) window.__cdp_urls.push(url);
                                return origOpen.apply(this, arguments);
                            };
                            const origFetch = window.fetch;
                            window.fetch = function(input, init) {
                                try {
                                    const url = typeof input === 'string' ? input : input.url;
                                    if (url) window.__cdp_urls.push(url);
                                } catch(e) {}
                                return origFetch.apply(this, arguments);
                            };
                        }
                    """)

                    click_play(driver, label=f"iframe#{idx+1}")
                    force_play(driver, label=f"iframe#{idx+1}")

                    # Collect from iframe
                    time.sleep(1.0)
                    interceptor.collect_in_iframe()

                    driver.switch_to.default_content()
                except Exception as e:
                    log("WARN", f"iframe#{idx+1} error: {e}")
                    driver.switch_to.default_content()

                if interceptor.found_urls:
                    log("HIT", f"CDN URL found via iframe#{idx+1}")
                    break
            else:
                if not interceptor.found_urls:
                    log("WARN", "No CDN URL found in any iframe")

        driver.switch_to.default_content()

        # ── Final collect pass on main page ───────────────────────────────────
        interceptor.collect()

        # ── Also do a direct DOM scan for video src (catches lazy-set sources) ─
        try:
            all_video_urls = driver.execute_script("""
                const urls = [];
                document.querySelectorAll('video').forEach(v => {
                    if (v.src && v.src.startsWith('http')) urls.push(v.src);
                    if (v.currentSrc && v.currentSrc.startsWith('http')) urls.push(v.currentSrc);
                    v.querySelectorAll('source').forEach(s => {
                        if (s.src) urls.push(s.src);
                    });
                });
                return urls;
            """)
            for u in (all_video_urls or []):
                if is_cdn_video_url(u) and u not in interceptor.found_urls:
                    interceptor.found_urls.append(u)
                    log("HIT", f"DOM video src: {u}")
        except Exception as e:
            log("WARN", f"DOM video scan failed: {e}")

        # ── Rank & return ──────────────────────────────────────────────────────
        found = interceptor.found_urls
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
        "Usage: <code>/dl &lt;hanime.tv URL&gt;</code>\n\n"
        "Stack: undetected-chromedriver · CDP network interception · yt-dlp",
        parse_mode=PM,
    )


@app.on_message(filters.command(["dl", "direct"]))
async def dl_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "❌ Usage: <code>/dl &lt;hanime.tv URL&gt;</code>",
            parse_mode=PM,
        )
        return

    url = args[1].strip()
    if "hanime.tv" not in url:
        await message.reply_text(
            "❌ Only hanime.tv URLs are supported.",
            parse_mode=PM,
        )
        return

    status = await message.reply_text(
        "🌐 Launching stealth Chrome... (~20–40s)",
        parse_mode=PM,
    )

    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(
            loop.run_in_executor(None, scrape_video_url, url), timeout=180
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timed out after 3 minutes.", parse_mode=PM)
        return
    except Exception as e:
        await status.edit_text(
            f"❌ Scraper crashed:\n<code>{html_esc(e)}</code>", parse_mode=PM
        )
        return

    if data.get("error"):
        await status.edit_text(
            f"❌ Error:\n<code>{html_esc(data['error'])}</code>", parse_mode=PM
        )
        return

    stream_url = data["stream_url"]
    title      = data["title"]
    all_urls   = data["download_urls"]

    if not stream_url:
        await status.edit_text(
            "❌ No CDN video URL found. Login may be required.", parse_mode=PM
        )
        return

    await status.edit_text(
        f"✅ Found <b>{len(all_urls)}</b> CDN URL(s)\n"
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
            await status.edit_text(
                f"⚠️ URL #{i} failed, trying #{i + 1}...", parse_mode=PM
            )

    if not file_path or not os.path.exists(file_path):
        await status.edit_text(
            f"❌ Download failed.\n\nStream URL:\n<code>{html_esc(stream_url)}</code>",
            parse_mode=PM,
        )
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if size_mb > 2000:
        await status.edit_text(
            f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 2000 MB.\n\n"
            f"<code>{html_esc(stream_url)}</code>",
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
        await status.edit_text(
            f"❌ Upload failed:\n<code>{html_esc(e)}</code>", parse_mode=PM
        )
    finally:
        try:
            import shutil
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        except Exception:
            pass


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run()
