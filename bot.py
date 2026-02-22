import asyncio
import re
import os
import json
import time
import glob
import uuid
from config import *
from Plugins.hentai_haven import *
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

DOWNLOAD_DIR = "./downloads"
COOKIES_FILE  = "./cookies.json"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def html_esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PM  = enums.ParseMode.HTML
app = Client("hanime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, plugins=dict(root="Plugins"))


def log(level: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level.ljust(5)}] {msg}")


# ─── URL VALIDATION ───────────────────────────────────────────────────────────

HANIME_CDN = re.compile(
    r'(hwcdn\.net|hanime\.tv|videodelivery\.net|mux\.com|'
    r'akamaized\.net|cloudfront\.net|fastly\.net|b-cdn\.net|hanime-cdn\.com|'
    r'highwinds-cdn\.com|m3u8s\.|freeanimehentai\.net)',
    re.IGNORECASE
)
VIDEO_EXT = re.compile(
    r'\.(m3u8|mp4|mkv|ts|m4v|webm|m3u)(\?|#|$)|/m3u8s/',
    re.IGNORECASE
)


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


# ─── CLOUDFLARE BYPASS ────────────────────────────────────────────────────────

def get_cf_cookies(url: str) -> dict:
    from curl_cffi import requests as cf_requests
    log("INFO", "curl_cffi: fetching page to get CF cookies...")
    try:
        session = cf_requests.Session(impersonate="chrome120")
        resp = session.get(
            url,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer":         "https://hanime.tv/",
            },
            allow_redirects=True,
        )
        cookies = dict(session.cookies)
        log("INFO", f"curl_cffi: status={resp.status_code} cookies={list(cookies.keys())}")
        if not cookies:
            for r in list(resp.history) + [resp]:
                for h_key, h_val in r.headers.items():
                    if h_key.lower() == "set-cookie":
                        try:
                            name, rest = h_val.split("=", 1)
                            value = rest.split(";")[0]
                            cookies[name.strip()] = value.strip()
                        except Exception:
                            pass
            log("INFO", f"curl_cffi fallback header parse: {list(cookies.keys())}")
        return cookies
    except Exception as e:
        log("WARN", f"curl_cffi failed: {e}")
        return {}


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
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return uc.Chrome(options=options)


def inject_cf_cookies(driver, cookies: dict) -> None:
    if not cookies:
        log("WARN", "No CF cookies to inject")
        return
    injected = 0
    for name, value in cookies.items():
        try:
            driver.add_cookie({
                "name":   name,
                "value":  value,
                "domain": ".hanime.tv",
                "path":   "/",
            })
            injected += 1
        except Exception as e:
            log("WARN", f"Cookie inject {name}: {e}")
    log("INFO", f"Injected {injected}/{len(cookies)} CF cookies into Selenium")


# ─── NETWORK INTERCEPTOR ──────────────────────────────────────────────────────
# Injects into every frame via CDP before any page JS runs.
# Scans every XHR/fetch response body for embedded CDN video URLs.
# Stores results in window.__cdn_backend.

NETWORK_INTERCEPT_JS = r"""
(function() {
    if (window.__cdn_urls_init) return;
    window.__cdn_urls_init = true;
    window.__cdn_backend = [];

    function isVideoUrl(url) {
        return /\.(m3u8|mp4|ts|m4v|mkv|webm)(\?|#|$)/i.test(url) || /\/m3u8s\//i.test(url);
    }

    function extractCdnUrls(text) {
        var found = [];
        var re = /https?:\/\/[^\s"'<>\\]+/gi;
        var m;
        while ((m = re.exec(text)) !== null) {
            var u = m[0].replace(/[.,;)\]]+$/, '');
            if (isVideoUrl(u)) found.push(u);
        }
        return found;
    }

    // XHR
    var _origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__req_url = url || '';
        return _origOpen.apply(this, arguments);
    };
    var _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
        var xhr = this;
        var url = xhr.__req_url || '';
        xhr.addEventListener('readystatechange', function() {
            if (xhr.readyState === 4 && xhr.status >= 200 && xhr.status < 300) {
                try {
                    extractCdnUrls(xhr.responseText || '').forEach(function(u) {
                        window.__cdn_backend.push({ url: u, via: 'xhr-body', src_url: url });
                    });
                } catch(e) {}
            }
        });
        return _origSend.apply(this, arguments);
    };

    // fetch
    var _origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        return _origFetch.apply(this, arguments).then(function(resp) {
            try {
                if (resp.ok) {
                    resp.clone().text().then(function(text) {
                        extractCdnUrls(text).forEach(function(u) {
                            window.__cdn_backend.push({ url: u, via: 'fetch-body', src_url: url });
                        });
                    }).catch(function(){});
                }
            } catch(e) {}
            return resp;
        });
    };
})();
"""


# ─── METHOD 2: Backend Response Body ─────────────────────────────────────────

