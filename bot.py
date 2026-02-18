import asyncio
import re
import os
import time
import glob
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
# STRICT URL FILTER — only CDN/video URLs
# ─────────────────────────────────────────────

# Must have one of these extensions
VIDEO_EXTENSIONS = re.compile(
    r'\.(m3u8|mp4|mkv|ts|m4v|webm)(\?|#|$)',
    re.IGNORECASE
)

# Must come from one of these CDN domains
CDN_DOMAINS = re.compile(
    r'('
    r'cdn\d*\.hanime\.tv|'
    r'hwcdn\.net|'
    r'storage\.googleapis\.com|'
    r'cloudfront\.net|'
    r'akamaized\.net|'
    r'fastly\.net|'
    r'b-cdn\.net|'
    r'r\.cloudflare\.com|'
    r'stream\.mux\.com'
    r')',
    re.IGNORECASE
)


def is_valid_video_url(url: str) -> bool:
    """
    Returns True only if the URL has a video file extension
    OR comes from a known CDN domain.
    Filters out all analytics, JS, images, API calls, etc.
    """
    if not url or not url.startswith("http"):
        return False
    has_video_ext  = bool(VIDEO_EXTENSIONS.search(url))
    has_cdn_domain = bool(CDN_DOMAINS.search(url))
    return has_video_ext or has_cdn_domain


# ─────────────────────────────────────────────
# CHROME OPTIONS
# ─────────────────────────────────────────────

def get_chrome_options() -> Options:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    return options


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

def scrape_video_url(page_url: str) -> dict:
    """
    Opens the page in selenium-wire headless Chrome,
    clicks play, then intercepts all network requests
    and scans page source + iframes for CDN/video URLs.
    """
    driver = wire_webdriver.Chrome(
        options=get_chrome_options(),
        seleniumwire_options={"disable_encoding": True},
    )

    result = {
        "title":         "Unknown",
        "stream_url":    None,
        "download_urls": [],
        "thumbnail":     None,
        "error":         None,
    }

    try:
        driver.get(page_url)
        wait = WebDriverWait(driver, 20)

        # ── Step 1: Grab title ──
        try:
            title_el = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "h1, .video-title, [class*='title']")
                )
            )
            result["title"] = title_el.text.strip() or driver.title
        except Exception:
            result["title"] = driver.title

        # ── Step 2: Click play button on main page ──
        play_selectors = [
            ".vjs-big-play-button",
            ".plyr__control--overlaid",
            "button.play-button",

"[class*='play-button']",
            "[class*='PlayButton']",
            "video",
        ]
        for sel in play_selectors:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                driver.execute_script("arguments[0].click();", btn)
                print(f"[+] Clicked play: {sel}")
                break
            except Exception:
                continue

        # ── Step 3: Force play via JS ──
        try:
            driver.execute_script(
                "document.querySelectorAll('video')"
                ".forEach(v => { try { v.play(); } catch(e) {} });"
            )
        except Exception:
            pass

        # ── Step 4: Click play inside every iframe ──
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                try:
                    driver.switch_to.frame(iframe)

                    # Try clicking play inside iframe
                    for sel in play_selectors:
                        try:
                            btn = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            print(f"[+] Clicked play inside iframe: {sel}")
                            break
                        except Exception:
                            continue

                    # Force play inside iframe too
                    try:
                        driver.execute_script(
                            "document.querySelectorAll('video')"
                            ".forEach(v => { try { v.play(); } catch(e) {} });"
                        )
                    except Exception:
                        pass

                    driver.switch_to.default_content()

                except Exception:
                    driver.switch_to.default_content()
                    continue
        except Exception:
            pass

        # ── Step 5: Wait for all network traffic to fire ──
        time.sleep(7)

        found_urls = []

        # ── Step 6: Scan intercepted network requests ──
        # selenium-wire logs every HTTP/HTTPS request Chrome made
        # including background XHR/fetch calls from the video player JS
        for req in driver.requests:
            url = req.url
            if not is_valid_video_url(url):
                continue
            if url in found_urls:
                continue
            # 200 = normal, 206 = Partial Content (normal for video streaming)
            if req.response and req.response.status_code not in (200, 206):
                continue
            found_urls.append(url)
            print(f"[+] Network request: {url}")

        # ── Step 7: Scan main page source ──
        try:
            src = driver.page_source

            # HTML attribute scan: src="...", file="...", url="..."
            for m in re.findall(
                r'(?:src|file|url|source)=["\']([^"\']+)["\']', src
            ):
                if is_valid_video_url(m) and m not in found_urls:
                    found_urls.append(m)
                    print(f"[+] Page source attr: {m}")

            # JSON blob scan: "src": "...", "stream": "..."
            for m in re.findall(
                r'"(?:src|url|file|source|stream|hls|video)"\s*:\s*"(https?://[^"]+)"',
                src
            ):
                if is_valid_video_url(m) and m not in found_urls:
                    found_urls.append(m)
                    print(f"[+] Page source JSON: {m}")

        except Exception:
            pass

        # ── Step 8: Scan every iframe's source ──
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                try:
                    driver.switch_to.frame(iframe)
                    iframe_src = driver.page_source

