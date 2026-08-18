"""
Telegram-бот для завантаження контенту з Instagram (пости, каруселі, рілси, сторіз).
Кидаєш посилання в чат — бот качає і одразу присилає всі фото/відео назад.

Логіка:
- пости / каруселі / рілси -> yt-dlp (без логіну, найстабільніше)
- сторіз -> instaloader (потрібен залогінений акаунт, бо сторіз без логіну не віддають)
"""

import os
import re
import shutil
import logging
import tempfile
from pathlib import Path

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ig-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
IG_USERNAME = os.environ.get("IG_USERNAME")  # потрібен лише для сторіз
IG_PASSWORD = os.environ.get("IG_PASSWORD")

IG_LINK_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?P<kind>p|reel|reels|tv|stories)/[A-Za-z0-9_\-/.]+",
    re.IGNORECASE,
)


def is_story_link(url: str) -> bool:
    return "/stories/" in url


async def download_with_ytdlp(url: str, out_dir: Path) -> list[Path]:
    """Качає пост / рілс / каруселю. yt-dlp сам розкладає карусель на кілька файлів."""
    ydl_opts = {
        "outtmpl": str(out_dir / "%(id)s_%(autonumber)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,  # карусель = плейлист із кількох елементів
        "format": "bestvideo+bestaudio/best/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return sorted(out_dir.glob("*"))


async def download_story_with_instaloader(url: str, out_dir: Path) -> list[Path]:
    """Качає сторіз. Потребує IG_USERNAME/IG_PASSWORD в env (окремий, не основний акаунт)."""
    import instaloader

    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError(
            "Для завантаження сторіз потрібен логін. Додай IG_USERNAME і IG_PASSWORD "
            "у змінні середовища (бажано окремий акаунт, не особистий)."
        )

    match = re.search(r"/stories/([^/]+)/", url)
    if not match:
        raise RuntimeError("Не вдалося визначити username із посилання на сторіз.")
    username = match.group(1)

    L = instaloader.Instaloader(
        dirname_pattern=str(out_dir),
        filename_pattern="{shortcode}",
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
    )

    session_file = out_dir.parent / f"session-{IG_USERNAME}"
    try:
        L.load_session_from_file(IG_USERNAME, str(session_file))
    except FileNotFoundError:
        L.login(IG_USERNAME, IG_PASSWORD)
        L.save_session_to_file(str(session_file))

    profile = instaloader.Profile.from_username(L.context, username)
    for story in L.get_stories(userids=[profile.userid]):
        for item in story.get_items():
            L.download_storyitem(item, target=str(out_dir))

    return sorted(out_dir.glob("*"))


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = IG_LINK_RE.search(text)
    if not match:
        return

    url = match.group(0)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text("качаю, секунду...")

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        try:
            if is_story_link(url):
                files = await download_story_with_instaloader(url, out_dir)
            else:
                files = await download_with_ytdlp(url, out_dir)

            media_files = [f for f in files if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".webp", ".mov")]

            if not media_files:
                await status_msg.edit_text("нічого не знайшов за цим посиланням — можливо контент приватний або видалений")
                return

            # телеграм дозволяє максимум 10 елементів в одній media group
            for i in range(0, len(media_files), 10):
                chunk = media_files[i : i + 10]
                if len(chunk) == 1:
                    f = chunk[0]
                    if f.suffix.lower() in (".mp4", ".mov"):
                        await update.message.reply_video(video=f.open("rb"))
                    else:
                        await update.message.reply_photo(photo=f.open("rb"))
                else:
                    media_group = []
                    for f in chunk:
                        if f.suffix.lower() in (".mp4", ".mov"):
                            media_group.append(InputMediaVideo(media=f.open("rb")))
                        else:
                            media_group.append(InputMediaPhoto(media=f.open("rb")))
                    await update.message.reply_media_group(media=media_group)

            await status_msg.delete()

        except Exception as e:
            log.exception("Помилка завантаження")
            await status_msg.edit_text(f"не вийшло завантажити: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "кидай посилання на пост, рілс, каруселю або сторіз з інстаграма — і я одразу пришлю все звідти"
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Бот запущений, чекаю посилань...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
