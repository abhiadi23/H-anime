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

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("hanime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ─────────────────────────────────────────────
# LOGGER
# ─────────────────────────────────────────────

def log(level: str, step, msg: str) -> None:
    """
    Unified logger with timestamps, step numbers, and level tags.

    Levels:
      STEP  — major pipeline step header
      INFO  — normal progress info
      HIT   — a CDN/video URL was found/accepted
      SKIP  — a URL was rejected and why
      WARN  — non-fatal warning
      ERROR — something failed
      YTDLP — raw yt-dlp subprocess output
    """
    ts       = time.strftime("%H:%M:%S")
    step_str = f"[Step {step:02d}]" if isinstance(step, int) else "        "
    lvl      = level.ljust(5)
    print(f"[{ts}] [{lvl}] {step_str} {msg}")


# ─────────────────────────────────────────────
# URL FILTERING
# ─────────────────────────────────────────────

VIDEO_EXTENSIONS = re.compile(
    r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)',
    re.IGNORECASE
)

CDN_DOMAINS = re.compile(
    r'('
    r'cdn\d*\.hanime\.tv|'
    r'hwcdn\.net|'
    r'storage\.googleapis\.com|'
    r'cloudfront\.net|'
    r'akamaized\.net|'
    r'fastly\.net|'
    r'b-cdn\.net|'
    r'videodelivery\.net|'
    r'stream\.cloudflare\.com|'
    r'stream\.mux\.com'
    r')',
    re.IGNORECASE
)

BLACKLISTED_DOMAINS = re.compile(
    r'('
    r'performance\.radar\.cloudflare\.com|'
    r'cdnjs\.cloudflare\.com|'
    r'cdn\.jsdelivr\.net|'
    r'google-analytics\.com|'
    r'googletagmanager\.com|'
    r'doubleclick\.net|'
    r'facebook\.net|'
    r'twitter\.com/i/|'
    r'sentry\.io|'
    r'newrelic\.com|'
    r'hotjar\.com|'
    r'segment\.com|'
    r'mixpanel\.com|'
    r'amplitude\.com|'
    r'analytics|'
    r'tracking|'
    r'telemetry|'
    r'metrics|'
    r'beacon|'
    r'\.js(\?|$)'
    r')',
    re.IGNORECASE
)


def is_valid_video_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if BLACKLISTED_DOMAINS.search(url):
        return False
    return bool(VIDEO_EXTENSIONS.search(url)) or bool(CDN_DOMAINS.search(url))


def has_minimum_size(req, min_bytes: int = 100_000) -> bool:
    """
    Returns True if Content-Length >= min_bytes OR header is absent.
    HLS/chunked streams have no Content-Length — they always pass.
    Rejects tiny JS/analytics files that slipped domain filtering.
    """
    try:
        if req.response and req.response.headers:
            cl = req.response.headers.get("Content-Length")
            if cl is not None and int(cl) < min_bytes:
                return False
    except (ValueError, TypeError):
        pass
    return True


def fmt_size(req) -> str:
    try:
        cl = req.response.headers.get("Content-Length")
        if cl:
            return f"{int(cl)/1024:.1f} KB"
    except Exception:
        pass
    return "unknown size"


def fmt_ctype(req) -> str:
    try:
        return req.response.headers.get("Content-Type", "unknown")
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────
# ANTI-CLOUDFLARE CHROME OPTIONS
# ─────────────────────────────────────────────

def get_chrome_options() -> Options:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--allow-running-insecure-content")
    options.add_argument("--disable-web-security")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    options.add_argument("--lang=en-US,en;q=0.9")
    options.add_argument("--accept-language=en-US,en;q=0.9")
    return options