# HTML attributes inside iframe
                    for m in re.findall(
                        r'(?:src|file|url|source)=["\']([^"\']+)["\']',
                        iframe_src
                    ):
                        if is_valid_video_url(m) and m not in found_urls:
                            found_urls.append(m)
                            print(f"[+] iframe source attr: {m}")

                    # JSON blobs inside iframe
                    for m in re.findall(
                        r'"(?:src|url|file|source|stream|hls|video)"\s*:\s*"(https?://[^"]+)"',
                        iframe_src
                    ):
                        if is_valid_video_url(m) and m not in found_urls:
                            found_urls.append(m)
                            print(f"[+] iframe JSON: {m}")

                    driver.switch_to.default_content()

                except Exception:
                    driver.switch_to.default_content()
                    continue
        except Exception:
            pass

        # ── Step 9: Scan anchor download tags ──
        try:
            for link in driver.find_elements(By.CSS_SELECTOR, "a[href], a[download]"):
                href = link.get_attribute("href") or ""
                if is_valid_video_url(href) and href not in found_urls:
                    found_urls.append(href)
                    print(f"[+] Anchor tag: {href}")
        except Exception:
            pass

        # ── Step 10: Prioritize URLs ──
        # m3u8 first (HLS manifest, yt-dlp picks best quality from it)
        # then mp4, mkv, then bare CDN URLs
        m3u8_urls = [u for u in found_urls if ".m3u8" in u.lower()]
        mp4_urls  = [u for u in found_urls if ".mp4"  in u.lower()]
        mkv_urls  = [u for u in found_urls if ".mkv"  in u.lower()]
        cdn_only  = [
            u for u in found_urls
            if u not in m3u8_urls + mp4_urls + mkv_urls
        ]

        ordered = m3u8_urls + mp4_urls + mkv_urls + cdn_only

        result["stream_url"]    = ordered[0] if ordered else None
        result["download_urls"] = ordered

        # ── Step 11: Thumbnail ──
        try:
            og = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:image"]')
            result["thumbnail"] = og.get_attribute("content")
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)
    finally:
        driver.quit()

    return result


# ─────────────────────────────────────────────
# YT-DLP DOWNLOADER
# ─────────────────────────────────────────────

