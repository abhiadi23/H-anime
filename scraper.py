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
    #  DISMISS AGE GATE (called after every page.goto)                    #
    # ------------------------------------------------------------------ #
    async def _dismiss_age_gate(self, page: Page) -> None:
        """
        Hanime.tv shows an age-verification gate on first visit.
        Try several common selectors and click the confirm button if found.
        """
        age_gate_selectors = [
            "button[class*='confirm']",
            "button[class*='agree']",
            "button[class*='enter']",
            "[class*='age-gate'] button",
            "[class*='age_gate'] button",
            "[class*='ageGate'] button",
            "[class*='age-verify'] button",
            "button:has-text('Enter')",
            "button:has-text('I am 18')",
            "button:has-text('Yes')",
            "button:has-text('Confirm')",
        ]
        for sel in age_gate_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=4000)
                if btn:
                    await btn.click()
                    await asyncio.sleep(0.6)
                    logger.info(f"Age gate dismissed with selector: {sel}")
                    return
            except Exception:
                continue
        logger.debug("No age gate detected or already dismissed.")

    # ------------------------------------------------------------------ #
    #  SEARCH                                                              #
    # ------------------------------------------------------------------ #
    async def search(self, query: str, retries: int = 2) -> list[dict]:
        """
        Opens hanime.tv/search, types the query into the search bar, waits
        for results and scrapes them.
        Returns: [{"title": str, "url": str, "thumb": str}]
        """
        for attempt in range(1, retries + 2):
            try:
                return await self._search_attempt(query)
            except Exception as e:
                logger.warning(f"Search attempt {attempt} failed: {e}")
                if attempt > retries:
                    logger.error("All search attempts exhausted.")
                    return []
                await asyncio.sleep(2)
        return []

    async def _search_attempt(self, query: str) -> list[dict]:
        context = await self._new_context()
        page = await context.new_page()
        results = []

        try:
            logger.info("Navigating to hanime.tv/search…")
            await page.goto(
                "https://hanime.tv/search",
                wait_until="networkidle",
                timeout=60000,
            )

            # Dismiss age gate before anything else
            await self._dismiss_age_gate(page)

            # ── Find the search input ──────────────────────────────────
            input_selectors = [
                "input[type='search']",
                "input[placeholder*='Search']",
                "input[placeholder*='search']",
                ".search-bar input",
                "[class*='search'] input",
                "input[name='query']",
                "input[name='search']",
                "input",
            ]

            search_input = None
            for sel in input_selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    if el:
                        search_input = el
                        logger.info(f"Search input found: {sel}")
                        break
                except Exception:
                    continue

            if not search_input:
                raise RuntimeError("Could not find search input on hanime.tv/search")

            # ── Click, clear, type ────────────────────────────────────
            await search_input.click()
            await asyncio.sleep(0.3)
            await search_input.triple_click()
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.2)
            await search_input.type(query, delay=80)
            logger.info(f"Typed query: '{query}'")

            await page.keyboard.press("Enter")
            logger.info("Pressed Enter, waiting for results…")

            # ── Wait for result cards ─────────────────────────────────
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
                    logger.info(f"Results detected with: {sel}")
                    break
                except Exception:
                    continue

            if not loaded:
                await page.wait_for_load_state("networkidle", timeout=15000)

            await asyncio.sleep(1.5)

            # ── Scrape cards ──────────────────────────────────────────
            seen_urls: set[str] = set()

            for sel in result_selectors:
                cards = await page.query_selector_all(sel)
                if not cards:
                    continue

                logger.info(f"Found {len(cards)} cards with selector: {sel}")

                for card in cards:
                    try:
                        # Resolve the anchor element (card may itself be <a>)
                        tag = await card.evaluate("el => el.tagName.toLowerCase()")
                        a_el = card if tag == "a" else await card.query_selector("a")

                        href = await a_el.get_attribute("href") if a_el else None
                        if not href or "/videos/hentai/" not in href:
                            continue
                        if not href.startswith("http"):
                            href = "https://hanime.tv" + href
                        if href in seen_urls:
                            continue
                        seen_urls.add(href)

                        # Title
                        title_el = await card.query_selector(
                            "[class*='title'], [class*='name'], h3, h4, span, p"
                        )
                        title = (
                            (await title_el.inner_text()).strip()
                            if title_el
                            else href.rstrip("/").split("/")[-1].replace("-", " ").title()
                        )

                        # Thumbnail
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
                    break

        finally:
            await context.close()

        logger.info(f"Search returned {len(results)} results for '{query}'")
        return results

    # ------------------------------------------------------------------ #
    #  GET SERIES EPISODES                                                 #
    # ------------------------------------------------------------------ #
    async def get_series_episodes(self, episode_url: str, retries: int = 2) -> list[dict]:
        """
        Given any episode URL, scrape the full episode list for that series.
        Returns: [{"title": str, "url": str, "number": int}]
        """
        for attempt in range(1, retries + 2):
            try:
                return await self._get_series_episodes_attempt(episode_url)
            except Exception as e:
                logger.warning(f"Episode list attempt {attempt} failed: {e}")
                if attempt > retries:
                    logger.error("All episode list attempts exhausted.")
                    return []
                await asyncio.sleep(2)
        return []

    async def _get_series_episodes_attempt(self, episode_url: str) -> list[dict]:
        context = await self._new_context()
        page = await context.new_page()
        episodes = []

        try:
            logger.info(f"Fetching episode list from: {episode_url}")
            await page.goto(episode_url, wait_until="networkidle", timeout=60000)
            await self._dismiss_age_gate(page)

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

            seen: set[str] = set()
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

                    slug = href.rstrip("/").split("/")[-1]
                    num = self._extract_episode_number(slug, title)

                    episodes.append({"title": title, "url": href, "number": num})

                except Exception as e:
                    logger.debug(f"Episode link parse error: {e}")

            episodes.sort(key=lambda x: x["number"])

        finally:
            await context.close()

        logger.info(f"Found {len(episodes)} episodes for {episode_url}")
        return episodes

    def _extract_episode_number(self, slug: str, title: str) -> int:
        """
        Extract episode number from slug or title, ignoring resolution tokens
        like 720, 1080, 480 so they don't false-positive as episode numbers.
        """
        RESOLUTION_TOKENS = {144, 240, 360, 480, 720, 1080, 1440, 2160, 4320}

        # Try title first (e.g. "Episode 3", "Ep 3", "- 3")
        title_match = re.search(
            r"(?:episode|ep\.?)\s*(\d+)|[-–]\s*(\d+)\s*$",
            title,
            re.IGNORECASE,
        )
        if title_match:
            num = int(title_match.group(1) or title_match.group(2))
            if num not in RESOLUTION_TOKENS:
                return num

        # Fall back to trailing number in slug
        slug_match = re.search(r"(\d+)\s*$", slug)
        if slug_match:
            num = int(slug_match.group(1))
            if num not in RESOLUTION_TOKENS:
                return num

        return 0

    # ------------------------------------------------------------------ #
    #  GET CDN URL — context-level interception catches iframes too       #
    # ------------------------------------------------------------------ #
    async def get_cdn_url(self, video_url: str, retries: int = 2) -> Optional[str]:
        """
        Intercept all network traffic (including iframes) at the context level
        to find the CDN video URL (mp4 or m3u8).  Also inspects XHR/JSON
        response bodies for embedded CDN URLs.
        Returns the first matching URL or None.
        """
        for attempt in range(1, retries + 2):
            try:
                result = await self._get_cdn_url_attempt(video_url)
                if result:
                    return result
                logger.warning(f"CDN attempt {attempt} returned no URL, retrying…")
            except Exception as e:
                logger.warning(f"CDN attempt {attempt} failed: {e}")
            if attempt <= retries:
                await asyncio.sleep(2)

        logger.error("All CDN extraction attempts exhausted.")
        return None

    async def _get_cdn_url_attempt(self, video_url: str) -> Optional[str]:
        context = await self._new_context()
        cdn_url: Optional[str] = None
        found_event = asyncio.Event()
        loop = asyncio.get_event_loop()

        cdn_pattern = re.compile(
            r"https?://[^\s\"'<>]+\.(?:m3u8|mp4)(?:[^\s\"'<>]*)?",
            re.IGNORECASE,
        )

        # ── Handlers ──────────────────────────────────────────────────

        async def handle_request(request):
            nonlocal cdn_url
            if cdn_url:
                return
            url = request.url
            if cdn_pattern.search(url):
                cdn_url = url
                logger.info(f"CDN URL (request intercept): {url[:100]}")
                found_event.set()

        async def handle_response(response):
            nonlocal cdn_url
            if cdn_url:
                return
            url = response.url

            # Direct URL match
            if cdn_pattern.search(url):
                cdn_url = url
                logger.info(f"CDN URL (response intercept): {url[:100]}")
                found_event.set()
                return

            # Inspect JSON / text response bodies for embedded CDN URLs
            content_type = response.headers.get("content-type", "")
            if any(ct in content_type for ct in ("json", "javascript", "text/plain")):
                try:
                    body = await response.text()
                    match = cdn_pattern.search(body)
                    if match:
                        cdn_url = match.group(0).rstrip("\"'\\")
                        logger.info(f"CDN URL (XHR body): {cdn_url[:100]}")
                        found_event.set()
                except Exception as e:
                    logger.debug(f"Could not read response body for {url}: {e}")

        # ── Attach listeners at CONTEXT level (catches all iframes) ───
        context.on("request", lambda req: loop.create_task(handle_request(req)))
        context.on("response", lambda res: loop.create_task(handle_response(res)))

        page = await context.new_page()

        try:
            logger.info(f"Loading video page: {video_url}")
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
            await self._dismiss_age_gate(page)

            # Try clicking a play button to trigger video initialisation
            play_selectors = [
                ".vjs-big-play-button",
                "button[aria-label='Play']",
                "[class*='play-btn']",
                "[class*='PlayBtn']",
                ".play-button",
                "video",                   # clicking the video element itself
            ]
            for sel in play_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        logger.info(f"Clicked play element: {sel}")
                        break
                except Exception:
                    continue

            # Also try clicking inside any iframe that looks like a player
            try:
                frames = page.frames
                for frame in frames:
                    frame_url = frame.url
                    if frame_url and frame_url != "about:blank":
                        for sel in play_selectors:
                            try:
                                btn = await frame.query_selector(sel)
                                if btn:
                                    await btn.click()
                                    logger.info(
                                        f"Clicked play inside frame ({frame_url[:60]}): {sel}"
                                    )
                                    break
                            except Exception:
                                continue
            except Exception as e:
                logger.debug(f"Frame play-click error: {e}")

            # Wait up to 25 s for CDN URL via network intercept
            try:
                await asyncio.wait_for(found_event.wait(), timeout=25)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for CDN URL via network intercept.")

            # Fallback 1 — raw page source
            if not cdn_url:
                content = await page.content()
                match = cdn_pattern.search(content)
                if match:
                    cdn_url = match.group(0).rstrip("\"'\\")
                    logger.info(f"CDN URL (page source fallback): {cdn_url[:100]}")

            # Fallback 2 — <video>/<source> elements in main page
            if not cdn_url:
                for sel in ["video source[src]", "video[src]", "source[src]"]:
                    el = await page.query_selector(sel)
                    if el:
                        src = await el.get_attribute("src")
                        if src and cdn_pattern.search(src):
                            cdn_url = src
                            logger.info(f"CDN URL (<video> element): {cdn_url[:100]}")
                            break

            # Fallback 3 — <video>/<source> elements inside iframes
            if not cdn_url:
                for frame in page.frames:
                    if cdn_url:
                        break
                    try:
                        for sel in ["video source[src]", "video[src]", "source[src]"]:
                            el = await frame.query_selector(sel)
                            if el:
                                src = await el.get_attribute("src")
                                if src and cdn_pattern.search(src):
                                    cdn_url = src
                                    logger.info(
                                        f"CDN URL (iframe <video> element): {cdn_url[:100]}"
                                    )
                                    break
                    except Exception as e:
                        logger.debug(f"iframe video element check failed: {e}")

        finally:
            await context.close()

        if not cdn_url:
            logger.warning(f"No CDN URL found for {video_url}")
        return cdn_url


# ------------------------------------------------------------------ #
#  Singleton instance                                                  #
# ------------------------------------------------------------------ #
scraper = HanimeScraper()