def patch_webdriver_flags(driver) -> None:
    """
    Injects 10 JS patches via CDP to hide all Selenium/WebDriver
    fingerprints that Cloudflare checks. Runs before any page JS.
    """
    stealth_js = """
        // 1. Remove navigator.webdriver — CF primary check
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // 2. Fake plugins — headless has 0, real browsers have 5+
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });

        // 3. Fake languages
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });

        // 4. Fix notifications permission — headless returns 'denied'
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );

        // 5. Add chrome runtime — missing in headless
        window.navigator.chrome = { runtime: {} };

        // 6. Realistic screen resolution
        Object.defineProperty(screen, 'width',  { get: () => 1920 });
        Object.defineProperty(screen, 'height', { get: () => 1080 });

        // 7. Hardware concurrency
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

        // 8. Device memory
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

        // 9. Remove cdc_ variables Selenium injects
        const removeCdc = () => {
            for (const key of Object.keys(window)) {
                if (key.startsWith('cdc_')) delete window[key];
            }
        };
        removeCdc();
        setInterval(removeCdc, 100);

        // 10. Fake WebGL vendor/renderer — CF checks GPU strings for VM detection
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, parameter);
        };
    """
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": stealth_js}
    )


def human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    d = random.uniform(min_s, max_s)
    log("INFO", None, f"Human delay: {d:.2f}s")
    time.sleep(d)


