import asyncio
import re
import logging
from typing import Optional
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

import config

logger = logging.getLogger(__name__)


class HanimeScraper:
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.playwright = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=config.HEADLESS_BROWSER,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        logger.info("Browser started.")

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser stopped.")

    async def _new_context(self) -> BrowserContext:
        return await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

    # ------------------------------------------------------------------ #
    #  SEARCH — actually types into the search bar like a human           #
    # ------------------------------------------------------------------ #
    async def search(self, query: str) -> list[dict]:
        """
        Opens hanime.tv/search, finds the search input, types the query
        character by character, waits for results to load, then scrapes them.
        Returns: [{"title": str, "url": str, "thumb": str}]
        """
        context = await self._new_context()
        page = await context.new_page()
        results = []

        try:
            logger.info("Navigating to hanime.tv/search…")
            await page.goto("https://hanime.tv/search", wait_until="networkidle", timeout=60000)

            # ── Find the search input ──────────────────────────────────
            search_input = None
            input_selectors = [
                "input[type='search']",
                "input[placeholder*='Search']",
                "input[placeholder*='search']",
                ".search-bar input",
                "[class*='search'] input",
                "input[name='query']",
                "input[name='search']",
                "input",                         # last resort: first input on page
            ]

            for sel in input_selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    if el:
                        search_input = el
                        logger.info(f"Search input found with selector: {sel}")
                        break
                except Exception:
                    continue

            if not search_input:
                logger.error("Could not find search input on hanime.tv/search")
                return []

            # ── Click, clear, then type the query ─────────────────────
            await search_input.click()
            await asyncio.sleep(0.3)

            # Clear any existing text
            await search_input.triple_click()
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.2)

            # Type like a human — character by character with small delays
            await search_input.type(query, delay=80)
            logger.info(f"Typed query: '{query}'")

            # Press Enter to trigger search
            await page.keyboard.press("Enter")
            logger.info("Pressed Enter, waiting for results…")

            # ── Wait for results to appear ─────────────────────────────
            result_selectors = [
                ".htv-card",
                "[class*='video-card']",
                "[class*='VideoCard']",
                "[class*='card']",
                "a[href*='/videos/hentai/']",
            ]

            loaded = False
            for sel in result_selectors:
                try:
                    await page.wait_for_selector(sel, timeout=15000)
                    loaded = True
                    logger.info(f"Results loaded, detected with: {sel}")
                    break
                except Exception:
                    continue

            if not loaded:
                # Give it one more chance — wait for network idle
                await page.wait_for_load_state("networkidle", timeout=15000)

            # Small extra wait for JS rendering
            await asyncio.sleep(1.5)

            # ── Scrape result cards ────────────────────────────────────
            for sel in result_selectors:
                cards = await page.query_selector_all(sel)
                if cards:
                    logger.info(f"Found {len(cards)} cards with selector: {sel}")

                    seen_urls = set()
                    for card in cards:
                        try:
                            # Get the link
                            a_el = await card.query_selector("a")
                            if not a_el:
                                # The card itself might be an <a>
                                tag = await card.evaluate("el => el.tagName.toLowerCase()")
                                if tag == "a":
                                    a_el = card

                            href = await a_el.get_attribute("href") if a_el else None
                            if not href or "/videos/hentai/" not in href:
                                continue
                            if not href.startswith("http"):
                                href = "https://hanime.tv" + href
                            if href in seen_urls:
                                continue
                            seen_urls.add(href)

                            # Get title
                            title_el = await card.query_selector(
                                "[class*='title'], [class*='name'], h3, h4, span, p"
                            )
                            title = (
                                (await title_el.inner_text()).strip()
                                if title_el
                                else href.rstrip("/").split("/")[-1].replace("-", " ").title()
                            )

                            # Get thumbnail
                            img_el = await card.query_selector("img")
                            thumb = await img_el.get_attribute("src") if img_el else ""

                            results.append({
                                "title": title or href.split("/")[-1],
                                "url": href,
                                "thumb": thumb or "",
                            })

                            if len(results) >= config.MAX_SEARCH_RESULTS:
                                break

                        except Exception as e:
                            logger.debug(f"Card parse error: {e}")

                    if results:
                        break  # Stop trying selectors once we have results

        except Exception as e:
            logger.error(f"Search error: {e}")
        finally:
            await context.close()

        logger.info(f"Search returned {len(results)} results for '{query}'")
        return results

    # ------------------------------------------------------------------ #
    #  GET SERIES EPISODES                                                 #
    # ------------------------------------------------------------------ #
    async def get_series_episodes(self, episode_url: str) -> list[dict]:
        """
        Given any episode URL, scrape the full episode list for that series.
        Returns: [{"title": str, "url": str, "number": int}]
        """
        context = await self._new_context()
        page = await context.new_page()
        episodes = []

        try:
            logger.info(f"Fetching episode list from: {episode_url}")
            await page.goto(episode_url, wait_until="networkidle", timeout=60000)

            ep_selectors = [
                ".episodes-wrapper a",
                "[class*='episode'] a",
                "[class*='Episode'] a",
                ".related-videos a",
                "[class*='playlist'] a",
                "[class*='Playlist'] a",
                "[class*='series'] a",
                "a[href*='/videos/hentai/']",
            ]

            ep_links = []
            for sel in ep_selectors:
                ep_links = await page.query_selector_all(sel)
                if ep_links:
                    logger.info(f"Episode links found with selector: {sel}")
                    break

            seen = set()
            for el in ep_links:
                try:
                    href = await el.get_attribute("href")
                    if not href or "/videos/hentai/" not in href:
                        continue
                    if not href.startswith("http"):
                        href = "https://hanime.tv" + href
                    if href in seen:
                        continue
                    seen.add(href)

                    title_el = await el.query_selector("[class*='title'], span, p")
                    title = (
                        (await title_el.inner_text()).strip()
                        if title_el
                        else (await el.inner_text()).strip()
                    )
                    if not title:
                        title = href.rstrip("/").split("/")[-1].replace("-", " ").title()

                    num_match = re.search(r"(\d+)\s*$", href.rstrip("/").split("/")[-1])
                    num = int(num_match.group(1)) if num_match else 0

                    episodes.append({"title": title, "url": href, "number": num})
                except Exception as e:
                    logger.debug(f"Episode link parse error: {e}")

            episodes.sort(key=lambda x: x["number"])

        except Exception as e:
            logger.error(f"Episode list error: {e}")
        finally:
            await context.close()

        logger.info(f"Found {len(episodes)} episodes for {episode_url}")
        return episodes

    # ------------------------------------------------------------------ #
    #  GET CDN URL (mp4 / m3u8) via network interception                  #
    # ------------------------------------------------------------------ #
    async def get_cdn_url(self, video_url: str) -> Optional[str]:
        """
        Intercept network requests to find the CDN video URL (mp4 or m3u8).
        Returns the first matching URL or None.
        """
        context = await self._new_context()
        page = await context.new_page()
        cdn_url: Optional[str] = None

        cdn_pattern = re.compile(
            r"https?://[^\s\"']+\.(?:m3u8|mp4)[^\s\"']*", re.IGNORECASE
        )

        found_event = asyncio.Event()

        async def handle_request(request):
            nonlocal cdn_url
            if cdn_url:
                return
            url = request.url
            if cdn_pattern.search(url):
                cdn_url = url
                logger.info(f"CDN URL intercepted (request): {url[:80]}")
                found_event.set()

        async def handle_response(response):
            nonlocal cdn_url
            if cdn_url:
                return
            url = response.url
            if cdn_pattern.search(url):
                cdn_url = url
                logger.info(f"CDN URL intercepted (response): {url[:80]}")
                found_event.set()

        page.on("request", lambda req: asyncio.ensure_future(handle_request(req)))
        page.on("response", lambda res: asyncio.ensure_future(handle_response(res)))

        try:
            logger.info(f"Loading video page: {video_url}")
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)

            # Try clicking the play button to trigger video load
            play_selectors = [
                ".vjs-big-play-button",
                "button[aria-label='Play']",
                "[class*='play-btn']",
                "[class*='PlayBtn']",
                ".play-button",
            ]
            for sel in play_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        logger.info(f"Clicked play button: {sel}")
                        break
                except Exception:
                    continue

            # Wait up to 20s for CDN URL via network intercept
            try:
                await asyncio.wait_for(found_event.wait(), timeout=20)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for CDN URL via network intercept.")

            # Fallback 1: parse raw page source
            if not cdn_url:
                content = await page.content()
                match = cdn_pattern.search(content)
                if match:
                    cdn_url = match.group(0)
                    logger.info(f"CDN URL found in page source: {cdn_url[:80]}")

            # Fallback 2: check <video> / <source> elements
            if not cdn_url:
                for sel in ["video source[src]", "video[src]", "source[src]"]:
                    el = await page.query_selector(sel)
                    if el:
                        src = await el.get_attribute("src")
                        if src and cdn_pattern.search(src):
                            cdn_url = src
                            logger.info(f"CDN URL from <video> element: {cdn_url[:80]}")
                            break

        except Exception as e:
            logger.error(f"CDN extraction error: {e}")
        finally:
            await context.close()

        return cdn_url


# Singleton instance
scraper = HanimeScraper()
