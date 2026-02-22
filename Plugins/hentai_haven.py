import os
import asyncio
import time
import json
import logging
from pyrogram import Client, filters, idle
from pyrogram.types import Message
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import aiohttp
from urllib.parse import urlparse, urljoin
import re

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class VideoScraper:
    def __init__(self):
        self.driver = None
        self.video_url = None

    def setup_driver(self):
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
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        return uc.Chrome(options=options)
        # Anti-detection
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Enable network tracking AFTER driver creation
        try:
            self.driver.execute_cdp_cmd('Network.enable', {})
            self.driver.execute_cdp_cmd('Performance.enable', {})
            logging.info("✅ Network tracking enabled")
        except Exception as e:
            logging.warning(f"Could not enable network tracking: {e}")
        
        logging.info("✅ Driver setup complete")

    def close_driver(self):
        """Close the driver safely"""
        if self.driver:
            try:
                self.driver.quit()
                logging.info("✅ Driver closed")
            except Exception as e:
                logging.error(f"Error closing driver: {e}")
            self.driver = None

    def remove_all_overlays(self):
        """Enhanced overlay removal - more aggressive ad nuking"""
        try:
            js_script = """
            // Remove fixed/absolute positioned elements
            var elements = document.querySelectorAll('*');
            elements.forEach(function(el) {
                var style = window.getComputedStyle(el);
                if ((style.position === 'fixed' || style.position === 'absolute') &&
                    (el.tagName === 'DIV' || el.tagName === 'IFRAME') &&
                    !el.querySelector('video')) {
                    var zIndex = parseInt(style.zIndex);
                    if (zIndex > 100 || isNaN(zIndex)) {
                        el.remove();
                    }
                }
            });

            // Remove common ad selectors
            var adSelectors = [
                '[id*="ad"]', '[class*="ad-"]', '[class*="_ad"]',
                '[id*="popup"]', '[class*="popup"]', 
                '[class*="overlay"]', '[class*="modal"]',
                '[id*="banner"]', '[class*="banner"]',
                'iframe[src*="ads"]', 'iframe[src*="banner"]',
                '[class*="interstitial"]', '[id*="interstitial"]'
            ];

            adSelectors.forEach(function(selector) {
                document.querySelectorAll(selector).forEach(function(el) {
                    if (!el.querySelector('video')) {
                        el.remove();
                    }
                });
            });

            // Remove invisible overlays blocking clicks
            document.querySelectorAll('div').forEach(function(el) {
                var rect = el.getBoundingClientRect();
                var style = window.getComputedStyle(el);
                if (rect.width >= window.innerWidth * 0.8 && 
                    rect.height >= window.innerHeight * 0.8 &&
                    (style.position === 'fixed' || style.position === 'absolute') &&
                    !el.querySelector('video')) {
                    el.remove();
                }
            });
            
            console.log('Overlays nuked!');
            """
            self.driver.execute_script(js_script)
            logging.info("✅ Overlays and ads removed")
        except Exception as e:
            logging.error(f"Error removing overlays: {e}")

    def get_video_urls_from_network(self):
        """Extract video URLs from network logs - captures XHR/Fetch responses"""
        video_urls = set()
        try:
            logs = self.driver.get_log('performance')
            logging.info(f"📊 Processing {len(logs)} network logs")

            for log in logs:
                try:
                    message = json.loads(log['message'])['message']
                    method = message.get('method', '')

                    # Capture both requests and responses
                    if method in ['Network.responseReceived', 'Network.requestWillBeSent']:
                        params = message['params']
                        
                        # Get URL from response or request
                        url = (
                            params.get('response', {}).get('url') or
                            params.get('request', {}).get('url', '')
                        )

                        # Check if it's a video URL
                        if url and (
                            'hentaihaven' in url or
                            any(ext in url.lower() for ext in [
                                '.m3u8', '.mp4', '.ts', 
                                'manifest', '.m4s', 
                                'playlist', 'master'
                            ])
                        ):
                            # Filter out small tracking/analytics files
                            if not any(skip in url.lower() for skip in ['analytics', 'tracking', 'pixel', 'beacon']):
                                video_urls.add(url)
                                logging.info(f"📹 Found video URL: {url[:80]}...")

                except Exception:
                    continue

            unique_urls = list(video_urls)
            logging.info(f"✅ Total video URLs found: {len(unique_urls)}")
            return unique_urls

        except Exception as e:
            logging.error(f"Error getting video URLs from network: {e}")
            return []

    def find_video_in_dom(self):
        """Find video URLs in DOM elements"""
        try:
            video_elements = self.driver.find_elements(By.TAG_NAME, 'video')
            logging.info(f"Found {len(video_elements)} video elements in DOM")
            
            for video in video_elements:
                src = video.get_attribute('src')
                if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                    logging.info(f"✅ Found video in DOM: {src[:80]}...")
                    return src

                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                        logging.info(f"✅ Found source in DOM: {src[:80]}...")
                        return src
            return None
        except Exception as e:
            logging.error(f"Error finding video in DOM: {e}")
            return None

    def extract_from_page_source(self):
        """Extract video URLs from page source"""
        try:
            page_source = self.driver.page_source
            patterns = [
                r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
                r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*',
                r'"file"\s*:\s*"([^"]+)"',
                r'"src"\s*:\s*"([^"]+\.(?:mp4|m3u8)[^"]*)"',
            ]

            for pattern in patterns:
                matches = re.findall(pattern, page_source, re.IGNORECASE)
                for match in matches:
                    url = match if isinstance(match, str) else match[0]
                    if url.startswith('http') and any(ext in url.lower() for ext in ['.m3u8', '.mp4']):
                        logging.info(f"✅ Found in source: {url[:80]}...")
                        return url
            return None
        except Exception as e:
            logging.error(f"Error extracting from page source: {e}")
            return None

    def _scrape_blocking(self, url: str):
        """
        Optimized scraping flow:
        1. Load page
        2. Nuke ads immediately
        3. Click play button
        4. Nuke ads again
        5. Capture XHR/network response for video CDN link
        """
        try:
            self.setup_driver()
            logging.info(f"🌐 Loading page: {url}")
            self.driver.get(url)

            # Step 1: Wait for page load (reduced from 5s to 3s)
            logging.info("⏳ Waiting for page to load...")
            time.sleep(3)
            
            # Step 2: First ad removal
            logging.info("💣 Step 1: Nuking ads/overlays...")
            self.remove_all_overlays()

            # Step 3: Find and click play button
            logging.info("🎬 Step 2: Looking for play button...")
            play_selectors = [
                ".vjs-control-bar button.vjs-play-control",
                ".plyr__control--play",
                "button[data-purpose='PLAY']",
                "[aria-label*='play'], [aria-label*='Play']",
                ".play-button", 
                ".big-play-button", 
                ".vjs-big-play-button",
                "button[title*='play'], button[title*='Play']"
            ]

            wait = WebDriverWait(self.driver, 10)
            play_clicked = False

            for selector in play_selectors:
                try:
                    play_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    
                    # Scroll into view
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", play_btn)
                    time.sleep(0.5)
                    
                    # Remove any overlays blocking the button
                    self.driver.execute_script("""
                        arguments[0].style.zIndex = '999999';
                        arguments[0].style.position = 'relative';
                    """, play_btn)
                    
                    # Click using JavaScript (more reliable)
                    self.driver.execute_script("arguments[0].click();", play_btn)
                    logging.info(f"✅ Clicked play button: {selector}")
                    play_clicked = True
                    break
                except TimeoutException:
                    continue
                except Exception as e:
                    logging.warning(f"Failed to click {selector}: {e}")
                    continue

            # Fallback: JS play if button click failed
            if not play_clicked:
                logging.info("⚠️ No play button found, trying JS play...")
                self.driver.execute_script("""
                    var video = document.querySelector('video');
                    if (video) {
                        video.play();
                        console.log('JS play triggered');
                    }
                """)

            # Step 4: Wait for video stream to initialize (reduced from 12s to 8s)
            logging.info("⏳ Step 3: Waiting for video stream to start...")
            time.sleep(8)

            # Step 5: Second ad removal (after play)
            logging.info("💣 Step 4: Nuking ads again after play...")
            self.remove_all_overlays()

            # Step 6: Capture network logs for CDN/m3u8/mp4 links
            logging.info("🔍 Step 5: Checking network logs for video CDN URL...")
            video_urls = self.get_video_urls_from_network()

            if video_urls:
                # Prioritize m3u8 (HLS streams)
                m3u8_urls = [u for u in video_urls if '.m3u8' in u.lower()]
                if m3u8_urls:
                    # Pick the longest URL (usually the master playlist)
                    best_url = max(m3u8_urls, key=len)
                    logging.info(f"✅ Found m3u8 CDN link: {best_url[:100]}...")
                    return best_url
                
                # Fallback to mp4 if no m3u8
                mp4_urls = [u for u in video_urls if '.mp4' in u.lower()]
                if mp4_urls:
                    best_url = mp4_urls[0]
                    logging.info(f"✅ Found MP4 CDN link: {best_url[:100]}...")
                    return best_url

            # Step 7: Fallback - Check DOM
            logging.info("🔍 Step 6: Fallback - Checking DOM...")
            dom_url = self.find_video_in_dom()
            if dom_url:
                logging.info(f"✅ Found video in DOM: {dom_url[:100]}...")
                return dom_url

            # Step 8: Last resort - Page source regex
            logging.info("🔍 Step 7: Last resort - Checking page source...")
            src_url = self.extract_from_page_source()
            if src_url:
                logging.info(f"✅ Found video in page source: {src_url[:100]}...")
                return src_url

            logging.warning("❌ No video URL found after all methods")
            return None

        except Exception as e:
            logging.error(f"❌ Scraping error: {e}", exc_info=True)
            return None
        finally:
            self.close_driver()

    async def scrape_video_url(self, url: str):
        """
        Async wrapper - offloads blocking Selenium work to a thread executor
        so the Telegram event loop is never blocked and the bot stays responsive.
        """
        loop = asyncio.get_event_loop()
        video_url = await loop.run_in_executor(None, self._scrape_blocking, url)
        return video_url


