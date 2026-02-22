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
        options.add_argument("--window-size=1280,720")  # Reduced from 1920x1080
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--ignore-ssl-errors")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-sync")
        options.add_argument("--mute-audio")
        options.add_argument("--no-first-run")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--page-load-strategy=eager")
        options.add_argument("--disable-images")  # Save memory
        options.add_argument("--blink-settings=imagesEnabled=false")  # Save memory
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Enable performance logging for network capture
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        
        self.driver = uc.Chrome(options=options)
        self.driver.set_page_load_timeout(20)  # Reduced from 30
        self.driver.set_script_timeout(20)
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
        """Aggressively remove ads and overlays"""
        try:
            js_script = """
            // Remove fixed/absolute positioned overlays
            document.querySelectorAll('*').forEach(el => {
                const style = window.getComputedStyle(el);
                const pos = style.position;
                const zIndex = parseInt(style.zIndex) || 0;
                
                if ((pos === 'fixed' || pos === 'absolute') && 
                    !el.querySelector('video') && 
                    (zIndex > 100 || el.offsetWidth > window.innerWidth * 0.8)) {
                    el.remove();
                }
            });
            
            // Remove common ad elements
            const adSelectors = [
                '[id*="ad"]', '[class*="ad-"]', '[class*="Ad-"]', 
                '[class*="popup"]', '[class*="overlay"]', '[class*="modal"]',
                '[id*="banner"]', '[class*="banner"]',
                'iframe:not([src*="video"]):not([src*="player"])'
            ];
            
            adSelectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    if (!el.querySelector('video')) el.remove();
                });
            });
            
            // Force body overflow visible
            document.body.style.overflow = 'visible';
            document.documentElement.style.overflow = 'visible';
            
            console.log('✅ Overlays nuked!');
            """
            self.driver.execute_script(js_script)
            logging.info("✅ Overlays removed")
        except Exception as e:
            logging.error(f"Error removing overlays: {e}")

    def get_video_urls_from_network(self):
        """Extract video URLs from network logs"""
        video_urls = set()
        try:
            logs = self.driver.get_log('performance')
            for log in logs:
                try:
                    message = json.loads(log['message'])['message']
                    method = message.get('method', '')
                    
                    if method in ['Network.responseReceived', 'Network.requestWillBeSent']:
                        params = message.get('params', {})
                        
                        # Get URL from response or request
                        url = (params.get('response', {}).get('url') or 
                               params.get('request', {}).get('url', ''))
                        
                        if url and len(url) > 30:
                            # Check if it's a video URL
                            if any(ext in url.lower() for ext in ['.m3u8', '.mp4', '.ts', '/playlist.m3u8']):
                                video_urls.add(url)
                            # Also check for streaming domains
                            elif any(domain in url.lower() for domain in ['hentaihaven', 'stream', 'cdn', 'video']):
                                if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'manifest']):
                                    video_urls.add(url)
                except Exception as e:
                    continue
                    
            logging.info(f"✅ Found {len(video_urls)} network video URLs")
            return list(video_urls)
        except Exception as e:
            logging.error(f"Network log error: {e}")
            return []

    def find_video_in_dom(self):
        """Find video element in DOM"""
        try:
            videos = self.driver.find_elements(By.TAG_NAME, 'video')
            for video in videos:
                # Check video src
                src = video.get_attribute('src')
                if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                    return src
                
                # Check source elements
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                        return src
            return None
        except:
            return None

    def find_and_prepare_play_button(self):
        """Find play button without clicking (faster approach with iframe support)"""
        try:
            # First check if there's an iframe (common for video players)
            iframes = self.driver.find_elements(By.TAG_NAME, 'iframe')
            
            # Comprehensive selector list
            all_selectors = [
                # Video.js (most common)
                ".vjs-big-play-button",
                ".vjs-play-control",
                "button.vjs-big-play-button",
                
                # Aria labels (accessibility)
                "button[aria-label*='Play']",
                "button[aria-label*='play']",
                "[aria-label*='Play' i]",
                
                # Generic classes
                ".play-button",
                ".play-btn",
                ".player-play-button",
                "[class*='play-button']",
                "[class*='play-btn']",
                "[class*='PlayButton']",
                
                # Plyr
                ".plyr__control--play",
                "button.plyr__control[data-plyr='play']",
                
                # JW Player
                ".jw-icon-playback",
                ".jw-display-icon-container",
                
                # Title/data attributes
                "button[title*='Play' i]",
                "button[data-purpose*='play' i]",
                "button[data-testid*='play' i]",
                
                # SVG/Icon based
                "button svg[class*='play']",
                "button .fa-play",
                
                # Generic buttons near video
                "button[class*='video']",
                ".video-overlay button"
            ]
            
            # Try in main document first
            for selector in all_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        # Check if element is visible and interactable
                        if elem.is_displayed() and elem.is_enabled():
                            logging.info(f"✅ Found play button: {selector}")
                            return elem
                except:
                    continue
            
            # Try inside iframes if no button found
            for idx, iframe in enumerate(iframes):
                try:
                    self.driver.switch_to.frame(iframe)
                    logging.info(f"🔍 Checking iframe {idx+1}/{len(iframes)}")
                    
                    for selector in all_selectors:
                        try:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                            for elem in elements:
                                if elem.is_displayed() and elem.is_enabled():
                                    logging.info(f"✅ Found play button in iframe: {selector}")
                                    return elem
                        except:
                            continue
                    
                    # Check for video in iframe
                    try:
                        video = self.driver.find_element(By.TAG_NAME, 'video')
                        if video.is_displayed():
                            logging.info(f"✅ Found video in iframe {idx+1}")
                            return video
                    except:
                        pass
                    
                    self.driver.switch_to.default_content()
                except:
                    self.driver.switch_to.default_content()
                    continue
            
            # Final fallback: video element in main document
            try:
                video = self.driver.find_element(By.TAG_NAME, 'video')
                if video.is_displayed():
                    logging.info("✅ Found video element (will click directly)")
                    return video
            except:
                pass
            
            logging.warning("⚠️ No play button found")
            return None
            
        except Exception as e:
            logging.error(f"Error finding play button: {e}")
            self.driver.switch_to.default_content()  # Ensure we're back to main frame
            return None

    def _scrape_blocking(self, url: str):
        try:
            self.setup_driver()
            if not self.driver:
                return None

            # **STEP 1: Open page**
            logging.info(f"🌐 Opening page: {url}")
            self.driver.get(url)
            time.sleep(2)  # Let page stabilize
            logging.info("✅ Page opened")

            # **STEP 2: Find play button (don't click yet)**
            logging.info("🎬 Step 1: Finding play button...")
            
            # Optional: Take screenshot for debugging (disable in production)
            # try:
            #     self.driver.save_screenshot('/tmp/page_before_play.png')
            #     logging.info("📸 Screenshot saved")
            # except:
            #     pass
            
            play_button = self.find_and_prepare_play_button()

            # **STEP 3: Nuke ads FIRST (before clicking)**
            logging.info("💣 Step 2: Nuking ads (pre-play)...")
            self.remove_all_overlays()
            time.sleep(0.5)

            # **STEP 4: Click play button**
            logging.info("🎬 Step 3: Clicking play button...")
            play_clicked = False
            
            if play_button:
                try:
                    # Scroll into view
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", 
                        play_button
                    )
                    time.sleep(0.3)
                    
                    # Try multiple click methods
                    try:
                        play_button.click()
                        logging.info("✅ Play button clicked (standard)")
                        play_clicked = True
                    except Exception as e1:
                        try:
                            self.driver.execute_script("arguments[0].click();", play_button)
                            logging.info("✅ Play button clicked (JS)")
                            play_clicked = True
                        except Exception as e2:
                            logging.warning(f"⚠️ Both clicks failed: {e1}, {e2}")
                    
                    # Switch back to main content if we were in iframe
                    self.driver.switch_to.default_content()
                    
                except Exception as e:
                    logging.warning(f"⚠️ Click error: {e}")
                    self.driver.switch_to.default_content()
            
            # Fallback: force play via JS if button click didn't work
            if not play_clicked:
                logging.info("⚠️ Using JS play fallback")
                # Try in main frame
                self.driver.execute_script("""
                    const video = document.querySelector('video');
                    if (video) {
                        video.muted = true;
                        video.play();
                        console.log('Video play attempted (main)');
                    }
                """)
                
                # Also try in iframes
                self.driver.execute_script("""
                    const iframes = document.querySelectorAll('iframe');
                    iframes.forEach(iframe => {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            const video = iframeDoc.querySelector('video');
                            if (video) {
                                video.muted = true;
                                video.play();
                                console.log('Video play attempted (iframe)');
                            }
                        } catch(e) {}
                    });
                """)
            
            time.sleep(2)  # Wait for stream to initialize

            # **STEP 5: Nuke ads AGAIN (post-play)**
            logging.info("💣 Step 4: Final ad cleanup (post-play)...")
            self.remove_all_overlays()
            time.sleep(0.5)

            # **STEP 6: Check backend response (network logs)**
            logging.info("🔍 Step 5: Checking backend responses...")
            time.sleep(3)  # Let network requests complete
            
            video_urls = self.get_video_urls_from_network()

            # Prioritize m3u8 > mp4
            m3u8_urls = [u for u in video_urls if '.m3u8' in u.lower()]
            if m3u8_urls:
                # Get the longest URL (usually master playlist)
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
