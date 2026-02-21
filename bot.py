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
    r'akamaized\.net|cloudfront\.net|fastly\.net|b-cdn\.net|hanime-cdn\.com|'
    r'highwinds-cdn\.com|m3u8s\.|freeanimehentai\.net)',
    re.IGNORECASE
)
# Match video extensions — also catch truncated .m3u8 that got cut to .m
VIDEO_EXT = re.compile(r'\.(m3u8|mp4|mkv|ts|m4v|webm|m3u)(\?|#|$)|/m3u8s/', re.IGNORECASE)

# The real omni-player iframe domain
OMNI_PLAYER = re.compile(r'hanime\.tv/omni-player', re.IGNORECASE)

# Ad iframe class signatures — never touch these
AD_CLASSES = re.compile(r'(ad-content|banner-ad|vertical-ad|hvp-panel)', re.IGNORECASE)


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


def is_real_player_iframe(frame) -> bool:
    """Return True only for the actual omni-player iframe, not ad iframes."""
    try:
        src = frame.get_attribute("src") or ""
        cls = frame.get_attribute("class") or ""
        # Must be the omni-player
        if not OMNI_PLAYER.search(src):
            return False
        # Must NOT be an ad frame
        if AD_CLASSES.search(cls):
            log("INFO", f"Skipping ad iframe: class={cls!r} src={src[:80]}")
            return False
        return True
    except Exception:
        return False


# ─── DRIVER ───────────────────────────────────────────────────────────────────

def get_cf_cookies(url: str) -> dict:
    """
    Use curl_cffi to impersonate Chrome TLS fingerprint (JA3/JA4).
    Cloudflare checks this before running any JS challenge.
    We follow redirects and grab all set-cookie headers.
    """
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
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://hanime.tv/",
            },
            allow_redirects=True,
        )
        # Pull cookies from the session jar (includes all redirect hops)
        cookies = dict(session.cookies)
        log("INFO", f"curl_cffi: status={resp.status_code} cookies={list(cookies.keys())}")

        if not cookies:
            # Fallback: manually parse Set-Cookie from response history
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
    """Inject curl_cffi CF cookies into Selenium."""
    if not cookies:
        log("WARN", "No CF cookies to inject")
        return
    injected = 0
    for name, value in cookies.items():
        try:
            driver.add_cookie({
                "name": name,
                "value": value,
                "domain": ".hanime.tv",
                "path": "/",
            })
            injected += 1
        except Exception as e:
            log("WARN", f"Cookie inject {name}: {e}")
    log("INFO", f"Injected {injected}/{len(cookies)} CF cookies into Selenium")


# ─── NETWORK INTERCEPTION (injected before page load via CDP) ─────────────────
# This intercepts BOTH:
#   - Outgoing request URLs (to catch m3u8/mp4 endpoints before response)
#   - Incoming response bodies (to catch CDN URLs the backend sends back)