def human_scroll(driver) -> None:
    try:
        total = driver.execute_script("return document.body.scrollHeight")
        curr  = 0
        steps = 0
        while curr < total:
            step = random.randint(200, 400)
            driver.execute_script(f"window.scrollBy(0, {step});")
            curr  += step
            steps += 1
            time.sleep(random.uniform(0.1, 0.3))
        log("INFO", None, f"Human scroll: {steps} steps, page height {total}px")
    except Exception as e:
        log("WARN", None, f"Scroll failed: {e}")


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:

    log("STEP", 0,  "=" * 70)
    log("STEP", 0,  f"SCRAPER START")
    log("INFO", 0,  f"Target URL: {page_url}")
    log("STEP", 0,  "=" * 70)

    sw_options = {
        "disable_encoding":           True,
        "verify_ssl":                 False,
        "suppress_connection_errors": True,
    }

    log("INFO", None, "Launching Chrome with selenium-wire transparent proxy...")
    driver = wire_webdriver.Chrome(
        options=get_chrome_options(),
        seleniumwire_options=sw_options,
    )
    log("INFO", None, "Chrome launched successfully")

    result = {
        "title":         "Unknown",
        "stream_url":    None,
        "download_urls": [],
        "thumbnail":     None,
        "error":         None,
    }

    play_selectors = [
        ".vjs-big-play-button",
        ".plyr__control--overlaid",
        "button.play-button",
        "[class*='play-button']",
        "[class*='PlayButton']",
        "[class*='play_button']",
        ".ytp-large-play-button",
        "[aria-label='Play']",
        "video",
    ]

    try:

        # ════════════════════════════════════════
        # STEP 1 — Inject anti-CF stealth patches
        # ════════════════════════════════════════
        log("STEP", 1,  "Injecting anti-Cloudflare stealth JS patches via CDP")
        log("INFO", 1,  "Patches applied BEFORE page load so CF bot checks see a clean browser:")
        log("INFO", 1,  "  [1] navigator.webdriver  → undefined")
        log("INFO", 1,  "  [2] navigator.plugins    → [1,2,3,4,5]")
        log("INFO", 1,  "  [3] navigator.languages  → ['en-US','en']")
        log("INFO", 1,  "  [4] permissions.query    → fixed for notifications")
        log("INFO", 1,  "  [5] navigator.chrome     → {runtime:{}}")
        log("INFO", 1,  "  [6] screen size          → 1920x1080")
        log("INFO", 1,  "  [7] hardwareConcurrency  → 8")
        log("INFO", 1,  "  [8] deviceMemory         → 8")
        log("INFO", 1,  "  [9] cdc_ variables       → deleted every 100ms")
        log("INFO", 1,  " [10] WebGL vendor/renderer → Intel Inc. / Intel Iris")
        patch_webdriver_flags(driver)
        log("INFO", 1,  "All stealth patches injected OK")

        # ════════════════════════════════════════
        # STEP 2 — Set realistic request headers
        # ════════════════════════════════════════
        log("STEP", 2,  "Setting realistic HTTP request headers")
        driver.header_overrides = {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "DNT":             "1",
        }
        log("INFO", 2,  "Headers set:")
        log("INFO", 2,  "  Accept-Language: en-US,en;q=0.9")
        log("INFO", 2,  "  Accept: text/html,application/xhtml+xml,...")
        log("INFO", 2,  "  DNT: 1")

        # ════════════════════════════════════════
        # STEP 3 — Load page
        # ════════════════════════════════════════
        log("STEP", 3,  f"Loading page: {page_url}")
        t0 = time.time()
        driver.get(page_url)
        elapsed = time.time() - t0
        log("INFO", 3,  f"Page loaded in {elapsed:.2f}s")
        log("INFO", 3,  f"Browser title:   {driver.title!r}")
        log("INFO", 3,  f"Resolved URL:    {driver.current_url}")
        log("INFO", 3,  f"Requests so far: {len(driver.requests)} (from page load)")

        # ════════════════════════════════════════
        # STEP 4 — Human behavior simulation
        # ════════════════════════════════════════
        log("STEP", 4,  "Simulating human behavior (scroll + random delays)")
        log("INFO", 4,  "Purpose: CF scores sessions on behavior — bots never scroll")
        human_delay(2.0, 4.0)
        human_scroll(driver)
        human_delay(1.0, 2.0)
        log("INFO", 4,  "Human simulation complete")

        # ════════════════════════════════════════
        # STEP 5 — Extract title
        # ════════════════════════════════════════
        log("STEP", 5,  "Extracting video title from page")
        wait = WebDriverWait(driver, 20)
        try:
            title_el = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "h1, .video-title, [class*='title']")
                )
            )
            result["title"] = title_el.text.strip() or driver.title
            log("INFO", 5,  f"Title via CSS selector: {result['title']!r}")
        except Exception:
            result["title"] = driver.title
            log("WARN", 5,  f"CSS selector timed out — using browser title: {result['title']!r}")

        # ════════════════════════════════════════
        # STEP 6 — Click play button (main page)
        # ════════════════════════════════════════
        log("STEP", 6,  "Clicking play button on main page")
        log("INFO", 6,  f"Trying {len(play_selectors)} CSS selectors...")
        clicked = False
        for sel in play_selectors:
            log("INFO", 6,  f"  → Trying: {sel!r}")
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                log("INFO", 6,  f"    Element found — scrolling into view")
                driver.execute_script(
                    "arguments[0].scrollIntoView({behavior:'smooth',block:'center'});",
                    btn
                )
                human_delay(0.5, 1.0)
                driver.execute_script("arguments[0].click();", btn)
                log("HIT",  6,  f"    ✅ Play clicked via: {sel!r}")
                clicked = True
                break
            except Exception as e:
                log("INFO", 6,  f"    Not found/clickable: {type(e).__name__}")

        if not clicked:
            log("WARN", 6,  "No play button matched any selector — relying on JS force play")

        # ════════════════════════════════════════
        # STEP 7 — Force play via JS (main page)
        # ════════════════════════════════════════
        log("STEP", 7,  "Force-playing all <video> elements via JS on main page")
        try:
            count = driver.execute_script(
                """
                var vids = document.querySelectorAll('video');
                var n = 0;
                vids.forEach(function(v) {
                    try { v.muted = false; v.volume = 1.0; v.play(); n++; }
                    catch(e) {}
                });
                return n;
                """
            )
            log("INFO", 7,  f"JS play() called on {count} <video> element(s)")
        except Exception as e:
            log("WARN", 7,  f"JS force play failed: {e}")

        human_delay(1.0, 2.0)

        # ════════════════════════════════════════
        # STEP 8 — iframes: click play + force play
        # ════════════════════════════════════════
        log("STEP", 8,  "Scanning iframes — click play + force play inside each")
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            log("INFO", 8,  f"Found {len(iframes)} iframe(s) on main page")

            for idx, iframe in enumerate(iframes):
                src_attr = iframe.get_attribute("src") or "no src attr"
                log("INFO", 8,  f"  ── iframe #{idx+1}: {src_attr}")

                try:
                    driver.switch_to.frame(iframe)
                    log("INFO", 8,  f"     Switched into iframe #{idx+1}")

                    iframe_clicked = False
                    for sel in play_selectors:
                        try:
                            btn = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                            )
                            driver.execute_script(
                                "arguments[0].scrollIntoView({behavior:'smooth',block:'center'});",
                                btn
                            )
                            human_delay(0.3, 0.7)
                            driver.execute_script("arguments[0].click();", btn)
                            log("HIT",  8,  f"     ✅ Play clicked in iframe #{idx+1} via {sel!r}")
                            iframe_clicked = True
                            break
                        except Exception:
                            continue

                    if not iframe_clicked:
                        log("INFO", 8,  f"     No play button in iframe #{idx+1}")

                    try:
                        count = driver.execute_script(
                            """
                            var vids = document.querySelectorAll('video');
                            var n = 0;
                            vids.forEach(function(v) {
                                try { v.muted=false; v.volume=1.0; v.play(); n++; }
                                catch(e) {}
                            });
                            return n;
                            """
                        )
                        log("INFO", 8,  f"     JS play() on {count} video(s) in iframe #{idx+1}")
                    except Exception as e:
                        log("WARN", 8,  f"     JS play failed in iframe #{idx+1}: {e}")

                    driver.switch_to.default_content()
                    log("INFO", 8,  f"     Switched back to main page from iframe #{idx+1}")

                except Exception as e:
                    log("WARN", 8,  f"     iframe #{idx+1} interaction failed: {type(e).__name__}: {e}")
                    driver.switch_to.default_content()

        except Exception as e:
            log("WARN", 8,  f"iframe scan error: {e}")

        # ════════════════════════════════════════
        # STEP 9 — Wait for network traffic
        # ════════════════════════════════════════
        log("STEP", 9,  "Waiting 12s for all network requests to complete")
        log("INFO", 9,  "Covers: CF auth tokens, CDN URL generation, HLS manifest fetch, XHR/fetch from player JS")
        for i in range(12, 0, -1):
            time.sleep(1)
            if i % 3 == 0:
                log("INFO", 9,  f"  {i}s remaining — requests captured so far: {len(driver.requests)}")
        log("INFO", 9,  f"Wait complete — total requests captured: {len(driver.requests)}")

        found_urls = []

        # ════════════════════════════════════════
        # STEP 10 — Intercept network requests (PRIMARY)
        # ════════════════════════════════════════
        total_reqs = len(driver.requests)
        log("STEP", 10, f"PRIMARY METHOD — Scanning {total_reqs} intercepted network requests")
        log("INFO", 10, "selenium-wire captured all HTTP/HTTPS traffic Chrome made,")
        log("INFO", 10, "including background XHR/fetch from video player JS")
        log("INFO", 10, "Filters: not blacklisted → video ext OR CDN domain → status 200/206 → size >= 100KB")
        log("INFO", 10, "-" * 60)

        stats = {"blacklisted": 0, "no_match": 0, "bad_status": 0, "too_small": 0, "accepted": 0}

        for i, req in enumerate(driver.requests):
            url    = req.url
            method = req.method
            status = req.response.status_code if req.response else "no-resp"
            ctype  = fmt_ctype(req)
            csize  = fmt_size(req)

            log("INFO", 10, f"  [{i+1:03d}/{total_reqs}] {method} {status} | {ctype} | {csize}")
            log("INFO", 10, f"           URL: {url}")

            # Filter 1: blacklist check
            if BLACKLISTED_DOMAINS.search(url):
                stats["blacklisted"] += 1
                log("SKIP", 10, f"           ✗ BLACKLISTED domain")
                continue

            # Filter 2: video extension or CDN domain
            has_ext = bool(VIDEO_EXTENSIONS.search(url))
            has_cdn = bool(CDN_DOMAINS.search(url))
            if not (has_ext or has_cdn):
                stats["no_match"] += 1
                log("SKIP", 10, f"           ✗ No video ext ({has_ext}) and no CDN domain ({has_cdn})")
                continue

            # Filter 3: duplicate
            if url in found_urls:
                log("SKIP", 10, f"           ✗ Duplicate — already captured")
                continue

            # Filter 4: response status
            if req.response and req.response.status_code not in (200, 206):
                stats["bad_status"] += 1
                log("SKIP", 10, f"           ✗ Bad status: {status} (need 200 or 206)")
                continue

            # Filter 5: minimum size
            if not has_minimum_size(req, min_bytes=100_000):
                stats["too_small"] += 1
                log("SKIP", 10, f"           ✗ Too small ({csize}) — not a video file")
                continue

            # ✅ Accepted
            stats["accepted"] += 1
            found_urls.append(url)
            url_kind = "m3u8/HLS" if ".m3u8" in url.lower() else \
                       "mp4"      if ".mp4"  in url.lower() else \
                       "mkv"      if ".mkv"  in url.lower() else "CDN"
            log("HIT",  10, f"           ✅ ACCEPTED #{stats['accepted']} [{url_kind}]: {url}")

        log("INFO", 10, "-" * 60)
        log("INFO", 10, f"Network scan summary:")
        log("INFO", 10, f"  Total requests scanned : {total_reqs}")
        log("INFO", 10, f"  Blacklisted            : {stats['blacklisted']}")
        log("INFO", 10, f"  No video ext / CDN     : {stats['no_match']}")
        log("INFO", 10, f"  Bad HTTP status        : {stats['bad_status']}")
        log("INFO", 10, f"  Response too small     : {stats['too_small']}")
        log("INFO", 10, f"  ✅ Accepted            : {stats['accepted']}")

        # ════════════════════════════════════════
        # STEP 11 — Page source regex (FALLBACK 1)
        # ════════════════════════════════════════
        log("STEP", 11, "FALLBACK 1 — Regex scan of main page source HTML")
        log("INFO", 11, "Only finds URLs hardcoded into HTML attributes or JSON blobs")
        try:
            src = driver.page_source
            log("INFO", 11, f"Page source length: {len(src):,} chars")

            attr_hits = json_hits = 0

            for m in re.findall(r'(?:src|file|url|source)=["\']([^"\']+)["\']', src):
                if is_valid_video_url(m) and m not in found_urls:
                    found_urls.append(m)
                    attr_hits += 1
                    log("HIT",  11, f"  HTML attr match: {m}")

            for m in re.findall(
                r'"(?:src|url|file|source|stream|hls|video|manifest)"\s*:\s*"(https?://[^"]+)"', src
            ):
                if is_valid_video_url(m) and m not in found_urls:
                    found_urls.append(m)
                    json_hits += 1
                    log("HIT",  11, f"  JSON blob match: {m}")

            log("INFO", 11, f"Page source: {attr_hits} HTML attr hit(s), {json_hits} JSON hit(s)")

        except Exception as e:
            log("WARN", 11, f"Page source scan failed: {e}")

        # ════════════════════════════════════════
        # STEP 12 — iframe source regex (FALLBACK 2)
        # ════════════════════════════════════════
        log("STEP", 12, "FALLBACK 2 — Regex scan of each iframe's source HTML")
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            log("INFO", 12, f"Re-entering {len(iframes)} iframe(s) for HTML source scan")

            for idx, iframe in enumerate(iframes):
                try:
                    driver.switch_to.frame(iframe)
                    iframe_src = driver.page_source
                    log("INFO", 12, f"  iframe #{idx+1} source: {len(iframe_src):,} chars")

                    ia = ij = 0
                    for m in re.findall(r'(?:src|file|url|source)=["\']([^"\']+)["\']', iframe_src):
                        if is_valid_video_url(m) and m not in found_urls:
                            found_urls.append(m)
                            ia += 1
                            log("HIT",  12, f"  iframe #{idx+1} HTML attr: {m}")

                    for m in re.findall(
                        r'"(?:src|url|file|source|stream|hls|video|manifest)"\s*:\s*"(https?://[^"]+)"',
                        iframe_src
                    ):
                        if is_valid_video_url(m) and m not in found_urls:
                            found_urls.append(m)
                            ij += 1
                            log("HIT",  12, f"  iframe #{idx+1} JSON: {m}")

                    log("INFO", 12, f"  iframe #{idx+1}: {ia} HTML attr, {ij} JSON")
                    driver.switch_to.default_content()

                except Exception as e:
                    log("WARN", 12, f"  iframe #{idx+1} source scan failed: {type(e).__name__}: {e}")
                    driver.switch_to.default_content()

        except Exception as e:
            log("WARN", 12, f"iframe source scan error: {e}")

        # ════════════════════════════════════════
        # STEP 13 — Safety reset
        # ════════════════════════════════════════
        log("STEP", 13, "Safety reset — returning driver context to main page")
        try:
            driver.switch_to.default_content()
            log("INFO", 13, "Driver context reset OK")
        except Exception as e:
            log("WARN", 13, f"Reset failed (probably already at default): {e}")

        # ════════════════════════════════════════
        # STEP 14 — Anchor tag scan (FALLBACK 3)
        # ════════════════════════════════════════
        log("STEP", 14, "FALLBACK 3 — Scanning <a href> and <a download> anchor tags")
        try:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href], a[download]")
            log("INFO", 14, f"Found {len(anchors)} anchor element(s)")
            hits = 0
            for link in anchors:
                href = link.get_attribute("href") or ""
                if is_valid_video_url(href) and href not in found_urls:
                    found_urls.append(href)
                    hits += 1
                    log("HIT",  14, f"  Anchor download link: {href}")
            log("INFO", 14, f"Anchor scan: {hits} hit(s)")
        except Exception as e:
            log("WARN", 14, f"Anchor scan failed: {e}")

        # ════════════════════════════════════════
        # STEP 15 — Prioritize & finalize
        # ════════════════════════════════════════
        log("STEP", 15, "Prioritizing URLs: m3u8 (HLS) > mp4 > mkv > bare CDN")
        m3u8_urls = [u for u in found_urls if ".m3u8" in u.lower()]
        mp4_urls  = [u for u in found_urls if ".mp4"  in u.lower()]
        mkv_urls  = [u for u in found_urls if ".mkv"  in u.lower()]
        cdn_only  = [u for u in found_urls if u not in m3u8_urls + mp4_urls + mkv_urls]
        ordered   = m3u8_urls + mp4_urls + mkv_urls + cdn_only

        log("INFO", 15, f"  m3u8 (HLS): {len(m3u8_urls)}")
        log("INFO", 15, f"  mp4:        {len(mp4_urls)}")
        log("INFO", 15, f"  mkv:        {len(mkv_urls)}")
        log("INFO", 15, f"  bare CDN:   {len(cdn_only)}")
        log("INFO", 15, f"  TOTAL:      {len(ordered)}")

        if ordered:
            log("HIT",  15, f"Best URL: {ordered[0]}")
            for i, u in enumerate(ordered[1:], 2):
                log("INFO", 15, f"Fallback #{i}: {u}")
        else:
            log("WARN", 15, "No valid CDN URL found after all methods")

        result["stream_url"]    = ordered[0] if ordered else None
        result["download_urls"] = ordered

        try:
            og = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:image"]')
            result["thumbnail"] = og.get_attribute("content")
            log("INFO", None, f"Thumbnail: {result['thumbnail']}")
        except Exception:
            log("INFO", None, "No og:image thumbnail found")

        log("STEP", 0,  "=" * 70)
        log("STEP", 0,  f"SCRAPER DONE — {len(ordered)} CDN URL(s) found | Title: {result['title']!r}")
        log("STEP", 0,  "=" * 70)

    except Exception as e:
        result["error"] = str(e)
        log("ERROR", None, f"Scraper crashed: {e}")
    finally:
        log("INFO", None, "Closing Chrome and selenium-wire proxy...")
        driver.quit()
        log("INFO", None, "Chrome closed OK")

    return result


