import asyncio
import os
import logging
from typing import Callable, Optional
import yt_dlp

import config

logger = logging.getLogger(__name__)

os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)


class Downloader:
    def __init__(self):
        pass

    def _make_ydl_opts(
        self,
        output_path: str,
        progress_hook: Optional[Callable] = None,
        cdn_url: Optional[str] = None,
    ) -> dict:
        opts = {
            "outtmpl": output_path,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://hanime.tv/",
                "Origin": "https://hanime.tv",
            },
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "concurrent_fragment_downloads": 5,
            "retries": 10,
            "fragment_retries": 10,
        }

        if progress_hook:
            opts["progress_hooks"] = [progress_hook]

        if config.PROXY_URL:
            opts["proxy"] = config.PROXY_URL

        return opts

    async def download(
        self,
        url: str,
        filename: str,
        progress_hook: Optional[Callable] = None,
    ) -> Optional[str]:
        """
        Download video from URL (page URL or direct CDN URL).
        Returns path to downloaded file or None on failure.
        """
        safe_name = "".join(
            c if c.isalnum() or c in " ._-" else "_" for c in filename
        ).strip()
        output_path = os.path.join(
            config.DOWNLOAD_DIR, f"{safe_name}.%(ext)s"
        )
        final_path_holder = []

        def _hook(d):
            if d["status"] == "finished":
                final_path_holder.append(d.get("filename") or d.get("info_dict", {}).get("_filename"))
            if progress_hook:
                progress_hook(d)

        opts = self._make_ydl_opts(output_path, _hook)

        loop = asyncio.get_event_loop()

        def _run():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                return True
            except Exception as e:
                logger.error(f"yt-dlp error: {e}")
                return False

        success = await loop.run_in_executor(None, _run)

        if not success:
            return None

        # Resolve final file path
        if final_path_holder:
            fp = final_path_holder[-1]
            if fp and os.path.exists(fp):
                return fp
            # Sometimes yt-dlp changes extension after merge
            for ext in ["mp4", "mkv", "webm"]:
                candidate = os.path.join(config.DOWNLOAD_DIR, f"{safe_name}.{ext}")
                if os.path.exists(candidate):
                    return candidate

        # Glob fallback
        for ext in ["mp4", "mkv", "webm"]:
            candidate = os.path.join(config.DOWNLOAD_DIR, f"{safe_name}.{ext}")
            if os.path.exists(candidate):
                return candidate

        logger.error("Downloaded file not found on disk.")
        return None


downloader = Downloader()
