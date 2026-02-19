import asyncio
import re
import logging
from typing import Optional
from playwright.async_api import async_playwright, Page, Browser

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

    async def _new_page(self) -> Page:
        context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        return page

    # ------------------------------------------------------------------ #
    #  SEARCH                                                              #
    # ------------------------------------------------------------------ #
    async def search(self, query: str) -> list[dict]:
        """
        Search hanime.tv and return a deduplicated list of series/titles
        with their episode URLs grouped together.
        Returns: [{"title": str, "episodes": [{"title": str, "url": str, "thumb": str}]}]
        """
        page = await self._new_page()
        results = []

        try:
            search_url = f"https://hanime.tv/search?query={query.replace(' ', '+')}"
            logger.info(f"Searching: {search_url}")
            await page.goto(search_url, wait_until="networkidle", timeout=60000)

            # Wait for video cards
            try:
                await page.wait_for_selector(".htv-card", timeout=15000)
            except Exception:
                logger.warning("No .htv-card elements found, trying alternate selectors.")

            cards = await page.query_selector_all(".htv-card")
            if not cards:
                cards = await page.query_selector_all("[class*='card']")

            seen_urls = set()
            for card in cards[: config.MAX_SEARCH_RESULTS * 3]:
                try:
                    a_el = await card.query_selector("a")
                    href = await a_el.get_attribute("href") if a_el else None
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = "https://hanime.tv" + href

                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    # Title
                    title_el = await card.query_selector(
                        ".htv-card-title, [class*='title'], h3, h4"
                    )
                    title = (
                        (await title_el.inner_text()).strip()
                        if title_el
                        else href.split("/")[-1].replace("-", " ").title()
                    )

                    # Thumbnail
                    img_el = await card.query_selector("img")
                    thumb = (
                        await img_el.get_attribute("src")
                        if img_el
                        else ""
                    )

                    results.append({"title": title, "url": href, "thumb": thumb or ""})

                    if len(results) >= config.MAX_SEARCH_RESULTS:
                        break

                except Exception as e:
                    logger.debug(f"Card parse error: {e}")

        except Exception as e:
            logger.error(f"Search error: {e}")
        finally:
            await page.context.close()

        return results

    # ------------------------------------------------------------------ #
    #  GET SERIES EPISODES                                                 #
    # ------------------------------------------------------------------ #
    async def get_series_episodes(self, episode_url: str) -> list[dict]:
        """
        Given any episode URL, find the full series episode list.
        Returns: [{"title": str, "url": str, "number": int}]
        """
        page = await self._new_page()
        episodes = []

        try:
            logger.info(f"Fetching episode list from: {episode_url}")
            await page.goto(episode_url, wait_until="networkidle", timeout=60000)

            # Try the episode list sidebar / related section
            ep_selectors = [
                ".episodes-wrapper a",
                "[class*='episode'] a",
                ".related-videos a",
                "[class*='playlist'] a",
                "[class*='Episodes'] a",
            ]

            ep_links = []
            for sel in ep_selectors:
                ep_links = await page.query_selector_all(sel)
                if ep_links:
                    break

            if not ep_links:
                # Fallback: extract all /videos/hentai/ links on page
                all_links = await page.query_selector_all("a[href*='/videos/hentai/']")
                ep_links = all_links

            seen = set()
            for el in ep_links:
                href = await el.get_attribute("href")
                if not href:
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
                    title = href.split("/")[-1].replace("-", " ").title()

                # Try to extract episode number
                num_match = re.search(r"(\d+)\s*$", href.split("/")[-1])
                num = int(num_match.group(1)) if num_match else 0

                episodes.append({"title": title, "url": href, "number": num})

            episodes.sort(key=lambda x: x["number"])

        except Exception as e:
            logger.error(f"Episode list error: {e}")
        finally:
            await page.context.close()

        return episodes

    # ------------------------------------------------------------------ #
    #  GET CDN URL (mp4 / m3u8)                                           #
    # ------------------------------------------------------------------ #
    async def get_cdn_url(self, video_url: str) -> Optional[str]:
        """
        Intercept network requests to find the CDN video URL (mp4 or m3u8).
        Returns the first matching URL or None.
        """
        page = await self._new_page()
        cdn_url: Optional[str] = None

        cdn_patterns = re.compile(
            r"(https?://[^\s\"']+\.(?:m3u8|mp4)[^\s\"']*)", re.IGNORECASE
        )

        found_event = asyncio.Event()

        async def handle_request(request):
            nonlocal cdn_url
            url = request.url
            if cdn_patterns.search(url):
                if cdn_url is None:
                    cdn_url = url
                    logger.info(f"CDN URL intercepted: {url}")
                    found_event.set()

        async def handle_response(response):
            nonlocal cdn_url
            url = response.url
            if cdn_patterns.search(url) and cdn_url is None:
                cdn_url = url
                logger.info(f"CDN URL from response: {url}")
                found_event.set()

        page.on("request", lambda req: asyncio.ensure_future(handle_request(req)))
        page.on("response", lambda res: asyncio.ensure_future(handle_response(res)))

        try:
            logger.info(f"Loading video page: {video_url}")
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)

            # Try to click play button if present
            try:
                play_btn = await page.query_selector(
                    "button[aria-label='Play'], .vjs-big-play-button, [class*='play-btn']"
                )
                if play_btn:
                    await play_btn.click()
            except Exception:
                pass

            # Wait up to 20s for CDN URL
            try:
                await asyncio.wait_for(found_event.wait(), timeout=20)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for CDN URL via network intercept.")

            # Fallback: parse page source for video URLs
            if not cdn_url:
                content = await page.content()
                matches = cdn_patterns.findall(content)
                if matches:
                    cdn_url = matches[0]
                    logger.info(f"CDN URL from page source: {cdn_url}")

            # Fallback: check video/source elements
            if not cdn_url:
                src_el = await page.query_selector("video source, video[src]")
                if src_el:
                    cdn_url = await src_el.get_attribute("src")

        except Exception as e:
            logger.error(f"CDN extraction error: {e}")
        finally:
            await page.context.close()

        return cdn_url


# Singleton instance
scraper = HanimeScraper()
