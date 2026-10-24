"""Публикация готовых клипов: YouTube Shorts, Telegram-канал, VK Клипы/видео.

Ключи берутся из переменных окружения (см. README.md).
"""
import html
import json
import os
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent


# Ссылка в SOCIAL_LINKS считается «своей» для площадки, если содержит этот адрес:
# в посте не нужна ссылка на ту же соцсеть, где его читают.
OWN_HOST = {"telegram": "t.me/", "youtube": "youtube.com", "vk": "vk.com"}


def social_links(platform=None):
    """Строки со ссылками на соцсети для подписи под каждым клипом.

    SOCIAL_LINKS в .env: пары «Название: ссылка» через « | », например
    SOCIAL_LINKS=Telegram: https://t.me/titanhockeylab | Rutube: https://rutube.ru/channel/23890442/
    Если не задано — только ссылка на TELEGRAM_CHANNEL.
    """
    raw = os.environ.get("SOCIAL_LINKS", "")
    links = [x.strip() for x in raw.split("|") if x.strip()]
    if not links and os.environ.get("TELEGRAM_CHANNEL", "").startswith("@"):
        links = [f"Telegram: https://t.me/{os.environ['TELEGRAM_CHANNEL'][1:]}"]
    own = OWN_HOST.get(platform)
    return [x for x in links if not (own and own in x)]


def caption(clip, platform=None, with_shorts_tag=False):
    """Описание + призыв и ссылки на соцсети + хэштеги."""
    tags = list(clip.get("hashtags") or [])
    if with_shorts_tag and "shorts" not in [t.lower() for t in tags]:
        tags.append("shorts")
    tag_line = " ".join("#" + t.lstrip("#").replace(" ", "") for t in tags)
    links = social_links(platform)
    links_block = ""
    if links:
        cta = os.environ.get("SOCIAL_CTA", "Подписывайтесь, там ещё больше полезного:")
        links_block = "\n".join([cta, *links])
    return "\n\n".join(p for p in (clip.get("description", ""), links_block, tag_line) if p)


# ---------- YouTube ----------

def youtube_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    token = HERE / "youtube_token.json"
    secret = Path(os.environ.get("YOUTUBE_CLIENT_SECRET", HERE / "client_secret.json"))
    creds = Credentials.from_authorized_user_file(str(token), scopes) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(str(secret), scopes).run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload_youtube(clip, privacy):
    from googleapiclient.http import MediaFileUpload

    yt = youtube_service()
    body = {
        "snippet": {
            # YouTube не принимает угловые скобки в заголовке и описании
            "title": re.sub(r"[<>]", "", clip["title"])[:100],
            "description": re.sub(r"[<>]", "", caption(clip, "youtube", with_shorts_tag=True))[:5000],
            "tags": clip.get("hashtags", []),
            "categoryId": "22",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(clip["file"], chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtube.com/shorts/{resp['id']}"


# ---------- Telegram ----------

def upload_telegram(clip, _privacy):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]  # @channel_name или числовой id
    text = f"<b>{html.escape(clip['title'])}</b>\n\n{html.escape(caption(clip, 'telegram'))}"
    with open(clip["file"], "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendVideo",
                          data={"chat_id": chat, "caption": text[:1024], "parse_mode": "HTML",
                                "supports_streaming": "true", "width": 1080, "height": 1920},
                          files={"video": f}, timeout=600)
    r.raise_for_status()
    msg = r.json()["result"]
    return f"telegram message {msg['message_id']}"


# ---------- VK ----------

def upload_vk(clip, _privacy):
    token = os.environ["VK_TOKEN"]
    group = os.environ.get("VK_GROUP_ID")  # без минуса; пусто — на свою страницу
    params = {"access_token": token, "v": "5.199", "name": clip["title"][:128],
              "description": caption(clip, "vk"), "wallpost": 1}
    if group:
        params["group_id"] = group
    save = requests.post("https://api.vk.com/method/video.save", data=params, timeout=60).json()
    if "error" in save:
        raise RuntimeError(save["error"].get("error_msg"))
    with open(clip["file"], "rb") as f:
        up = requests.post(save["response"]["upload_url"], files={"video_file": f}, timeout=600).json()
    if "error" in up:
        raise RuntimeError(up["error"])
    owner = save["response"]["owner_id"]
    return f"https://vk.com/video{owner}_{save['response']['video_id']}"


UPLOADERS = {"youtube": upload_youtube, "telegram": upload_telegram, "vk": upload_vk}


def publish_all(clips, targets, privacy="private"):
    """Выкладывает клипы; возвращает {(номер клипа, площадка): ссылка или None при ошибке}."""
    results = {}
    for t in targets:
        if t not in UPLOADERS:
            print(f"Неизвестная площадка: {t} (доступны: {', '.join(UPLOADERS)})")
    for i, clip in enumerate(clips):
        for t in targets:
            if t not in UPLOADERS:
                continue
            try:
                link = UPLOADERS[t](clip, privacy)
                print(f"  {t}: {clip['title']} → {link}")
            except Exception as e:
                link = None
                print(f"  {t}: ОШИБКА для «{clip['title']}»: {e}")
            results[(i, t)] = link
    return results


if __name__ == "__main__":
    # Выложить уже нарезанное: python publish.py output/<id>/clips.json youtube,telegram [public]
    from envfile import load_env
    load_env()
    clips = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    publish_all(clips, sys.argv[2].split(","), sys.argv[3] if len(sys.argv) > 3 else "private")
