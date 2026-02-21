import os
import asyncio
import time
import json
from pyrogram import Client, filters
from pyrogram.types import Message
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bot import *
import requests
from urllib.parse import urlparse, urljoin
import re

class VideoScraper:
    def __init__(self):
        self.driver = None
        self.video_url = None
        
    def setup_driver(self):
        """Setup undetected Chrome driver with optimized options"""
        options = uc.ChromeOptions()
        options.add_argument('--headless=new')
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
        
        # Enable performance logging
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL', 'browser': 'ALL'})
        
        self.driver = uc.Chrome(options=options, version_main=None)
        self.driver.set_page_load_timeout(30)
        
        # Enable network tracking
        self.driver.execute_cdp_cmd('Network.enable', {})
        
    def close_driver(self):
        """Close the driver safely"""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            self.driver = None
    
    def remove_all_overlays(self):
        """Remove all overlay elements including ads and popups"""
        try:
            # JavaScript to remove all overlays
            js_script = """
            // Remove all fixed/absolute positioned elements that might be overlays
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
            
            // Remove common ad containers
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
            print("Overlays and ads removed")
        except Exception as e:
            print(f"Error removing overlays: {e}")
    
    def get_video_urls_from_network(self):
        """Extract video URLs from network logs"""
        video_urls = []
        try:
            logs = self.driver.get_log('performance')
            
            for log in logs:
                try:
                    message = json.loads(log['message'])['message']
                    
                    # Check for network responses
                    if message['method'] == 'Network.responseReceived':
                        response = message['params']['response']
                        url = response['url']
                        mime_type = response.get('mimeType', '')
                        
                        # Look for video content
                        if (any(ext in url.lower() for ext in ['.m3u8', '.mp4', '.ts', 'manifest', '.m4s']) or
                            'video' in mime_type):
                            # Exclude HentaiHaven's own domain for CDN links
                            if 'hentaihaven.com' not in url or any(cdn in url for cdn in ['cdn', 'stream', 'video']):
                                video_urls.append(url)
                                print(f"Found potential video URL: {url}")
                                
                    # Check for XHR requests with video data
                    elif message['method'] == 'Network.requestWillBeSent':
                        request = message['params']['request']
                        url = request['url']
                        
                        if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'manifest']):
                            video_urls.append(url)
                            print(f"Found video URL in request: {url}")
                            
                except Exception as e:
                    continue
            
            # Remove duplicates while preserving order
            seen = set()
            unique_urls = []
            for url in video_urls:
                if url not in seen:
                    seen.add(url)
                    unique_urls.append(url)
                    
            return unique_urls
        except Exception as e:
            print(f"Error getting video URLs from network: {e}")
            return []
    
    def find_video_in_dom(self):
        """Find video URLs in DOM elements"""
        try:
            # Check video elements
            video_elements = self.driver.find_elements(By.TAG_NAME, 'video')
            for video in video_elements:
                # Check src attribute
                src = video.get_attribute('src')
                if src and any(ext in src for ext in ['.mp4', '.m3u8']):
                    return src
                
                # Check source children
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and any(ext in src for ext in ['.mp4', '.m3u8']):
                        return src
            
            # Check for iframe players
            iframes = self.driver.find_elements(By.TAG_NAME, 'iframe')
            for iframe in iframes:
                src = iframe.get_attribute('src')
                if src and 'player' in src.lower():
                    print(f"Found player iframe: {src}")
                    # Could switch to iframe and search there
                    
            return None
        except Exception as e:
            print(f"Error finding video in DOM: {e}")
            return None
    
    def extract_from_page_source(self):
        """Extract video URLs from page source"""
        try:
            page_source = self.driver.page_source
            
            # Patterns for video URLs
            patterns = [
                r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
                r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*',
                r'"file"\s*:\s*"([^"]+)"',
                r'"src"\s*:\s*"([^"]+\.(?:mp4|m3u8)[^"]*)"',
                r"'file'\s*:\s*'([^']+)'",
                r'source:\s*"([^"]+)"',
                r'url:\s*"([^"]+\.(?:mp4|m3u8)[^"]*)"',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, page_source, re.IGNORECASE)
                for match in matches:
                    url = match if isinstance(match, str) else match[0]
                    if url.startswith('http') and any(ext in url for ext in ['.m3u8', '.mp4']):
                        return url
            
            return None
        except Exception as e:
            print(f"Error extracting from page source: {e}")
            return None
    
    def wait_for_video_load(self):
        """Wait for video player to load"""
        try:
            # Wait for video element or player
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "video"))
            )
            print("Video element found")
            return True
        except TimeoutException:
            print("Video element not found, continuing anyway...")
            return False
    
    async def scrape_video_url(self, url: str):
        """Main scraping function with multiple fallback methods"""
        try:
            self.setup_driver()
            print(f"Loading page: {url}")
            self.driver.get(url)
            
            # Wait for initial page load
            time.sleep(5)
            
            # Remove initial overlays
            self.remove_all_overlays()
            
            # Find and click play button
            print("Looking for play button...")
            play_button_selectors = [
                "button.play-button",
                "button.vjs-big-play-button",
                ".vjs-big-play-button",
                "button[class*='play']",
                "div.play-button",
                "div[class*='play-overlay']",
                ".plyr__control--overlaid",
                "button[aria-label*='Play']",
                "button[title*='Play']",
            ]
            
            play_clicked = False
            for selector in play_button_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for element in elements:
                        if element.is_displayed():
                            try:
                                # Scroll into view
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                                time.sleep(0.5)
                                
                                # Try clicking
                                try:
                                    element.click()
                                except:
                                    self.driver.execute_script("arguments[0].click();", element)
                                
                                print(f"Clicked play button: {selector}")
                                play_clicked = True
                                break
                            except:
                                continue
                    if play_clicked:
                        break
                except:
                    continue
            
            if not play_clicked:
                print("No play button found, trying auto-play detection")
            
            # Wait for video to start loading
            time.sleep(5)
            
            # Remove ads again after play
            self.remove_all_overlays()
            
            # Wait for video player
            self.wait_for_video_load()
            
            # Wait a bit more for network requests
            time.sleep(3)
            
            # Method 1: Get from network logs
            print("Checking network logs...")
            video_urls = self.get_video_urls_from_network()
            
            if video_urls:
                # Prefer m3u8 over mp4, and longer URLs (usually more complete)
                m3u8_urls = [u for u in video_urls if '.m3u8' in u]
                mp4_urls = [u for u in video_urls if '.mp4' in u]
                
                if m3u8_urls:
                    return max(m3u8_urls, key=len)
                elif mp4_urls:
                    return max(mp4_urls, key=len)
                elif video_urls:
                    return video_urls[0]
            
            # Method 2: Check DOM
            print("Checking DOM...")
            video_url = self.find_video_in_dom()
            if video_url:
                return video_url
            
            # Method 3: Page source
            print("Checking page source...")
            video_url = self.extract_from_page_source()
            if video_url:
                return video_url
            
            print("No video URL found")
            return None
            
        except Exception as e:
            print(f"Error during scraping: {e}")
            import traceback
            traceback.print_exc()
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
                for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Update progress every 10MB
                        if status_msg and downloaded - last_update > 10*1024*1024:
                            progress = (downloaded / total_size) * 100
                            await status_msg.edit_text(
                                f"⬇️ **Downloading...**\n\n"
                                f"Progress: {progress:.1f}%\n"
                                f"Downloaded: {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB"
                            )
                            last_update = downloaded
        
        return True
    except Exception as e:
        print(f"Error downloading video: {e}")
        return False

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "**🎬 HentaiHaven Video Downloader Bot**\n\n"
        "This bot can download videos from HentaiHaven.com\n\n"
        "**Usage:**\n"
        "`/dl2 <url>` - Download video\n\n"
        "**Example:**\n"
        "`/dl2 https://hentaihaven.com/video/arisugawa-ren-tte-honto-wa-onn/episode-5/`\n\n"
        "**Note:** Large files may take time to process and upload."
    )

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
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
        "• Processing time depends on video length\n"
        "• Some videos may be geo-restricted"
    )

@app.on_message(filters.command("dl2"))
async def download_command(client: Client, message: Message):
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
        
        # Validate URL
        if "hentaihaven.com" not in url:
            await message.reply_text("❌ **Invalid URL!** Please provide a valid HentaiHaven.com URL.")
            return
        
        status_msg = await message.reply_text("🔍 **Initializing scraper...**")
        
        # Scrape video URL
        await status_msg.edit_text("🔍 **Scraping video URL...**\n\nThis may take 30-60 seconds...")
        
        scraper = VideoScraper()
        video_url = await scraper.scrape_video_url(url)
        
        if not video_url:
            await status_msg.edit_text(
                "❌ **Failed to extract video URL!**\n\n"
                "**Possible reasons:**\n"
                "• Video is protected or encrypted\n"
                "• Page structure has changed\n"
                "• Video is geo-restricted\n"
                "• Invalid or expired link\n\n"
                "Please try again or check if the video plays in your browser."
            )
            return
        
        await status_msg.edit_text(
            f"✅ **Video URL found!**\n\n"
            f"🔗 CDN URL: `{video_url[:100]}...`\n\n"
            f"⬇️ **Starting download...**"
        )
        
        # Generate filename from URL
        video_title = url.split('/')[-2] if url.split('/')[-1] == '' else url.split('/')[-1]
        video_title = video_title.split('?')[0]
        filename = f"{video_title}_{int(time.time())}.mp4"
        
        # Download video
        success = await download_video(video_url, filename, status_msg)
        
        if not success or not os.path.exists(filename):
            await status_msg.edit_text(
                "❌ **Download failed!**\n\n"
                "**Direct video URL:**\n"
                f"`{video_url}`\n\n"
                "You can try downloading manually with this URL."
            )
            return
        
        # Check file size
        file_size = os.path.getsize(filename)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > 2000:  # 2GB limit
            await status_msg.edit_text(
                f"❌ **File too large!** ({file_size_mb:.1f}MB)\n\n"
                f"Telegram has a 2GB upload limit.\n\n"
                f"**Direct video URL:**\n"
                f"`{video_url}`"
            )
            os.remove(filename)
            return
        
        # Upload video
        await status_msg.edit_text(
            f"📤 **Uploading to Telegram...**\n\n"
            f"File size: {file_size_mb:.1f}MB"
        )
        
        await message.reply_video(
            video=filename,
            caption=f"🎬 **Video Downloaded**\n\n📁 Size: {file_size_mb:.1f}MB\n🔗 Source: {url}",
            supports_streaming=True,
            progress=lambda current, total: asyncio.create_task(
                status_msg.edit_text(
                    f"📤 **Uploading to Telegram...**\n\n"
                    f"Progress: {(current/total)*100:.1f}%\n"
                    f"Uploaded: {current/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB"
                )
            ) if current % (10*1024*1024) == 0 else None
        )
        
        await status_msg.delete()
        
        # Clean up
        if os.path.exists(filename):
            os.remove(filename)
            print(f"Cleaned up: {filename}")
            
    except Exception as e:
        error_msg = str(e)
        await message.reply_text(f"❌ **Error occurred:**\n\n`{error_msg}`")
        print(f"Error in download_command: {e}")
        import traceback
        traceback.print_exc()
