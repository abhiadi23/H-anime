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
COOKIES_FILE  = "./cookies.json"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def html_esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PM  = enums.ParseMode.HTML
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
VIDEO_EXT = re.compile(
    r'\.(m3u8|mp4|mkv|ts|m4v|webm|m3u)(\?|#|$)|/m3u8s/',
    re.IGNORECASE
)
OMNI_PLAYER = re.compile(r'hanime\.tv/omni-player', re.IGNORECASE)


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
    """Return True for any omni-player iframe regardless of CSS classes."""
    try:
        src = frame.get_attribute("src") or ""
        return bool(OMNI_PLAYER.search(src))
    except Exception:
        return False


# ─── CLOUDFLARE BYPASS ────────────────────────────────────────────────────────

def get_cf_cookies(url: str) -> dict:
    """
    Use curl_cffi to impersonate Chrome120 TLS fingerprint (JA3/JA4).
    Cloudflare checks this before running any JS challenge.
    Returns cookies to inject into Selenium.
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
    """Inject curl_cffi CF cookies into Selenium."""
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


# ─── NETWORK INTERCEPTOR (injected via CDP before any page JS runs) ───────────
#
# Populates two arrays in every window context (main page + all iframes):
#   window.__cdn_urls    — METHOD 1: direct CDN request URLs (fires AFTER play click)
#   window.__cdn_backend — METHOD 2: CDN URLs found inside API response bodies
#                          (fires on page load AND again after play click)

NETWORK_INTERCEPT_JS = r"""
(function() {
    if (window.__cdn_urls_init) return;
    window.__cdn_urls_init = true;
    window.__cdn_urls    = [];
    window.__cdn_backend = [];

    function isVideoUrl(url) {
        return /\.(m3u8|mp4|ts|m4v|mkv|webm)(\?|#|$)/i.test(url) || /\/m3u8s\//i.test(url);
    }

    function isVideoCt(ct) {
        return ct.includes('video') || ct.includes('mpegurl') || ct.includes('octet-stream');
    }

    // Scan a block of text for embedded CDN video URLs
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

    // ── XHR interception ─────────────────────────────────────────────────────
    var _origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__req_url = url || '';
        return _origOpen.apply(this, arguments);
    };

    var _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
        var xhr = this;
        var url = xhr.__req_url || '';

        // Method 1: capture direct CDN request URLs
        if (isVideoUrl(url)) {
            window.__cdn_urls.push({ url: url, via: 'xhr-req' });
        }

        xhr.addEventListener('readystatechange', function() {
            if (xhr.readyState === 4 && xhr.status >= 200 && xhr.status < 300) {
                var ct = xhr.getResponseHeader('Content-Type') || '';

                // Method 1: video content-type response URL
                if (isVideoCt(ct) && url) {
                    window.__cdn_urls.push({ url: url, via: 'xhr-resp-ct', ct: ct });
                }

                // Method 2: scan response body for embedded CDN URLs
                try {
                    extractCdnUrls(xhr.responseText || '').forEach(function(u) {
                        window.__cdn_backend.push({ url: u, via: 'xhr-body', src_url: url });
                    });
                } catch(e) {}
            }
        });

        return _origSend.apply(this, arguments);
    };

    // ── fetch interception ────────────────────────────────────────────────────
    var _origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';

        // Method 1: capture direct CDN request URLs
        if (isVideoUrl(url)) {
            window.__cdn_urls.push({ url: url, via: 'fetch-req' });
        }

        return _origFetch.apply(this, arguments).then(function(resp) {
            try {
                var ct = resp.headers.get('Content-Type') || '';

                // Method 1: video content-type response URL
                if (resp.ok && isVideoCt(ct) && url) {
                    window.__cdn_urls.push({ url: url, via: 'fetch-resp-ct', ct: ct });
                }

                // Method 2: scan response body for embedded CDN URLs
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
    Called both:
      - Early (right after page load — Vue fetches /api/v8/guest/videos/{id}/ on mount)
      - Post-click (in case the API re-fetches after play is triggered)
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
                    log("HIT", f"Method2 backend response: {url[:120]}")
                    return url
            if attempt < retries - 1:
                log("INFO", f"M2 no hit yet, retry {attempt + 1}/{retries - 1}...")
                time.sleep(delay)
        except Exception as e:
            log("WARN", f"Method2 error: {e}")
    return None


# ─── METHOD 1: Network Tab (direct CDN request URLs) ─────────────────────────

def method1_network_tab(driver) -> str | None:
    """
    Read window.__cdn_urls — populated when the player directly requests
    a CDN video URL (m3u8/mp4). Only fires AFTER play is clicked because
    the player only starts streaming once the user triggers playback.
    """
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


# ─── METHOD 3: Player iFrame Scan ────────────────────────────────────────────

def method3_iframe_scan(driver) -> str | None:
    """
    Find the real omni-player iframe and:
      3a) Decode any CDN URL embedded in the iframe src parameters
      3b) Switch into iframe JS context and read its own __cdn_backend / __cdn_urls
    """
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            if not is_real_player_iframe(frame):
                continue

            src = frame.get_attribute("src") or ""
            log("INFO", f"M3 — player iframe src: {src[:150]}")

            # 3a: CDN URL sometimes URL-encoded in iframe src params
            decoded = unquote(src)
            for url in re.findall(r'https?://[^\s,&#\'"<>]+', decoded):
                url = url.rstrip(".,;)")
                if is_real_video_url(url):
                    log("HIT", f"Method3a iframe src decode: {url[:120]}")
                    return url

            # 3b: Switch into iframe and read its intercepted network data
            try:
                driver.switch_to.frame(frame)
                log("INFO", "M3 — switched into player iframe")

                # Check backend response data first, then direct CDN requests
                for arr_name in ("__cdn_backend", "__cdn_urls"):
                    raw = driver.execute_script(f"return window.{arr_name} || [];")
                    for item in (raw or []):
                        url = item.get("url", "") if isinstance(item, dict) else str(item)
                        if is_real_video_url(url):
                            log("HIT", f"Method3b iframe {arr_name}: {url[:120]}")
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
    Nuke all known ad elements from the DOM.
    Called TWICE:
      1. Before play click — clears existing ads
      2. After play click  — removes invisible overlay ads that inject on play
    A MutationObserver is also installed to block future ad injections.
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
            'ins.adsbygoogle',
        ];
        var removed = 0;
        adSelectors.forEach(function(sel) {
            try {
                document.querySelectorAll(sel).forEach(function(el) {
                    el.remove();
                    removed++;
                });
            } catch(e) {}
        });

        // Install MutationObserver once to block future ad injections
        if (!window.__adObserverActive) {
            window.__adObserverActive = true;
            try {
                new MutationObserver(function(mutations) {
                    mutations.forEach(function(m) {
                        m.addedNodes.forEach(function(node) {
                            if (!node.tagName) return;
                            var src = (node.src || (node.getAttribute && node.getAttribute('src')) || '').toLowerCase();
                            var cls = (node.className || '').toLowerCase();
                            // Never remove the real omni-player iframe
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
    """
    Click the play button inside .htv-video-player.
    Tries multiple strategies to properly trigger Vue's play handler.
    """
    # Strategy 1: specific play button child selectors
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

    # Strategy 2: dispatch a real MouseEvent on the player container (Vue-friendly)
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

    # Strategy 3: call video.play() directly on the DOM element
    try:
        driver.execute_script("""
            var v = document.querySelector('video');
            if (v) { v.play(); }
        """)
        log("HIT", "Clicked play: called video.play() directly")
        return True
    except Exception:
        pass

    return False


# ─── WAIT FOR PLAYER IFRAME ───────────────────────────────────────────────────

def wait_for_player_iframe(driver, timeout: int = 12) -> bool:
    """Wait for the real omni-player iframe to appear in the DOM."""
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
    result  = {"title": "Unknown", "stream_url": None, "error": None}
    driver  = build_driver()

    try:
        # Inject network interceptor into every frame before any JS runs
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": NETWORK_INTERCEPT_JS}
        )
        log("INFO", "Network interceptor injected via CDP")

        # ── CF bypass ─────────────────────────────────────────────────────────
        cf_cookies = get_cf_cookies(page_url)

        driver.get("https://hanime.tv")
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        inject_cf_cookies(driver, cf_cookies)
        load_cookies(driver, COOKIES_FILE)

        # Re-register interceptor for subsequent navigations
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": NETWORK_INTERCEPT_JS}
        )

        # ── Navigate to video page ─────────────────────────────────────────────
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

        # ══════════════════════════════════════════════════════════════════════
        # EARLY Method 2 — runs before play click
        # Vue calls /api/v8/guest/videos/{id}/ on page mount.
        # The JSON response contains all CDN stream URLs.
        # If we catch it here, we skip play click entirely.
        # ══════════════════════════════════════════════════════════════════════
        log("INFO", "Early Method2: checking page-load API response...")
        time.sleep(2)
        cdn_url     = method2_backend_response(driver, retries=3, delay=0.8)
        method_used = "Method2 early (page-load API response)" if cdn_url else None

        if not cdn_url:
            # ── Remove ads BEFORE play click ──────────────────────────────────
            log("INFO", "Waiting for player container...")
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".htv-video-player"))
                )
                remove_ads(driver)
                time.sleep(0.5)

                # ── Click play ────────────────────────────────────────────────
                log("INFO", "Player container found — clicking play...")
                clicked = find_and_click_play(driver)
                if not clicked:
                    log("WARN", "Could not click any play button")

                # ── Remove ads AFTER play click ───────────────────────────────
                # Invisible overlay ads inject into the DOM right after play.
                # Remove them so they can't steal focus or intercept events.
                time.sleep(1)
                remove_ads(driver)

            except Exception as e:
                log("WARN", f"Player container not found: {e}")

            # Give player time to fire post-click network requests
            time.sleep(2)

            # ══════════════════════════════════════════════════════════════════
            # Method 2 POST-CLICK — API may re-fetch after play is triggered.
            # Ads are now cleared so no interference with the response capture.
            # ══════════════════════════════════════════════════════════════════
            log("INFO", "Method2: checking backend response bodies (post-click)...")
            cdn_url = method2_backend_response(driver, retries=4, delay=1.0)
            if cdn_url:
                method_used = "Method2 (post-click API response)"

        if not cdn_url:
            # ══════════════════════════════════════════════════════════════════
            # Method 1 — direct CDN request URLs.
            # Only fires AFTER play click because the player only starts
            # requesting stream segments once playback is triggered.
            # ══════════════════════════════════════════════════════════════════
            log("INFO", "Method1: checking network tab for direct CDN request URLs...")
            cdn_url = method1_network_tab(driver)
            if cdn_url:
                method_used = "Method1 (network tab)"

        if not cdn_url:
            # ══════════════════════════════════════════════════════════════════
            # Method 3 — iframe fallback.
            # Scans the omni-player iframe src params and its own JS context.
            # Last resort only.
            # ══════════════════════════════════════════════════════════════════
            log("INFO", "Method3: waiting for player iframe then scanning...")
            found = wait_for_player_iframe(driver, timeout=10)
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
        # Retry aggressively on stalled fragments
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "3",
        # Lower concurrency to avoid CDN throttling/stalls on last fragments
        "--concurrent-fragments", "2",
        # Kill and retry a fragment if it stalls for >30s
        "--socket-timeout", "30",
        "--newline", "--progress", "--no-warnings",
        "--add-header", "Referer:https://hanime.tv/",
        "--add-header",
        "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    last_update  = time.time()
    last_progress = time.time()
    last_line    = ""

    async for raw in process.stdout:
        line = raw.decode("utf-8", errors="ignore").strip()
        if not line:
            continue

        # Track last progress timestamp to detect stalls
        if "[download]" in line and "%" in line:
            last_progress = time.time()
            last_line = line

        # Update Telegram status every 5s
        if "[download]" in line and time.time() - last_update > 5:
            try:
                await status_msg.edit_text(f"⬇️ Downloading...\n\n{line}")
                last_update = time.time()
            except Exception:
                pass

        # Log all yt-dlp output for debugging
        log("YTDL", line[:120])

    await process.wait()
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
        await status.edit_text(
            "❌ No CDN URL found. Login may be required.", parse_mode=PM
        )
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