def method2_backend_response(driver, retries: int = 4, delay: float = 1.0) -> str | None:
    """
    Read window.__cdn_backend — CDN URLs extracted from API response bodies.
    Retries because fetch().then(text()) resolves asynchronously.
    """
    for attempt in range(retries):
        try:
            raw = driver.execute_script("return window.__cdn_backend || [];")
            for item in (raw or []):
                url = item.get("url", "") if isinstance(item, dict) else str(item)
                via = item.get("via", "?") if isinstance(item, dict) else "?"
                src = item.get("src_url", "") if isinstance(item, dict) else ""
                log("INFO", f"M2 [{via}] from={src[:60]}: {url[:100]}")
                if is_real_video_url(url):
                    log("HIT", f"Method2 found CDN URL: {url[:120]}")
                    return url
            if attempt < retries - 1:
                log("INFO", f"M2 no hit yet, retry {attempt + 1}/{retries - 1}...")
                time.sleep(delay)
        except Exception as e:
            log("WARN", f"Method2 error: {e}")
    return None


# ─── COOKIE LOADER ────────────────────────────────────────────────────────────

def load_cookies(driver, cookies_file: str) -> bool:
    if not os.path.exists(cookies_file):
        log("WARN", f"No cookies file at {cookies_file!r} — no login")
        return False
    try:
        with open(cookies_file) as f:
            cookies = json.load(f)
        loaded = 0
        for c in cookies:
            try:
                clean = {
                    "name":   c["name"],
                    "value":  c["value"],
                    "domain": c.get("domain", ".hanime.tv"),
                    "path":   c.get("path", "/"),
                }
                if "secure" in c:
                    clean["secure"] = c["secure"]
                if "expirationDate" in c:
                    clean["expiry"] = int(c["expirationDate"])
                driver.add_cookie(clean)
                loaded += 1
            except Exception as e:
                log("WARN", f"Skipped cookie {c.get('name', '?')}: {e}")
        log("INFO", f"Loaded {loaded}/{len(cookies)} login cookies")
        return loaded > 0
    except Exception as e:
        log("ERROR", f"Cookie load failed: {e}")
        return False


# ─── AD REMOVAL ───────────────────────────────────────────────────────────────

def remove_ads(driver) -> None:
    """
    Nuke all known ad elements. Called before AND after play click.
    MutationObserver blocks future ad injections (never touches omni-player).
    """
    driver.execute_script("""
        var adSelectors = [
            'iframe[src*="googlesyndication"]',
            'iframe[src*="doubleclick"]',
            'iframe[src*="adservice"]',
            'iframe[src*="adsystem"]',
            'iframe[src*="advertising"]',
            'iframe[src*="adnxs"]',
            'iframe[src*="ad."]',
            '.ad-container', '.ad-wrapper', '.ad-slot',
            '.banner-ad', '.vertical-ad', '.hvp-adslot',
            '#ad-container', '#ad-banner', '#adsense',
            '[id*="google_ads"]', '[class*="google-ad"]',
            '[class*="dfp-ad"]', '[class*="ad-unit"]',
            'ins.adsbygoogle'
        ];
        var removed = 0;
        adSelectors.forEach(function(sel) {
            try {
                document.querySelectorAll(sel).forEach(function(el) {
                    el.remove(); removed++;
                });
            } catch(e) {}
        });
        if (!window.__adObserverActive) {
            window.__adObserverActive = true;
            try {
                new MutationObserver(function(mutations) {
                    mutations.forEach(function(m) {
                        m.addedNodes.forEach(function(node) {
                            if (!node.tagName) return;
                            var src = (node.src || (node.getAttribute && node.getAttribute('src')) || '').toLowerCase();
                            var cls = (node.className || '').toLowerCase();
                            if (src.includes('omni-player')) return;
                            if (src.includes('googlesyndication') || src.includes('doubleclick') ||
                                src.includes('adnxs') || src.includes('adservice') ||
                                cls.includes('ad-slot') || cls.includes('ad-unit') ||
                                cls.includes('hvp-adslot')) {
                                try { node.remove(); } catch(e) {}
                            }
                        });
                    });
                }).observe(document.body, { childList: true, subtree: true });
            } catch(e) {}
        }
        console.log('[AdKill] removed', removed, 'ad elements');
    """)
    log("INFO", "Ads removed from DOM")


# ─── PLAY BUTTON ──────────────────────────────────────────────────────────────

def find_and_click_play(driver) -> bool:
    PLAY_SELECTORS = [
        ".htv-video-player .play-btn",
        ".htv-video-player .vjs-big-play-button",
        ".htv-video-player .play-button",
        ".htv-video-player video",
        "div.play-btn",
        ".play-btn",
    ]
    for sel in PLAY_SELECTORS:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            driver.execute_script("arguments[0].click();", btn)
            log("HIT", f"Clicked play: {sel!r}")
            return True
        except Exception:
            continue

    try:
        player = driver.find_element(By.CSS_SELECTOR, ".htv-video-player")
        driver.execute_script("""
            arguments[0].dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, view: window
            }));
        """, player)
        log("HIT", "Clicked play: dispatched MouseEvent on .htv-video-player")
        return True
    except Exception:
        pass

    try:
        driver.execute_script("var v = document.querySelector('video'); if (v) v.play();")
        log("HIT", "Clicked play: called video.play() directly")
        return True
    except Exception:
        pass

    return False


