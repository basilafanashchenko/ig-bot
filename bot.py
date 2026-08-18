"""
Telegram-бот для завантаження контенту з Instagram, YouTube і TikTok.
Кидаєш посилання в чат — бот качає і одразу присилає все звідти, а лінк прибирає.

Логіка:
- Instagram пости / каруселі / рілси -> instaloader (надійно для фото і відео)
- Instagram сторіз -> instaloader (потрібен залогінений акаунт)
- YouTube / TikTok -> yt-dlp, відео до 4K
"""

import os
import re
import logging
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

import yt_dlp
import imageio_ffmpeg

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ig-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
IG_USERNAME = os.environ.get("IG_USERNAME")  # потрібен лише для сторіз
IG_PASSWORD = os.environ.get("IG_PASSWORD")

LINK_RE = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)


def detect_platform(url: str) -> str:
    if "instagram.com" in url:
        return "instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    return "unknown"


def is_story_link(url: str) -> bool:
    return "/stories/" in url


async def download_post_with_instaloader(url: str, out_dir: Path) -> list[Path]:
    """Качає Instagram пост / рілс / каруселю через instaloader."""
    import instaloader

    match = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_\-]+)", url)
    if not match:
        raise RuntimeError("Не вдалося розпізнати посилання на пост чи рілс.")
    shortcode = match.group(1)

    L = instaloader.Instaloader(
        dirname_pattern=str(out_dir),
        filename_pattern="{shortcode}_{typename}",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )

    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=str(out_dir))

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


async def download_video_with_ytdlp(url: str, out_dir: Path) -> list[Path]:
    """Качає YouTube / TikTok відео, до 4K, з автоматичним об'єднанням відео+звук."""
    ydl_opts = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return sorted(out_dir.glob("*"))


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = LINK_RE.search(text)
    if not match:
        return

    url = match.group(0)
    platform = detect_platform(url)
    if platform == "unknown":
        return  # ігноруємо посилання не з підтримуваних платформ

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text("качаю, секунду...")

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        try:
            if platform == "instagram":
                if is_story_link(url):
                    files = await download_story_with_instaloader(url, out_dir)
                else:
                    files = await download_post_with_instaloader(url, out_dir)
            else:  # youtube або tiktok
                files = await download_video_with_ytdlp(url, out_dir)

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

            # прибираємо повідомлення з лінком, лишаючи тільки готовий контент
            # (працює в групах лише якщо бот доданий як адміністратор)
            try:
                await update.message.delete()
            except Exception:
                pass  # немає прав адміна — просто лишаємо повідомлення як є

        except Exception as e:
            log.exception("Помилка завантаження")
            await status_msg.edit_text(f"не вийшло завантажити: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "кидай посилання на пост, рілс, каруселю чи сторіз з інстаграма, "
        "або відео з youtube чи tiktok — і я одразу пришлю все звідти"
    )


class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # не смітимо логи запитами перевірки живості


def _run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    server.serve_forever()


def main() -> None:
    # Render (Web Service) вимагає, щоб додаток слухав якийсь порт —
    # це не впливає на роботу бота, просто "заглушка" для перевірки живості
    threading.Thread(target=_run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Бот запущений, чекаю посилань...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
