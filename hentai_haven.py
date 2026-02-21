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
from bot import app  # Assuming app is defined in bot.py
import requests
from urllib.parse import urlparse, urljoin
import re

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class VideoScraper:
    def __init__(self):
        self.driver = None
        self.video_url = None
        
    def setup_driver(self):
        """Setup undetected Chrome driver - HEADLESS DISABLED for reliability"""
        options = uc.ChromeOptions()
        # options.add_argument('--headless=new')  # DISABLED - enable after testing
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--mute-audio')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # Enable performance logging
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL', 'browser': 'ALL'})
        
        self.driver = uc.Chrome(options=options, version_main=120)
        self.driver.set_page_load_timeout(30)
        
        # Anti-detection
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # Enable network tracking
        self.driver.execute_cdp_cmd('Network.enable', {})
        self.driver.execute_cdp_cmd('Performance.enable', {})
        logging.info("Driver setup complete")
        
    def close_driver(self):
        """Close the driver safely"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logging.error(f"Error closing driver: {e}")
            self.driver = None
    
    def remove_all_overlays(self):
        """Remove all overlay elements including ads and popups"""
        try:
            js_script = """
            var elements = document.querySelectorAll('*');
            elements.forEach(function(el) {
                var style = window.getComputedStyle(el);
                if ((style.position === 'fixed' || style.position === 'absolute') && 
                    (el.tagName === 'DIV' || el.tagName === 'IFRAME') &&
                    !el.querySelector('video')) {
                    var zIndex = parseInt(style.zIndex);
                    if (zIndex > 100 || style.zIndex === 'auto') {
                        el.remove();
                    }
                }
            });
            
            var adSelectors = [
                '[id*="ad"]', '[class*="ad"]', '[id*="popup"]', 
                '[class*="popup"]', '[class*="overlay"]', '[id*="modal"]',
                '[class*="modal"]', 'iframe[src*="ads"]'
            ];
            
            adSelectors.forEach(function(selector) {
                document.querySelectorAll(selector).forEach(function(el) {
                    if (!el.querySelector('video')) {
                        el.remove();
                    }
                });
            });
            """
            self.driver.execute_script(js_script)
            logging.info("Overlays and ads removed")
        except Exception as e:
            logging.error(f"Error removing overlays: {e}")
    
    def get_video_urls_from_network(self):
        """Extract video URLs from network logs - improved for m3u8/XHR"""
        video_urls = set()
        try:
            logs = self.driver.get_log('performance')
            logging.info(f"Processing {len(logs)} network logs")
            
            for log in logs:
                try:
                    message = json.loads(log['message'])['message']
                    
                    if message['method'] in ['Network.responseReceived', 'Network.requestWillBeSent']:
                        params = message['params']
                        url = (params.get('response', {}).get('url') or 
                               params.get('request', {}).get('url', ''))
                        
                        if url and ('hentaihaven' in url or any(ext in url.lower() 
                            for ext in ['.m3u8', '.mp4', '.ts', 'manifest', '.m4s'])):
                            video_urls.add(url)
                            logging.info(f"Found video URL: {url[:100]}...")
                            
                except Exception:
                    continue
            
            unique_urls = list(video_urls)
            logging.info(f"Total unique video URLs found: {len(unique_urls)}")
            return unique_urls
            
        except Exception as e:
            logging.error(f"Error getting video URLs from network: {e}")
            return []
    
    def find_video_in_dom(self):
        """Find video URLs in DOM elements"""
        try:
            video_elements = self.driver.find_elements(By.TAG_NAME, 'video')
            for video in video_elements:
                src = video.get_attribute('src')
                if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                    logging.info(f"Found video in DOM: {src}")
                    return src
                
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                        logging.info(f"Found source in DOM: {src}")
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
                        logging.info(f"Found in source: {url}")
                        return url
            return None
        except Exception as e:
            logging.error(f"Error extracting from page source: {e}")
            return None
    
    async def scrape_video_url(self, url: str):
        """Main scraping function with improved play detection"""
        try:
            self.setup_driver()
            logging.info(f"Loading page: {url}")
            self.driver.get(url)
            
            time.sleep(5)
            self.remove_all_overlays()
            
            # Improved play button detection
            logging.info("Looking for play button...")
            play_selectors = [
                ".vjs-control-bar button.vjs-play-control",
                ".plyr__control--play", 
                "button[data-purpose='PLAY']",
                "[aria-label*='play'], [aria-label*='Play']",
                ".play-button", ".big-play-button", ".vjs-big-play-button",
                "button[title*='play'], button[title*='Play']"
            ]
            
            wait = WebDriverWait(self.driver, 15)
            play_clicked = False
            
            for selector in play_selectors:
                try:
                    play_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", play_btn)
                    time.sleep(1)
                    play_btn.click()
                    logging.info(f"✅ Clicked play button: {selector}")
                    play_clicked = True
                    break
                except TimeoutException:
                    continue
            
            if not play_clicked:
                logging.info("No play button found, trying JS play")
                self.driver.execute_script("""
                    var video = document.querySelector('video');
                    if (video) {
                        video.play();
                        console.log('JS play triggered');
                    }
                """)
            
            # Wait longer for stream initialization
            logging.info("Waiting for video stream...")
            time.sleep(12)
            
            self.remove_all_overlays()
            
            # Method 1: Network logs (primary)
            logging.info("Checking network logs...")
            video_urls = self.get_video_urls_from_network()
            
            # Select best URL
            if video_urls:
                m3u8_urls = [u for u in video_urls if '.m3u8' in u.lower()]
                if m3u8_urls:
                    best_url = max(m3u8_urls, key=len)
                    logging.info(f"Selected m3u8: {best_url[:100]}...")
                    return best_url
            
            # Method 2: DOM
            logging.info("Checking DOM...")
            dom_url = self.find_video_in_dom()
            if dom_url:
                return dom_url
            
            # Method 3: Page source
            logging.info("Checking page source...")
            src_url = self.extract_from_page_source()
            if src_url:
                return src_url
            
            logging.warning("No video URL found")
            return None
            
        except Exception as e:
            logging.error(f"Scraping error: {e}", exc_info=True)
            return None
        finally:
            self.close_driver()

async def download_video(url: str, filename: str, status_msg: Message = None):
    """Download video from URL with progress updates"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://hentaihaven.com/'
        }
        
        response = requests.get(url, stream=True, headers=headers, timeout=30)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        
        with open(filename, 'wb') as f:
            if total_size == 0:
                f.write(response.content)
            else:
                downloaded = 0
                last_update = 0
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if status_msg and downloaded - last_update > 10*1024*1024:
                            progress = (downloaded / total_size) * 100
                            await status_msg.edit_text(
                                f"⬇️ **Downloading...**\n\n"
                                f"Progress: {progress:.1f}%\n"
                                f"Downloaded: {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB"
                            )
                            last_update = downloaded
        
        logging.info(f"Download complete: {filename}")
        return True
    except Exception as e:
        logging.error(f"Download error: {e}")
        return False

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    try:
        await message.reply_text(
            "**🎬 HentaiHaven Video Downloader Bot**\n\n"
            "This bot can download videos from HentaiHaven.com\n\n"
            "**Usage:**\n"
            "`/dl2 <url>` - Download video\n\n"
            "**Example:**\n"
            "`/dl2 https://hentaihaven.com/video/arisugawa-ren-tte-honto-wa-onn/episode-5/`\n\n"
            "**Note:** Large files may take time. Check logs for debugging."
        )
    except Exception as e:
        logging.error(f"Start command error: {e}")

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    try:
        await message.reply_text(
            "**📖 Help - HentaiHaven Downloader**\n\n"
            "**Commands:**\n"
            "• `/start` - Show welcome message\n"
            "• `/dl2 <url>` - Download a video\n"
            "• `/help` - Show this help message\n\n"
            "**Supported URLs:**\n"
            "• Direct video page links from hentaihaven.com\n\n"
            "**Limitations:**\n"
            "• Maximum file size: 2GB (Telegram limit)\n"
            "• Processing time depends on video length"
        )
    except Exception as e:
        logging.error(f"Help command error: {e}")