# ─── MAIN SCRAPER ─────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")
    result = {"title": "Unknown", "stream_url": None, "error": None}
    driver = build_driver()

    try:
        # Inject interceptor into every frame before any JS runs
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": NETWORK_INTERCEPT_JS}
        )
        log("INFO", "Network interceptor injected via CDP")

        # CF bypass
        cf_cookies = get_cf_cookies(page_url)
        driver.get("https://hanime.tv")
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        inject_cf_cookies(driver, cf_cookies)
        load_cookies(driver, COOKIES_FILE)

        # Re-register for subsequent navigations
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": NETWORK_INTERCEPT_JS}
        )

        # Navigate to video page
        driver.get(page_url)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
        log("INFO", f"Page loaded: {driver.title!r}")

        # Extract title
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

        # ── Wait for player, remove ads, click play ────────────────────────────
        log("INFO", "Waiting for player container...")
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".htv-video-player"))
            )
            remove_ads(driver)       # before click
            time.sleep(0.5)

            log("INFO", "Player container found — clicking play...")
            clicked = find_and_click_play(driver)
            if not clicked:
                log("WARN", "Could not click any play button")

            time.sleep(1)
            remove_ads(driver)       # after click — clears overlay ads
        except Exception as e:
            log("WARN", f"Player container not found: {e}")

        # Give player time to fire post-click network requests
        time.sleep(2)

        # ── Method 2: Backend response body ───────────────────────────────────
        log("INFO", "Method2: checking backend response bodies...")
        cdn_url = method2_backend_response(driver, retries=4, delay=1.0)

        result["stream_url"] = cdn_url
        if cdn_url:
            log("HIT", f"CDN URL found: {cdn_url[:100]}")
        else:
            log("WARN", "CDN URL NOT FOUND — login may be required")
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

async def download_with_ytdlp(
    cdn_url: str, title: str, session_id: str, status_msg: Message
) -> str | None:
    safe_title  = re.sub(r'[^\w\s-]', '', title)[:60].strip() or "video"
    session_dir = os.path.join(DOWNLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    output_template = os.path.join(session_dir, f"{safe_title}.%(ext)s")

    cmd = [
        "yt-dlp", cdn_url,
        "--output", output_template,
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "1",
        "--concurrent-fragments", "16",
        "--socket-timeout", "15",
        "--http-chunk-size", "20M",
        "--buffer-size", "16K",
        "--no-part",
        "--newline", "--progress", "--no-warnings",
        "--add-header", "Referer:https://hanime.tv/",
        "--add-header",
        "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    last_update   = time.time()
    merging_notified = False

    async def read_output():
        nonlocal last_update, merging_notified
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            log("YTDL", line[:120])

            # Detect merge phase — yt-dlp prints this then goes silent for ffmpeg
            if "[Merger]" in line or "Merging formats" in line or "ffmpeg" in line.lower():
                if not merging_notified:
                    merging_notified = True
                    try:
                        await status_msg.edit_text("🔀 Merging video & audio... please wait")
                    except Exception:
                        pass

            elif "[download]" in line and "%" in line:
                if time.time() - last_update > 5:
                    try:
                        await status_msg.edit_text(f"⬇️ Downloading...\n\n{line}")
                        last_update = time.time()
                    except Exception:
                        pass

    # Run output reader and a periodic heartbeat ping in parallel
    async def heartbeat():
        """Ping TG every 30s during silent ffmpeg merge so Heroku doesn't kill us."""
        while True:
            await asyncio.sleep(30)
            if merging_notified:
                try:
                    await status_msg.edit_text("🔀 Merging video & audio... please wait")
                except Exception:
                    pass

    hb_task = asyncio.create_task(heartbeat())
    try:
        await read_output()
        await process.wait()
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    if process.returncode != 0:
        log("WARN", f"yt-dlp exited with code {process.returncode}")
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
        await message.reply_text(
            "❌ Usage: <code>/dl &lt;hanime.tv URL&gt;</code>", parse_mode=PM
        )
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
        await status.edit_text(
            f"❌ Error:\n<code>{html_esc(data['error'])}</code>", parse_mode=PM
        )
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
    file_path  = await download_with_ytdlp(cdn_url, title, session_id, status)

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
        await status.edit_text(
            f"❌ Upload failed:\n<code>{html_esc(e)}</code>", parse_mode=PM
        )
    finally:
        try:
            import shutil
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    app.run()