NETWORK_INTERCEPT_JS = r"""
(function() {
    if (window.__cdn_urls_init) return;
    window.__cdn_urls_init = true;
    window.__cdn_urls     = [];   // METHOD 1: network tab (request URLs)
    window.__cdn_backend  = [];   // METHOD 2: backend response body

    function isVideoUrl(url) {
        return /\.(m3u8|mp4|ts|m4v|mkv|webm)(\?|#|$)/i.test(url) || /\/m3u8s\//i.test(url);
    }

    function isVideoCt(ct) {
        return ct.includes('video') || ct.includes('mpegurl') || ct.includes('octet-stream');
    }

    // Extract all CDN video URLs from a block of text (response body / JSON)
    // Uses a generous pattern — stops at whitespace or unbalanced quote only
    function extractCdnUrls(text) {
        var found = [];
        // Match full URL including query string, stop at whitespace / quote / angle bracket
        var re = /https?:\/\/[^\s"'<>\\]+/gi;
        var m;
        while ((m = re.exec(text)) !== null) {
            var u = m[0].replace(/[.,;)\]]+$/, ''); // strip trailing punctuation
            if (isVideoUrl(u)) found.push(u);
        }
        return found;
    }

    // ── METHOD 1: Intercept outgoing request URLs ─────────────────────────
    var _origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__req_url = url || '';
        return _origOpen.apply(this, arguments);
    };

    var _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
        var xhr  = this;
        var url  = xhr.__req_url || '';
        if (isVideoUrl(url)) {
            window.__cdn_urls.push({ url: url, via: 'xhr-req' });
        }
        xhr.addEventListener('readystatechange', function() {
            if (xhr.readyState === 4 && xhr.status >= 200 && xhr.status < 300) {
                var ct = xhr.getResponseHeader('Content-Type') || '';
                if (isVideoCt(ct) && url) {
                    window.__cdn_urls.push({ url: url, via: 'xhr-resp-ct', ct: ct });
                }
                // Method 2: scan response text for CDN URLs
                try {
                    var matches = extractCdnUrls(xhr.responseText || '');
                    matches.forEach(function(u) {
                        window.__cdn_backend.push({ url: u, via: 'xhr-body', src_url: url });
                    });
                } catch(e) {}
            }
        });
        return _origSend.apply(this, arguments);
    };

    // ── fetch interception ────────────────────────────────────────────────
    var _origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        if (isVideoUrl(url)) {
            window.__cdn_urls.push({ url: url, via: 'fetch-req' });
        }
        return _origFetch.apply(this, arguments).then(function(resp) {
            try {
                var ct = resp.headers.get('Content-Type') || '';
                if (resp.ok && isVideoCt(ct) && url) {
                    window.__cdn_urls.push({ url: url, via: 'fetch-resp-ct', ct: ct });
                }
                // Method 2: scan response body
                if (resp.ok) {
                    resp.clone().text().then(function(text) {
                        var matches = extractCdnUrls(text);
                        matches.forEach(function(u) {
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


# ─── METHOD 1: Check Network Tab (outgoing request URLs) ─────────────────────

def method1_network_tab(driver, in_iframe: bool = False) -> str | None:
    """Read __cdn_urls — populated from outgoing XHR/fetch request URLs."""
    try:
        raw = driver.execute_script("return window.__cdn_urls || [];")
        for item in (raw or []):
            url = item.get("url", "") if isinstance(item, dict) else str(item)
            via = item.get("via", "?") if isinstance(item, dict) else "?"
            log("INFO", f"M1 network [{via}]: {url[:100]}")
            if is_real_video_url(url):
                log("HIT", f"Method1 network tab: {url[:120]}")
                return url
    except Exception as e:
        log("WARN", f"Method1 error: {e}")
    return None


# ─── METHOD 2: Backend Response Body ─────────────────────────────────────────

def method2_backend_response(driver) -> str | None:
    """Read __cdn_backend — CDN URLs extracted from XHR/fetch response bodies."""
    try:
        raw = driver.execute_script("return window.__cdn_backend || [];")
        for item in (raw or []):
            url = item.get("url", "") if isinstance(item, dict) else str(item)
            via = item.get("via", "?") if isinstance(item, dict) else "?"
            src = item.get("src_url", "") if isinstance(item, dict) else ""
            log("INFO", f"M2 backend [{via}] from={src[:60]}: {url[:100]}")
            if is_real_video_url(url):
                log("HIT", f"Method2 backend response: {url[:120]}")
                return url
    except Exception as e:
        log("WARN", f"Method2 error: {e}")
    return None


# ─── METHOD 3: Scan Player iFrame ────────────────────────────────────────────

def method3_iframe_scan(driver) -> str | None:
    """
    Switch into the real omni-player iframe (not ad iframes) and:
      a) Check its own __cdn_urls / __cdn_backend (tracker injected via CDP)
      b) Decode CDN URL from the iframe's own src parameter
    """
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            if not is_real_player_iframe(frame):
                continue

            src = frame.get_attribute("src") or ""
            log("INFO", f"M3 — real player iframe src: {src[:150]}")

            # 3a: Decode CDN URL from iframe src parameters
            decoded = unquote(src)
            candidates = re.findall(r'https?://[^\s,&#\'"<>]+', decoded)
            for url in candidates:
                url = url.rstrip(".,;)")
                if is_real_video_url(url):
                    log("HIT", f"Method3a iframe src decode: {url[:120]}")
                    return url

            # 3b: Switch into iframe and check its intercepted network data
            try:
                driver.switch_to.frame(frame)
                log("INFO", "M3 — switched into player iframe")

                # Check network tab data inside iframe
                raw1 = driver.execute_script("return window.__cdn_urls || [];")
                for item in (raw1 or []):
                    url = item.get("url", "") if isinstance(item, dict) else str(item)
                    if is_real_video_url(url):
                        log("HIT", f"Method3b iframe __cdn_urls: {url[:120]}")
                        driver.switch_to.default_content()
                        return url

                # Check backend response data inside iframe
                raw2 = driver.execute_script("return window.__cdn_backend || [];")
                for item in (raw2 or []):
                    url = item.get("url", "") if isinstance(item, dict) else str(item)
                    if is_real_video_url(url):
                        log("HIT", f"Method3b iframe __cdn_backend: {url[:120]}")
                        driver.switch_to.default_content()
                        return url

            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

    except Exception as e:
        log("WARN", f"Method3 error: {e}")
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
        loaded = 0
        for c in cookies:
            try:
                clean = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".hanime.tv"),
                    "path": c.get("path", "/"),
                }
                if "secure" in c:
                    clean["secure"] = c["secure"]
                if "expirationDate" in c:
                    clean["expiry"] = int(c["expirationDate"])
                driver.add_cookie(clean)
                loaded += 1
            except Exception as e:
                log("WARN", f"Skipped cookie {c.get('name','?')}: {e}")
        log("INFO", f"Loaded {loaded}/{len(cookies)} login cookies")
        return loaded > 0
    except Exception as e:
        log("ERROR", f"Cookie load failed: {e}")
        return False


# ─── PLAY BUTTON ──────────────────────────────────────────────────────────────

def find_and_click_play(driver) -> bool:
    """
    Find the real play button (not inside an ad iframe) and click it.
    Tries CSS selectors scoped to the main player container.
    """
    PLAY_SELECTORS = [
        ".htv-video-player .play-btn",
        ".htv-video-player",
        "div.play-btn",
        ".play-btn",
    ]
    for sel in PLAY_SELECTORS:
        try:
            btn = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            # Make sure we're not accidentally clicking inside an ad iframe
            driver.execute_script("arguments[0].click();", btn)
            log("HIT", f"Clicked play: {sel!r}")
            return True
        except Exception:
            continue
    return False


# ─── WAIT FOR REAL PLAYER IFRAME ──────────────────────────────────────────────

def wait_for_player_iframe(driver, timeout: int = 15) -> bool:
    """
    Wait for the real omni-player iframe to appear (ignoring ad iframes).
    Returns True if found within timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for frame in iframes:
                if is_real_player_iframe(frame):
                    log("INFO", "Real player iframe found")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ─── MAIN SCRAPER ─────────────────────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    log("INFO", f"Scraping: {page_url}")
    result = {"title": "Unknown", "stream_url": None, "error": None}
    driver = build_driver()

    try:
        # Inject network interceptor into ALL frames before any page JS runs
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": NETWORK_INTERCEPT_JS}
        )
        log("INFO", "Network interceptor injected via CDP")

        # ── STEP 0: curl_cffi CF bypass ───────────────────────────────────────
        # curl_cffi impersonates Chrome120 TLS fingerprint (JA3/JA4).
        # Cloudflare checks this BEFORE any JS challenge.
        # We grab cf_clearance + __cf_bm from curl_cffi and inject into Selenium.
        cf_cookies = get_cf_cookies(page_url)

        # Navigate to hanime.tv so we can set cookies for its domain
        driver.get("https://hanime.tv")
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # Inject CF clearance cookies
        inject_cf_cookies(driver, cf_cookies)

        # Inject login cookies if available
        load_cookies(driver, COOKIES_FILE)

        # Re-register tracker for any new documents (e.g., after page reload)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": NETWORK_INTERCEPT_JS}
        )

        # ── STEP 1: Navigate to video page ────────────────────────────────────
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

        # Scroll a bit so lazy-loaded player renders
        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.3);")
        time.sleep(1)

        # ── STEP 2: Wait for player container and click play ──────────────────
        log("INFO", "Waiting for player container...")
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".htv-video-player"))
            )
            log("INFO", "Player container found — clicking play...")
            clicked = find_and_click_play(driver)
            if not clicked:
                log("WARN", "Could not click any play button")
        except Exception as e:
            log("WARN", f"Player container not found: {e}")

        # Give the player a moment to fire its initial requests after click
        time.sleep(2)
        cdn_url = None
        method_used = None

        # ── STEP 3: METHOD 1 — Network tab (outgoing request URLs) ───────────
        log("INFO", "Method1: checking network tab for direct CDN request URLs...")
        cdn_url = method1_network_tab(driver)
        if cdn_url:
            method_used = "Method1 (network tab)"

        # ── STEP 4: METHOD 2 — Backend response body ──────────────────────────
        if not cdn_url:
            log("INFO", "Method2: checking backend response bodies for CDN URLs...")
            cdn_url = method2_backend_response(driver)
            if cdn_url:
                method_used = "Method2 (backend response body)"

        # ── STEP 5: METHOD 3 — Player iframe scan ─────────────────────────────
        if not cdn_url:
            log("INFO", "Method3: waiting for player iframe then scanning...")
            found = wait_for_player_iframe(driver, timeout=12)
            if not found:
                log("WARN", "Real player iframe did not appear")
            else:
                time.sleep(2)
                cdn_url = method3_iframe_scan(driver)
                if cdn_url:
                    method_used = "Method3 (player iframe)"

        result["stream_url"] = cdn_url
        if cdn_url:
            log("HIT", f"CDN URL found via {method_used}: {cdn_url[:100]}")
        else:
            log("WARN", "All 3 methods exhausted — CDN URL NOT FOUND")
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
