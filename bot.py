import asyncio
import logging
import os
import time
from typing import Optional

from pyrogram import Client, filters
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

# ------------------------------------------------------------------ #
#  Pyrogram client                                                     #
# ------------------------------------------------------------------ #
app = Client(
    "hanime_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)

# In-memory state: {user_id: {search_results: [...], selected_url: str, episodes: [...]}}
user_state: dict = {}


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #
def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def format_bytes(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


async def safe_edit(msg: Message, text: str, **kwargs):
    try:
        await msg.edit_text(text, **kwargs)
    except (MessageNotModified, FloodWait):
        pass


# ------------------------------------------------------------------ #
#  /start  /help                                                       #
# ------------------------------------------------------------------ #
@app.on_message(filters.command(["start", "help"]))
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "🎌 **Hanime Downloader Bot**\n\n"
        "**Commands:**\n"
        "• `/search <name>` — Search for anime on hanime.tv\n"
        "• `/dl <url>` — Direct download from a hanime.tv episode URL\n"
        "• `/help` — Show this message\n\n"
        "_After searching, pick an episode from the inline buttons and the bot "
        "will scrape the CDN URL and download it for you._"
    )


# ------------------------------------------------------------------ #
#  /search                                                             #
# ------------------------------------------------------------------ #
@app.on_message(filters.command("search"))
async def cmd_search(client: Client, message: Message):
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Usage: `/search <anime name>`")
        return

    uid = message.from_user.id
    status_msg = await message.reply_text(f"🔍 Searching for **{query}**…")

    try:
        results = await scraper.search(query)
    except Exception as e:
        logger.error(e)
        await safe_edit(status_msg, f"❌ Search failed: {e}")
        return

    if not results:
        await safe_edit(status_msg, "😔 No results found. Try a different query.")
        return

    user_state[uid] = {"search_results": results, "selected_url": None, "episodes": []}

    buttons = []
    for i, r in enumerate(results):
        title = r["title"][:50]
        buttons.append([InlineKeyboardButton(f"📺 {title}", callback_data=f"series:{uid}:{i}")])

    await safe_edit(
        status_msg,
        f"🔎 Found **{len(results)}** results for `{query}`:\n_Select a title to view episodes._",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------ #
#  Callback: series selected                                           #
# ------------------------------------------------------------------ #
@app.on_callback_query(filters.regex(r"^series:"))
async def cb_series(client: Client, cb: CallbackQuery):
    _, uid_str, idx_str = cb.data.split(":")
    uid = int(uid_str)
    idx = int(idx_str)

    if cb.from_user.id != uid:
        await cb.answer("This is not your search!", show_alert=True)
        return

    state = user_state.get(uid)
    if not state:
        await cb.answer("Session expired. Run /search again.", show_alert=True)
        return

    selected = state["search_results"][idx]
    await cb.answer()
    await cb.message.edit_text(
        f"⏳ Loading episodes for **{selected['title']}**…"
    )

    try:
        episodes = await scraper.get_series_episodes(selected["url"])
    except Exception as e:
        logger.error(e)
        await cb.message.edit_text(f"❌ Failed to load episodes: {e}")
        return

    if not episodes:
        # Treat the search result itself as the only episode
        episodes = [{"title": selected["title"], "url": selected["url"], "number": 1}]

    state["episodes"] = episodes
    state["selected_url"] = selected["url"]

    buttons = []
    for i, ep in enumerate(episodes):
        label = ep["title"][:50] or f"Episode {ep['number'] or i+1}"
        buttons.append(
            [InlineKeyboardButton(f"🎬 {label}", callback_data=f"episode:{uid}:{i}")]
        )
    buttons.append(
        [InlineKeyboardButton("🔙 Back to results", callback_data=f"back:{uid}")]
    )

    await cb.message.edit_text(
        f"📋 **{selected['title']}** — {len(episodes)} episode(s):\n_Tap to download._",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------ #
#  Callback: back to search results                                    #
# ------------------------------------------------------------------ #
@app.on_callback_query(filters.regex(r"^back:"))
async def cb_back(client: Client, cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    if cb.from_user.id != uid:
        await cb.answer("Not your session!", show_alert=True)
        return

    state = user_state.get(uid)
    if not state:
        await cb.answer("Session expired. Run /search again.", show_alert=True)
        return

    results = state["search_results"]
    buttons = []
    for i, r in enumerate(results):
        title = r["title"][:50]
        buttons.append([InlineKeyboardButton(f"📺 {title}", callback_data=f"series:{uid}:{i}")])

    await cb.answer()
    await cb.message.edit_text(
        "🔎 Search results — select a title:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------ #
#  Callback: episode selected → download                              #
# ------------------------------------------------------------------ #
@app.on_callback_query(filters.regex(r"^episode:"))
async def cb_episode(client: Client, cb: CallbackQuery):
    _, uid_str, idx_str = cb.data.split(":")
    uid = int(uid_str)
    idx = int(idx_str)

    if cb.from_user.id != uid:
        await cb.answer("Not your session!", show_alert=True)
        return

    state = user_state.get(uid)
    if not state or not state.get("episodes"):
        await cb.answer("Session expired. Run /search again.", show_alert=True)
        return

    episode = state["episodes"][idx]
    await cb.answer()

    await _download_and_send(client, cb.message, uid, episode["url"], episode["title"])


# ------------------------------------------------------------------ #
#  /dl — direct URL download                                          #
# ------------------------------------------------------------------ #
@app.on_message(filters.command("dl"))
async def cmd_dl(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        await message.reply_text("Usage: `/dl <hanime.tv episode URL>`")
        return

    url = args[0].strip()
    if "hanime.tv" not in url:
        await message.reply_text("❌ Please provide a valid hanime.tv URL.")
        return

    title = url.split("/")[-1].replace("-", " ").title()
    uid = message.from_user.id
    await _download_and_send(client, message, uid, url, title)


# ------------------------------------------------------------------ #
#  Core: extract CDN URL → download → upload                          #
# ------------------------------------------------------------------ #
async def _download_and_send(
    client: Client,
    trigger_msg: Message,
    uid: int,
    page_url: str,
    title: str,
):
    # Step 1: get CDN url
    status = await trigger_msg.reply_text(
        f"🕵️ Extracting CDN URL for **{title}**…\n_This may take ~30s_"
    )

    cdn_url = await scraper.get_cdn_url(page_url)

    if not cdn_url:
        # Let yt-dlp try to handle the page URL directly
        logger.warning("CDN URL not found via Playwright, passing page URL to yt-dlp")
        cdn_url = page_url

    await safe_edit(status, f"⬇️ Downloading **{title}**…\n`{cdn_url[:80]}…`")

    # Progress tracking
    last_update = [time.time()]

    def progress_hook(d):
        nonlocal last_update
        if d["status"] == "downloading":
            now = time.time()
            if now - last_update[0] < 5:
                return
            last_update[0] = now

            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            speed = d.get("speed", 0) or 0
            eta = d.get("eta", 0) or 0

            pct = (downloaded / total * 100) if total else 0
            bar_len = 20
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)

            text = (
                f"⬇️ **Downloading:** {title}\n"
                f"`[{bar}]` {pct:.1f}%\n"
                f"📦 {format_bytes(downloaded)} / {format_bytes(total)}\n"
                f"⚡ {format_bytes(int(speed))}/s  ⏱ {eta}s"
            )
            asyncio.run_coroutine_threadsafe(safe_edit(status, text), client.loop)

    # Step 2: Download
    file_path = await downloader.download(cdn_url, title, progress_hook)

    if not file_path:
        await safe_edit(
            status,
            f"❌ Download failed for **{title}**.\n"
            "Try `/dl <url>` with the direct episode URL, or the CDN might be rate-limited.",
        )
        return

    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)

    if size_mb > config.MAX_FILE_SIZE_MB:
        await safe_edit(
            status,
            f"⚠️ File is too large ({size_mb:.1f} MB) to send via Telegram.\n"
            f"Max allowed: {config.MAX_FILE_SIZE_MB} MB.",
        )
        os.remove(file_path)
        return

    await safe_edit(status, f"📤 Uploading **{title}** ({size_mb:.1f} MB)…")

    # Step 3: Upload
    try:
        await client.send_video(
            chat_id=trigger_msg.chat.id,
            video=file_path,
            caption=f"🎌 **{title}**\n🔗 {page_url}",
            supports_streaming=True,
            progress=_upload_progress,
            progress_args=(status, title, client),
        )
        await status.delete()
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await safe_edit(status, f"❌ Upload failed: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


async def _upload_progress(current, total, status_msg, title, client):
    pct = current / total * 100 if total else 0
    bar_len = 20
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    text = (
        f"📤 **Uploading:** {title}\n"
        f"`[{bar}]` {pct:.1f}%\n"
        f"📦 {format_bytes(current)} / {format_bytes(total)}"
    )
    await safe_edit(status_msg, text)


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #
async def main():
    await scraper.start()
    logger.info("Bot starting…")
    await app.start()
    logger.info("Bot is running. Press Ctrl+C to stop.")
    await asyncio.Event().wait()  # keep alive


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        asyncio.run(scraper.stop())