async def download_with_ytdlp(
    cdn_url: str,
    title: str,
    status_msg: Message,
) -> str | None:
    """
    Runs yt-dlp as a subprocess to download from CDN/stream URL.
    Streams stdout so we can update Telegram with live progress.
    Returns the path to the downloaded file or None on failure.
    """
    safe_title = re.sub(r'[^\w\s-]', '', title)[:60].strip() or "video"
    output_template = os.path.join(DOWNLOAD_DIR, f"{safe_title}.%(ext)s")

    cmd = [
        "yt-dlp",
        cdn_url,
        "--output", output_template,
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--retries", "5",
        "--fragment-retries", "10",
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

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    last_update = time.time()
    last_line   = ""

    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line:
            continue
        last_line = line
        print(f"[yt-dlp] {line}")

# Update Telegram message every 5 seconds with progress
        if "[download]" in line and time.time() - last_update > 5:
            try:
                await status_msg.edit_text(
                    f"⬇️ Downloading...\n\n{line}"
                )
                last_update = time.time()
            except Exception:
                pass

    await process.wait()

    if process.returncode != 0:
        print(f"[!] yt-dlp failed — last line: {last_line}")
        return None

    # Find the output file (extension may vary)
    pattern = os.path.join(DOWNLOAD_DIR, f"{safe_title}.*")
    files   = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else None


# ─────────────────────────────────────────────
# BOT COMMANDS
# ─────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 Hanime Downloader Bot\n\n"
        "Usage:\n"
        "/dl <hanime.tv video URL>\n\n"
        "Example:\n"
        "/dl https://hanime.tv/videos/hentai/some-title\n\n"
        "What happens:\n"
        "1. Opens page in headless Chrome\n"
        "2. Clicks play to trigger CDN requests\n"
        "3. Intercepts network traffic via selenium-wire\n"
        "4. Scans page source + iframes for video URLs\n"
        "5. Filters strictly for .m3u8 / .mp4 / .mkv / CDN domains\n"
        "6. Downloads best quality via yt-dlp\n"
        "7. Uploads to Telegram"
    )


@app.on_message(filters.command("dl"))
async def dl_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.reply_text(
            "❌ Please provide a URL.\n\n"
            "Usage: /dl <hanime.tv URL>"
        )
        return

    url = args[1].strip()

    if "hanime.tv" not in url:
        await message.reply_text("❌ Only hanime.tv URLs are supported.")
        return

    status = await message.reply_text(
        "🌐 Opening page in headless Chrome...\n"
        "This may take 15–30 seconds."
    )

    # ── Phase 1: Scrape ──
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, scrape_video_url, url)
    except Exception as e:
        await status.edit_text(f"❌ Scraper crashed:\n{e}")
        return

    if data.get("error"):
        await status.edit_text(f"❌ Scraper error:\n{data['error']}")
        return

    stream_url = data.get("stream_url")
    title      = data.get("title", "video")
    all_urls   = data.get("download_urls", [])

    if not stream_url:
        await status.edit_text(
            "❌ No video URL found.\n\n"
            "The page may require login or the stream is obfuscated."
        )
        return

    # Show what we found
    url_type = "m3u8 (HLS)" if ".m3u8" in stream_url else \
               "mp4" if ".mp4" in stream_url else \
               "mkv" if ".mkv" in stream_url else "CDN stream"

    found_text = "\n".join(
        f"{i+1}. {u[:80]}{'...' if len(u) > 80 else ''}"
        for i, u in enumerate(all_urls[:5])
    )

    await status.edit_text(
        f"✅ Found {len(all_urls)} video URL(s)\n\n"
        f"Title: {title}\n"
        f"Type: {url_type}\n\n"
        f"All found:\n{found_text}\n\n"
        f"⬇️ Starting download..."
    )

    # ── Phase 2: Download ──
    file_path = await download_with_ytdlp(stream_url, title, status)

    if not file_path or not os.path.exists(file_path):
        # Retry with original page URL directly
        await status.edit_text(
            "⚠️ CDN URL failed, retrying with page URL via yt-dlp..."
        )
        file_path = await download_with_ytdlp(url, title, status)

    if not file_path or not os.path.exists(file_path):
        await status.edit_text(
            f"❌ Download failed.\n\n"
            f"Stream URL (copy manually):\n{stream_url}"
        )
        return

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

# ── Phase 3: Upload ──
    await status.edit_text(
        f"📤 Uploading {file_size_mb:.1f} MB to Telegram..."
    )

    try:
        await client.send_video(
            chat_id=message.chat.id,
            video=file_path,
            caption=(
                f"🎬 {title}\n\n"
                f"📦 Size: {file_size_mb:.1f} MB\n"
                f"🔗 Source: {url}"
            ),
            supports_streaming=True,
            reply_to_message_id=message.id,
        )
        await status.delete()

    except Exception as e:
        await status.edit_text(
            f"❌ Upload failed:\n{e}\n\n"
            f"File was downloaded but couldn't be sent."
        )

    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if name == "main":
    print("[*] Bot starting...")
    app.run()
`