@app.on_message(filters.command("dl2"))
async def download_command(client: Client, message: Message):
    try:
        # Extract URL
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
            await message.reply_text("❌ **Invalid URL!** Please provide a valid HentaiHaven.com URL.")
            return
        
        status_msg = await message.reply_text("🔍 **Initializing scraper...**")
        
        await status_msg.edit_text("🔍 **Scraping video URL...**\n\nThis may take 45-90 seconds...")
        
        scraper = VideoScraper()
        video_url = await scraper.scrape_video_url(url)
        
        if not video_url:
            await status_msg.edit_text(
                "❌ **Failed to extract video URL!**\n\n"
                "**Possible reasons:**\n"
                "• Anti-bot protection\n"
                "• Page structure changed\n"
                "• Try running without headless mode\n\n"
                "Check bot logs for details."
            )
            return
        
        await status_msg.edit_text(
            f"✅ **Video URL found!**\n\n"
            f"🔗 `{video_url[:100]}...`\n\n"
            f"⬇️ **Starting download...**"
        )
        
        # Generate filename
        video_title = url.split('/')[-2] if url.split('/')[-1] == '' else url.split('/')[-1]
        video_title = re.sub(r'[^\w\-_\.]', '_', video_title.split('?')[0])
        filename = f"{video_title}_{int(time.time())}.mp4"
        
        # Download
        success = await download_video(video_url, filename, status_msg)
        
        if not success or not os.path.exists(filename):
            await status_msg.edit_text(
                f"❌ **Download failed!**\n\n"
                f"**Direct URL:**\n`{video_url}`\n\n"
                "Try downloading manually."
            )
            if os.path.exists(filename):
                os.remove(filename)
            return
        
        # Check size
        file_size = os.path.getsize(filename)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > 2000:
            await status_msg.edit_text(
                f"❌ **File too large!** ({file_size_mb:.1f}MB)\n\n"
                f"Telegram limit: 2GB\n\n**Direct URL:** `{video_url}`"
            )
            os.remove(filename)
            return
        
        # Upload
        await status_msg.edit_text(
            f"📤 **Uploading...**\n\n"
            f"Size: {file_size_mb:.1f}MB"
        )
        
        await message.reply_video(
            video=filename,
            caption=f"🎬 **Downloaded Successfully!**\n\n📁 Size: {file_size_mb:.1f}MB\n🔗 {url}",
            supports_streaming=True
        )
        
        await status_msg.delete()
        
        # Cleanup
        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Download command error: {e}", exc_info=True)
        try:
            await message.reply_text(f"❌ **Unexpected error:**\n\n`{error_msg[:150]}...`\n\nCheck bot logs.")
        except:
            pass