async def download_video(url: str, filename: str, status_msg: Message = None):
    """
    Async video downloader using aiohttp.
    Replaces the old blocking requests.get() call.
    """
    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://hentaihaven.com/'
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3600)) as response:
                response.raise_for_status()

                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                last_update = 0

                with open(filename, 'wb') as f:
                    async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                            # Update progress every 10MB
                            if status_msg and downloaded - last_update > 10 * 1024 * 1024:
                                if total_size:
                                    progress = (downloaded / total_size) * 100
                                    size_info = (
                                        f"{downloaded / (1024*1024):.1f}MB "
                                        f"/ {total_size / (1024*1024):.1f}MB"
                                    )
                                else:
                                    progress = 0
                                    size_info = f"{downloaded / (1024*1024):.1f}MB"

                                await status_msg.edit_text(
                                    f"⬇️ **Downloading...**\n\n"
                                    f"Progress: {progress:.1f}%\n"
                                    f"Downloaded: {size_info}"
                                )
                                last_update = downloaded

        logging.info(f"✅ Download complete: {filename}")
        return True

    except Exception as e:
        logging.error(f"❌ Download error: {e}")
        return False


@Client.on_message(filters.command("dl2"))
async def download_command(client: Client, message: Message):
    """
    Main download command handler
    Usage: /dl2 <hentaihaven_url>
    """
    try:
        # Extract URL from command
        if len(message.command) < 2:
            await message.reply_text(
                "❌ **Missing URL!**\n\n"
                "**Usage:** `/dl2 <url>`\n\n"
                "**Example:**\n"
                "`/dl2 https://hentaihaven.com/video/arisugawa-ren-tte-honto-wa-onn/episode-5/`"
            )
            return

        url = message.command[1]

        if "hentaihaven.com" not in url:
            await message.reply_text(
                "❌ **Invalid URL!** Please provide a valid HentaiHaven.com URL."
            )
            return

        status_msg = await message.reply_text("🔍 **Initializing scraper...**")
        await status_msg.edit_text(
            "🔍 **Scraping video URL...**\n\n"
            "**Process:**\n"
            "• Loading page\n"
            "• Removing ads\n"
            "• Clicking play button\n"
            "• Capturing video stream\n\n"
            "⏱️ This may take 30-60 seconds..."
        )

        # Scrape — non-blocking thanks to run_in_executor
        scraper = VideoScraper()
        video_url = await scraper.scrape_video_url(url)

        if not video_url:
            await status_msg.edit_text(
                "❌ **Failed to extract video URL!**\n\n"
                "**Possible reasons:**\n"
                "• Anti-bot protection triggered\n"
                "• Page structure changed\n"
                "• Video not available\n"
                "• Network issues\n\n"
                "Check bot logs for detailed error information."
            )
            return

        await status_msg.edit_text(
            f"✅ **Video URL found!**\n\n"
            f"🔗 `{video_url[:100]}...`\n\n"
            f"⬇️ **Starting download...**"
        )

        # Build safe filename
        parts = url.rstrip('/').split('/')
        video_title = parts[-1] if parts[-1] else parts[-2]
        video_title = re.sub(r'[^\w\-_\.]', '_', video_title.split('?')[0])
        filename = f"{video_title}_{int(time.time())}.mp4"

        # Download — fully async, no blocking
        success = await download_video(video_url, filename, status_msg)

        if not success or not os.path.exists(filename):
            await status_msg.edit_text(
                f"❌ **Download failed!**\n\n"
                f"**Direct URL:**\n`{video_url}`\n\n"
                "Try downloading manually or check your network connection."
            )
            if os.path.exists(filename):
                os.remove(filename)
            return

        # Size check
        file_size = os.path.getsize(filename)
        file_size_mb = file_size / (1024 * 1024)

        if file_size_mb > 2000:
            await status_msg.edit_text(
                f"❌ **File too large!** ({file_size_mb:.1f}MB)\n\n"
                f"Telegram limit: 2GB (2000MB)\n\n"
                f"**Direct URL:**\n`{video_url}`\n\n"
                "Download manually using a download manager."
            )
            os.remove(filename)
            return

        # Upload to Telegram
        await status_msg.edit_text(
            f"📤 **Uploading to Telegram...**\n\n"
            f"Size: {file_size_mb:.1f}MB\n\n"
            f"Please wait..."
        )

        await message.reply_video(
            video=filename,
            caption=(
                f"🎬 **Downloaded Successfully!**\n\n"
                f"📁 Size: {file_size_mb:.1f}MB\n"
                f"🔗 Source: {url}"
            ),
            supports_streaming=True
        )

        await status_msg.delete()
        logging.info(f"✅ Successfully uploaded video: {filename}")

    except Exception as e:
        error_msg = str(e)
        logging.error(f"❌ Download command error: {e}", exc_info=True)
        try:
            await message.reply_text(
                f"❌ **Unexpected error occurred:**\n\n"
                f"`{error_msg[:150]}...`\n\n"
                f"Check bot logs for detailed information."
            )
        except Exception:
            pass
    finally:
        # Always clean up the local file
        if 'filename' in locals() and os.path.exists(filename):
            try:
                os.remove(filename)
                logging.info(f"🗑️ Cleaned up temporary file: {filename}")
            except Exception as e:
                logging.error(f"Failed to delete temp file: {e}")
