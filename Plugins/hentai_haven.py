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
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import aiohttp
import re

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class VideoScraper:
    def __init__(self):
        self.driver = None

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
        options.add_argument("--page-load-strategy=eager")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        self.driver = uc.Chrome(options=options)
        self.driver.set_page_load_timeout(30)
        self.driver.set_script_timeout(30)
        logging.info("✅ Driver setup complete")
        return self.driver

    def close_driver(self):
        if self.driver:
            try:
                self.driver.quit()
                logging.info("✅ Driver closed")
            except:
                pass
            self.driver = None

    def remove_all_overlays(self):
        try:
            js_script = """
            var elements = document.querySelectorAll('*');
            elements.forEach(function(el) {
                var style = window.getComputedStyle(el);
                if ((style.position === 'fixed' || style.position === 'absolute') &&
                    (el.tagName === 'DIV' || el.tagName === 'IFRAME') &&
                    !el.querySelector('video')) {
                    var zIndex = parseInt(style.zIndex);
                    if (zIndex > 100 || isNaN(zIndex)) el.remove();
                }
            });
            var adSelectors = ['[id*="ad"]', '[class*="ad-"]', '[class*="popup"]', '[class*="overlay"]', '[class*="modal"]', '[id*="banner"]', '[class*="banner"]'];
            adSelectors.forEach(function(selector) {
                document.querySelectorAll(selector).forEach(function(el) {if (!el.querySelector('video')) el.remove();});
            });
            console.log('Overlays nuked!');
            """
            self.driver.execute_script(js_script)
            logging.info("✅ Overlays removed")
        except Exception as e:
            logging.error(f"Error removing overlays: {e}")

    def get_video_urls_from_network(self):
        video_urls = set()
        try:
            logs = self.driver.get_log('performance')
            for log in logs:
                try:
                    message = json.loads(log['message'])['message']
                    if message.get('method') in ['Network.responseReceived', 'Network.requestWillBeSent']:
                        params = message['params']
                        url = params.get('response', {}).get('url') or params.get('request', {}).get('url', '')
                        if url and ('hentaihaven' in url.lower() or any(ext in url.lower() for ext in ['.m3u8', '.mp4', '.ts']) and len(url) > 50):
                            video_urls.add(url)
                except:
                    continue
            logging.info(f"✅ Found {len(video_urls)} network video URLs")
            return list(video_urls)
        except Exception as e:
            logging.error(f"Network log error: {e}")
            return []

    def find_video_in_dom(self):
        try:
            videos = self.driver.find_elements(By.TAG_NAME, 'video')
            for video in videos:
                src = video.get_attribute('src')
                if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                    return src
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                        return src
            return None
        except:
            return None

    def _scrape_blocking(self, url: str):
        try:
            self.setup_driver()
            if not self.driver:
                return None

            # **STEP 1: Open page**
            logging.info(f"🌐 Opening page: {url}")
            self.driver.get(url)
            logging.info("✅ Page opened")

            # **STEP 2: Find play button (don't click yet)**
            logging.info("🎬 Step 1: Finding play button...")
            play_selectors = [
                ".vjs-big-play-button", ".vjs-play-control", ".plyr__control--play",
                ".play-button", "[aria-label*='play']", "[data-purpose='PLAY']",
                "button[title*='play']", ".video-play-button", ".play"
            ]
            
            play_button = None
            wait = WebDriverWait(self.driver, 8)
            
            for selector in play_selectors:
                try:
                    play_button = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                    logging.info(f"✅ Found play button: {selector}")
                    break
                except TimeoutException:
                    continue

            if not play_button:
                logging.warning("⚠️ No play button found, trying JS play later")

            # **STEP 3: Nuke ads FIRST**
            logging.info("💣 Step 2: Nuking ads...")
            self.remove_all_overlays()
            time.sleep(1)

            # **STEP 4: Click play button**
            logging.info("🎬 Step 3: Clicking play button...")
            if play_button:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", play_button)
                    self.driver.execute_script("arguments[0].click();", play_button)
                    logging.info("✅ Play button clicked")
                except:
                    logging.warning("⚠️ Play button click failed, using JS fallback")
                    self.driver.execute_script("document.querySelector('video')?.play();")
            else:
                self.driver.execute_script("document.querySelector('video')?.play();")
            
            time.sleep(3)  # Wait for stream to start

            # **STEP 5: Nuke ads AGAIN**
            logging.info("💣 Step 4: Final ad cleanup...")
            self.remove_all_overlays()

            # **STEP 6: Check backend response (network logs)**
            logging.info("🔍 Step 5: Checking backend responses...")
            time.sleep(4)  # Let network requests complete
            video_urls = self.get_video_urls_from_network()

            # Prioritize m3u8 > mp4
            m3u8_urls = [u for u in video_urls if '.m3u8' in u.lower()]
            if m3u8_urls:
                best_url = max(m3u8_urls, key=len)
                logging.info(f"✅ Found m3u8: {best_url[:80]}...")
                return best_url
            
            mp4_urls = [u for u in video_urls if '.mp4' in u.lower()]
            if mp4_urls:
                logging.info(f"✅ Found mp4: {mp4_urls[0][:80]}...")
                return mp4_urls[0]

            # Fallback: DOM check
            logging.info("🔍 Fallback: Checking DOM...")
            dom_url = self.find_video_in_dom()
            if dom_url:
                logging.info(f"✅ Found in DOM: {dom_url[:80]}...")
                return dom_url

            logging.warning("❌ No video URL found")
            return None

        except Exception as e:
            logging.error(f"❌ Scraping error: {e}")
            return None
        finally:
            self.close_driver()

    async def scrape_video_url(self, url: str):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._scrape_blocking, url)


async def download_video(url: str, filename: str, status_msg=None):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://hentaihaven.com/'
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=1800)) as resp:
                with open(filename, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024*1024):
                        f.write(chunk)
        logging.info(f"✅ Download complete: {filename}")
        return True
    except Exception as e:
        logging.error(f"❌ Download error: {e}")
        return False


@Client.on_message(filters.command("dl2"))
async def download_command(client: Client, message: Message):
    try:
        if len(message.command) < 2 or "hentaihaven.com" not in message.command[1]:
            return await message.reply_text("❌ **Usage:** `/dl2 <hentaihaven_url>`")

        url = message.command[1]
        status_msg = await message.reply_text("🔍 **Scraping...** (30-60s)")

        scraper = VideoScraper()
        video_url = await scraper.scrape_video_url(url)

        if not video_url:
            await status_msg.edit_text("❌ **No video found!** Check logs.")
            return

        await status_msg.edit_text(f"✅ **Video ready!** Downloading...\n`{video_url[:80]}...`")

        video_title = re.sub(r'[^a-zA-Z0-9._-]', '_', url.split('/')[-1])
        filename = f"{video_title}_{int(time.time())}.mp4"

        if await download_video(video_url, filename, status_msg):
            file_size_mb = os.path.getsize(filename) / (1024 * 1024)
            if file_size_mb > 2000:
                os.remove(filename)
                return await status_msg.edit_text(f"❌ **Too large!** ({file_size_mb:.1f}MB)")

            await message.reply_video(
                video=filename,
                caption=f"🎬 **Done!** {file_size_mb:.1f}MB\n🔗 {url}",
                supports_streaming=True
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text(f"❌ **Download failed!**\n`{video_url}`")

    except Exception as e:
        logging.error(f"❌ Error: {e}")
        await message.reply_text(f"❌ **Error:** `{str(e)[:100]}`")
    finally:
        if 'filename' in locals() and os.path.exists(filename):
            os.remove(filename)
