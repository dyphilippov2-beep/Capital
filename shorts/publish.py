"""Публикация готовых клипов: YouTube Shorts, Telegram-канал, VK Клипы/видео.

Ключи берутся из переменных окружения (см. README.md).
"""
import json
import os
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent


def caption(clip, with_shorts_tag=False):
    tags = list(clip.get("hashtags") or [])
    if with_shorts_tag and "shorts" not in [t.lower() for t in tags]:
        tags.append("shorts")
    tag_line = " ".join("#" + t.lstrip("#").replace(" ", "") for t in tags)
    return f"{clip.get('description', '')}\n\n{tag_line}".strip()


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
            "title": clip["title"][:100],
            "description": caption(clip, with_shorts_tag=True),
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
    text = f"<b>{clip['title']}</b>\n\n{caption(clip)}"
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
              "description": caption(clip), "wallpost": 1}
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
    for t in targets:
        if t not in UPLOADERS:
            print(f"Неизвестная площадка: {t} (доступны: {', '.join(UPLOADERS)})")
    for clip in clips:
        for t in targets:
            if t not in UPLOADERS:
                continue
            try:
                link = UPLOADERS[t](clip, privacy)
                print(f"  {t}: {clip['title']} → {link}")
            except Exception as e:
                print(f"  {t}: ОШИБКА для «{clip['title']}»: {e}")


if __name__ == "__main__":
    # Выложить уже нарезанное: python publish.py output/<id>/clips.json youtube,telegram [public]
    clips = json.loads(Path(sys.argv[1]).read_text())
    publish_all(clips, sys.argv[2].split(","), sys.argv[3] if len(sys.argv) > 3 else "private")