# ─────────────────────────────────────────────
# YT-DLP DOWNLOADER
# ─────────────────────────────────────────────

async def download_with_ytdlp(
    cdn_url:    str,
    title:      str,
    session_id: str,
    status_msg: Message,
    attempt:    int = 1,
) -> str | None:

    log("INFO", None, f"━━━ yt-dlp Attempt #{attempt} ━━━")
    log("INFO", None, f"URL: {cdn_url}")

    safe_title  = re.sub(r'[^\w\s-]', '', title)[:60].strip() or "video"
    session_dir = os.path.join(DOWNLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    output_template = os.path.join(session_dir, f"{safe_title}.%(ext)s")

    log("INFO", None, f"Session dir:     {session_dir}")
    log("INFO", None, f"Output template: {output_template}")

    cmd = [
        "yt-dlp",
        cdn_url,
        "--output",               output_template,
        "--format",               "bestvideo+bestaudio/best",
        "--merge-output-format",  "mp4",
        "--no-playlist",
        "--retries",              "5",
        "--fragment-retries",     "10",
        "--concurrent-fragments", "4",
        "--newline",
        "--progress",
        "--no-warnings",
        "--add-header", "Referer:https://hanime.tv/",
        "--add-header", (
            "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
    ]

    log("INFO", None, f"Command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    log("INFO", None, f"yt-dlp subprocess started — PID: {process.pid}")

    last_update = time.time()
    last_line   = ""

    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line:
            continue
        last_line = line
        log("YTDLP", None, line)

        if "[download]" in line and time.time() - last_update > 5:
            try:
                await status_msg.edit_text(f"⬇️ Downloading...\n\n{line}")
                last_update = time.time()
            except Exception:
                pass

    await process.wait()
    log("INFO", None, f"yt-dlp exited — return code: {process.returncode}")

    if process.returncode != 0:
        log("ERROR", None, f"yt-dlp FAILED — last output: {last_line}")
        return None

    pattern = os.path.join(session_dir, f"{safe_title}.*")
    files   = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    if files:
        size_mb = os.path.getsize(files[0]) / (1024 * 1024)
        log("INFO", None, f"Output file: {files[0]}")
        log("INFO", None, f"File size:   {size_mb:.2f} MB")
        return files[0]
    else:
        log("ERROR", None, f"yt-dlp exited 0 but no file found matching: {pattern}")
        return None


# ─────────────────────────────────────────────
# BOT COMMANDS
# ─────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 **Hanime Downloader Bot**\n\n"
        "**Usage:**\n"
        "`/dl <hanime.tv video URL>`\n"
        "`/direct <hanime.tv video URL>`\n\n"
        "**Example:**\n"
        "`/dl https://hanime.tv/videos/hentai/some-title`\n\n"
        "**What happens:**\n"
        "1. Opens page in stealth headless Chrome\n"
        "2. Bypasses Cloudflare bot detection\n"
        "3. Clicks play to trigger CDN requests\n"
        "4. Intercepts all network traffic via selenium-wire\n"
        "5. Scans page source + iframes as fallback\n"
        "6. Downloads best quality via yt-dlp\n"
        "7. Uploads to Telegram"
    )


@app.on_message(filters.command(["dl", "direct"]))
async def dl_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.reply_text("❌ Please provide a URL.\n\nUsage: `/dl <hanime.tv URL>`")
        return

    url = args[1].strip()

    if "hanime.tv" not in url:
        await message.reply_text("❌ Only hanime.tv URLs are supported.")
        return

    uid = message.from_user.id if message.from_user else "unknown"
    log("INFO", None, f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log("INFO", None, f"New /dl request — User: {uid} | URL: {url}")
    log("INFO", None, f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    status = await message.reply_text(
        "🌐 Opening page in stealth headless Chrome...\n"
        "Bypassing Cloudflare — this may take 20–40 seconds."
    )

    # ── Phase 1: Scrape ──
    log("INFO", None, "Starting scrape phase...")
    try:
        loop = asyncio.get_event_loop()
        data = await asyncio.wait_for(
            loop.run_in_executor(None, scrape_video_url, url),
            timeout=120
        )
    except asyncio.TimeoutError:
        log("ERROR", None, "Scraper timed out after 120s")
        await status.edit_text("❌ Scraper timed out after 2 minutes.")
        return
    except Exception as e:
        log("ERROR", None, f"Executor crashed: {e}")
        await status.edit_text(f"❌ Scraper crashed:\n`{e}`")
        return

    if data.get("error"):
        log("ERROR", None, f"Scraper error: {data['error']}")
        await status.edit_text(f"❌ Scraper error:\n`{data['error']}`")
        return

    stream_url = data.get("stream_url")
    title      = data.get("title", "video")
    all_urls   = data.get("download_urls", [])

    log("INFO", None, f"Scrape result — Title: {title!r} | URLs: {len(all_urls)}")
    for i, u in enumerate(all_urls):
        log("INFO", None, f"  #{i+1}: {u}")

    if not stream_url:
        log("WARN", None, "No stream URL found — aborting")
        await status.edit_text(
            "❌ No video URL found.\n\n"
            "The page may require login or the stream is heavily obfuscated."
        )
        return

    url_type = (
        "m3u8 (HLS)" if ".m3u8" in stream_url else
        "mp4"        if ".mp4"  in stream_url else
        "mkv"        if ".mkv"  in stream_url else
        "CDN stream"
    )

    found_text = "\n".join(
        f"{i+1}. `{u[:80]}{'...' if len(u) > 80 else ''}`"
        for i, u in enumerate(all_urls[:5])
    )

    await status.edit_text(
        f"✅ Found **{len(all_urls)}** video URL(s)\n\n"
        f"**Title:** {title}\n"
        f"**Type:** {url_type}\n\n"
        f"**URLs found:**\n{found_text}\n\n"
        f"⬇️ Starting download..."
    )

    session_id = str(uuid.uuid4())
    file_path  = None
    log("INFO", None, f"Download session ID: {session_id}")

    # ── Phase 2: Try primary stream_url ──
    log("INFO", None, "Download phase — trying primary stream URL...")
    file_path = await download_with_ytdlp(stream_url, title, session_id, status, attempt=1)

    # ── Phase 3: Try remaining CDN URLs ──
    if not file_path or not os.path.exists(file_path):
        log("WARN", None, "Primary URL failed — trying remaining CDN URLs...")
        for i, fallback_url in enumerate(all_urls[1:], start=2):
            log("INFO", None, f"Trying fallback URL #{i} of {len(all_urls)}: {fallback_url}")
            await status.edit_text(
                f"⚠️ URL #{i-1} failed, trying URL #{i} of {len(all_urls)}..."
            )
            file_path = await download_with_ytdlp(fallback_url, title, session_id, status, attempt=i)
            if file_path and os.path.exists(file_path):
                log("INFO", None, f"Fallback URL #{i} succeeded")
                break
            log("WARN", None, f"Fallback URL #{i} also failed")

    # ── Phase 4: Last resort — original page URL ──
    if not file_path or not os.path.exists(file_path):
        log("WARN", None, "All CDN URLs failed — trying original page URL via yt-dlp")
        await status.edit_text("⚠️ All CDN URLs failed. Trying original page URL via yt-dlp...")
        file_path = await download_with_ytdlp(url, title, session_id, status, attempt=99)

    if not file_path or not os.path.exists(file_path):
        log("ERROR", None, "All download attempts exhausted — giving up")
        await status.edit_text(
            f"❌ Download failed.\n\n"
            f"**Stream URL (copy manually):**\n`{stream_url}`"
        )
        return

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    log("INFO", None, f"Download complete — {file_path} ({file_size_mb:.2f} MB)")

    # ── Phase 5: Size check ──
    if file_size_mb > 2000:
        log("WARN", None, f"File too large for Telegram: {file_size_mb:.1f} MB")
        await status.edit_text(
            f"❌ File too large: **{file_size_mb:.1f} MB**\n"
            f"Telegram limit is 2000 MB.\n\n"
            f"**Stream URL:**\n`{stream_url}`"
        )
        try:
            import shutil
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        except Exception:
            pass
        return

    # ── Phase 6: Upload ──
    log("INFO", None, f"Uploading {file_size_mb:.2f} MB to chat {message.chat.id}...")
    await status.edit_text(f"📤 Uploading **{file_size_mb:.1f} MB** to Telegram...")

    upload_ok = False
    try:
        await client.send_video(
            chat_id=message.chat.id,
            video=file_path,
            caption=(
                f"🎬 **{title}**\n\n"
                f"📦 Size: {file_size_mb:.1f} MB\n"
                f"🔗 Source: {url}"
            ),
            supports_streaming=True,
            reply_to_message_id=message.id,
        )
        upload_ok = True
        log("INFO", None, "Upload to Telegram successful")
        await status.delete()

    except Exception as e:
        log("ERROR", None, f"Telegram upload failed: {e}")
        await status.edit_text(
            f"❌ Upload failed:\n`{e}`\n\n"
            f"File was downloaded but couldn't be sent."
        )

    finally:
        if upload_ok:
            try:
                import shutil
                shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
                log("INFO", None, f"Session folder cleaned up: {os.path.dirname(file_path)}")
            except Exception as e:
                log("WARN", None, f"Cleanup failed: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    log("INFO", None, "Bot starting...")
    app.run()
