import asyncio
import logging
import os
import time

from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import FloodWait, MessageNotModified

import config
from scraper import scraper
from downloader import downloader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# In-memory state per user
user_state: dict = {}

# Global event loop — set in main(), used by yt-dlp thread
_loop: asyncio.AbstractEventLoop = None


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #
def format_bytes(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


async def safe_edit(msg: Message, text: str, **kwargs):
    try:
        await msg.edit_text(text, **kwargs)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await msg.edit_text(text, **kwargs)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"safe_edit ignored: {e}")


# ------------------------------------------------------------------ #
#  /start  /help                                                       #
# ------------------------------------------------------------------ #
@Client.on_message(filters.command(["start", "help"]) & filters.incoming)
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "🎌 **Hanime Downloader Bot**\n\n"
        "**Commands:**\n"
        "• `/search <n>` — Search hanime.tv and browse episodes\n"
        "• `/dl <url>` — Download directly from a hanime.tv episode URL\n"
        "• `/help` — Show this message\n\n"
        "_After `/search`, pick a title → pick an episode → bot downloads & sends it._"
    )


# ------------------------------------------------------------------ #
#  /search                                                             #
# ------------------------------------------------------------------ #
@Client.on_message(filters.command("search") & filters.incoming)
async def cmd_search(client: Client, message: Message):
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text(
            "❓ **Usage:** `/search <anime name>`\n"
            "Example: `/search isekai harem`"
        )
        return

    uid = message.from_user.id
    status_msg = await message.reply_text(f"🔍 Searching for **{query}**…")

    try:
        results = await scraper.search(query)
    except Exception as e:
        logger.error(f"Search error: {e}")
        await safe_edit(status_msg, f"❌ Search failed:\n`{e}`")
        return

    if not results:
        await safe_edit(status_msg, "😔 No results found. Try a different keyword.")
        return

    user_state[uid] = {
        "search_results": results,
        "selected_url": None,
        "episodes": [],
        "query": query,
    }

    buttons = [
        [InlineKeyboardButton(f"📺 {r['title'][:50]}", callback_data=f"series:{uid}:{i}")]
        for i, r in enumerate(results)
    ]

    await safe_edit(
        status_msg,
        f"🔎 **{len(results)} results** for `{query}`\n_Tap a title to see its episodes:_",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------ #
#  /dl — direct URL download                                          #
# ------------------------------------------------------------------ #
@Client.on_message(filters.command("dl") & filters.incoming)
async def cmd_dl(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        await message.reply_text(
            "❓ **Usage:** `/dl <hanime.tv episode URL>`\n"
            "Example: `/dl https://hanime.tv/videos/hentai/my-anime-1`"
        )
        return

    url = args[0].strip()
    if "hanime.tv" not in url:
        await message.reply_text("❌ Please provide a valid **hanime.tv** URL.")
        return

    title = url.rstrip("/").split("/")[-1].replace("-", " ").title()
    await _download_and_send(client, message, url, title)


# ------------------------------------------------------------------ #
#  Callback: series selected → show episode list                      #
# ------------------------------------------------------------------ #
@Client.on_callback_query(filters.regex(r"^series:\d+:\d+$"))
async def cb_series(client: Client, cb: CallbackQuery):
    parts = cb.data.split(":")
    uid, idx = int(parts[1]), int(parts[2])

    if cb.from_user.id != uid:
        await cb.answer("❌ This is not your search session!", show_alert=True)
        return

    state = user_state.get(uid)
    if not state:
        await cb.answer("⌛ Session expired. Run /search again.", show_alert=True)
        return

    selected = state["search_results"][idx]
    await cb.answer()
    await cb.message.edit_text(f"⏳ Loading episodes for **{selected['title']}**…")

    try:
        episodes = await scraper.get_series_episodes(selected["url"])
    except Exception as e:
        logger.error(f"Episode fetch error: {e}")
        await cb.message.edit_text(f"❌ Failed to load episodes:\n`{e}`")
        return

    if not episodes:
        episodes = [{"title": selected["title"], "url": selected["url"], "number": 1}]

    state["episodes"] = episodes
    state["selected_url"] = selected["url"]
    state["selected_title"] = selected["title"]

    buttons = [
        [InlineKeyboardButton(
            f"🎬 {ep['title'][:48] if ep.get('title') else f'Episode {ep.get('number', i + 1)}'}",
            callback_data=f"episode:{uid}:{i}"
        )]
        for i, ep in enumerate(episodes)
    ]
    buttons.append([InlineKeyboardButton("🔙 Back to results", callback_data=f"back:{uid}")])

    await cb.message.edit_text(
        f"📋 **{selected['title']}**\n{len(episodes)} episode(s) — tap to download:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------ #
#  Callback: back to search results                                    #
# ------------------------------------------------------------------ #
@Client.on_callback_query(filters.regex(r"^back:\d+$"))
async def cb_back(client: Client, cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])

    if cb.from_user.id != uid:
        await cb.answer("❌ Not your session!", show_alert=True)
        return

    state = user_state.get(uid)
    if not state:
        await cb.answer("⌛ Session expired. Run /search again.", show_alert=True)
        return

    await cb.answer()
    results = state["search_results"]
    buttons = [
        [InlineKeyboardButton(f"📺 {r['title'][:50]}", callback_data=f"series:{uid}:{i}")]
        for i, r in enumerate(results)
    ]
    await cb.message.edit_text(
        f"🔎 Results for `{state.get('query', '...')}` — pick a title:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------ #
#  Callback: episode selected → download                              #
# ------------------------------------------------------------------ #
@Client.on_callback_query(filters.regex(r"^episode:\d+:\d+$"))
async def cb_episode(client: Client, cb: CallbackQuery):
    parts = cb.data.split(":")
    uid, idx = int(parts[1]), int(parts[2])

    if cb.from_user.id != uid:
        await cb.answer("❌ Not your session!", show_alert=True)
        return

    state = user_state.get(uid)
    if not state or not state.get("episodes"):
        await cb.answer("⌛ Session expired. Run /search again.", show_alert=True)
        return

    episode = state["episodes"][idx]
    await cb.answer("⬇️ Starting download…")
    await _download_and_send(client, cb.message, episode["url"], episode["title"])


# ------------------------------------------------------------------ #
#  Core: CDN extract → yt-dlp download → Telegram upload              #
# ------------------------------------------------------------------ #
async def _download_and_send(
    client: Client,
    trigger_msg: Message,
    page_url: str,
    title: str,
):
    # ── Step 1: Extract CDN URL via Playwright ──────────────────────
    status = await trigger_msg.reply_text(
        f"🕵️ **Extracting CDN URL…**\n"
        f"🎬 {title}\n"
        f"_This can take up to 30 seconds_"
    )

    cdn_url = await scraper.get_cdn_url(page_url)

    if cdn_url:
        cdn_display = cdn_url[:70] + "…" if len(cdn_url) > 70 else cdn_url
        await safe_edit(
            status,
            f"✅ CDN URL found!\n"
            f"⬇️ **Downloading:** {title}\n"
            f"`{cdn_display}`"
        )
    else:
        logger.warning("CDN not intercepted — falling back to page URL for yt-dlp")
        cdn_url = page_url
        await safe_edit(
            status,
            f"⚠️ CDN not intercepted, trying yt-dlp directly…\n"
            f"⬇️ **Downloading:** {title}"
        )

    # ── Step 2: Download via yt-dlp ─────────────────────────────────
    last_update = [0.0]

    def progress_hook(d):
        if d["status"] != "downloading":
            return
        now = time.time()
        if now - last_update[0] < 4:
            return
        last_update[0] = now

        downloaded = d.get("downloaded_bytes", 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        speed = d.get("speed") or 0
        eta = d.get("eta") or 0

        pct = (downloaded / total * 100) if total else 0
        filled = int(20 * pct / 100)
        bar = "█" * filled + "░" * (20 - filled)

        text = (
            f"⬇️ **Downloading:** {title}\n"
            f"`[{bar}]` {pct:.1f}%\n"
            f"📦 {format_bytes(downloaded)} / {format_bytes(total)}\n"
            f"⚡ {format_bytes(int(speed))}/s  ⏱ ETA {eta}s"
        )
        if _loop and not _loop.is_closed():
            asyncio.run_coroutine_threadsafe(safe_edit(status, text), _loop)

    file_path = await downloader.download(cdn_url, title, progress_hook)

    if not file_path:
        await safe_edit(
            status,
            f"❌ **Download failed** for _{title}_\n"
            "The CDN may be rate-limiting or the URL expired.\n"
            "Try again or use `/dl <url>` directly."
        )
        return

    # ── Step 3: Size check ──────────────────────────────────────────
    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)

    if size_mb > config.MAX_FILE_SIZE_MB:
        await safe_edit(
            status,
            f"⚠️ **File too large:** {size_mb:.1f} MB\n"
            f"Telegram bot limit is {config.MAX_FILE_SIZE_MB} MB.\n"
            "File deleted from server."
        )
        os.remove(file_path)
        return

    await safe_edit(status, f"📤 **Uploading:** {title} ({size_mb:.1f} MB)…")

    # ── Step 4: Upload to Telegram ──────────────────────────────────
    last_upload = [0.0]

    async def upload_progress(current, total):
        now = time.time()
        if now - last_upload[0] < 4:
            return
        last_upload[0] = now
        pct = (current / total * 100) if total else 0
        filled = int(20 * pct / 100)
        bar = "█" * filled + "░" * (20 - filled)
        await safe_edit(
            status,
            f"📤 **Uploading:** {title}\n"
            f"`[{bar}]` {pct:.1f}%\n"
            f"📦 {format_bytes(current)} / {format_bytes(total)}"
        )

    try:
        await client.send_video(
            chat_id=trigger_msg.chat.id,
            video=file_path,
            caption=f"🎌 **{title}**\n🔗 {page_url}",
            supports_streaming=True,
            progress=upload_progress,
        )
        await status.delete()
        logger.info(f"✅ Delivered: {title}")
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await safe_edit(status, f"❌ Upload failed:\n`{e}`")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #
async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    logger.info("Starting Playwright browser…")
    await scraper.start()

    bot = Client(
        "hanime_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        sleep_threshold=60,
    )

    await bot.start()
    me = await bot.get_me()
    logger.info(f"Bot online as @{me.username} (id={me.id})")
    logger.info("Listening for commands. Press Ctrl+C to stop.")

    await idle()

    logger.info("Shutting down…")
    await bot.stop()
    await scraper.stop()


if __name__ == "__main__":
    asyncio.run(main())
