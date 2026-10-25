"""Публикация готовых клипов: YouTube Shorts, Telegram-канал, VK Клипы/видео.

Ключи берутся из переменных окружения (см. README.md).
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent


# Строка подписи считается «своей» для площадки, если содержит этот адрес:
# в посте не нужна ссылка на ту же соцсеть, где его читают.
OWN_HOST = {"telegram": ("t.me/",), "youtube": ("youtube.com",), "vk": ("vk.ru/", "vk.com/"), "rutube": ("rutube.ru/",)}
FOOTER = HERE / "footer.txt"
BASE_TAGS = ["shorts", "хоккей"]


def footer(platform=None):
    """Общий блок под каждым шортсом (запись в секцию и соцсети) из footer.txt."""
    if not FOOTER.exists():
        return ""
    own = OWN_HOST.get(platform, ())
    lines = FOOTER.read_text(encoding="utf-8").strip().splitlines()
    return "\n".join(x for x in lines if not any(h in x for h in own))


def full_video_link(clip):
    """Строка со ссылкой на полное видео, из которого нарезан шортс."""
    if clip.get("video_id"):
        return f"▶️ Смотреть полное видео: https://youtu.be/{clip['video_id']}"
    return ""


def caption(clip, platform=None):
    """Описание клипа + ссылка на полное видео + общий блок из footer.txt + хэштеги."""
    tags = []
    for t in BASE_TAGS + list(clip.get("hashtags") or []):
        t = "#" + t.lstrip("#").replace(" ", "")
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    parts = (clip.get("description", ""), full_video_link(clip), footer(platform), " ".join(tags))
    return "\n\n".join(p for p in parts if p)


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
            "description": re.sub(r"[<>]", "", caption(clip, "youtube"))[:5000],
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
    # В Telegram — только название ролика и ссылка на полное видео, без описания и хэштегов
    text = f"<b>{html.escape(clip['title'])}</b>"
    if full_video_link(clip):
        text += "\n\n" + html.escape(full_video_link(clip))
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


# ---------- Rutube ----------
# Rutube принимает видео только по ссылке на файл, поэтому в GitHub Actions клип
# сначала кладётся в релиз репозитория (RUTUBE_RELEASE), а Rutube скачивает его оттуда.

def public_file_url(path):
    repo, tag = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("RUTUBE_RELEASE", "shorts-files")
    if not repo or not shutil.which("gh"):
        raise RuntimeError("Rutube работает только в GitHub Actions: нужна публичная ссылка на файл")
    name = re.sub(r"[^\w.-]+", "_", Path(path).name.encode("ascii", "ignore").decode()) or "clip.mp4"
    name = f"{int(time.time())}_{name}"
    staged = Path(path).with_name(name)
    shutil.copy(path, staged)
    if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode != 0:
        subprocess.run(["gh", "release", "create", tag, "--title", "Shorts files",
                        "--notes", "Файлы шортсов для загрузки в Rutube"], check=True)
    subprocess.run(["gh", "release", "upload", tag, str(staged), "--clobber"], check=True)
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def upload_rutube(clip, privacy):
    api = "https://rutube.ru/api"
    auth = requests.post(f"{api}/accounts/token_auth/", timeout=60,
                         data={"username": os.environ["RUTUBE_LOGIN"], "password": os.environ["RUTUBE_PASSWORD"]})
    auth.raise_for_status()
    headers = {"Authorization": f"Token {auth.json()['token']}"}
    data = {"url": public_file_url(clip["file"]), "title": clip["title"][:100],
            "description": caption(clip, "rutube")[:5000], "is_hidden": privacy != "public", "is_short": True}
    r = requests.post(f"{api}/video/", headers=headers, data=data, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Rutube {r.status_code}: {r.text[:300]}")
    return f"rutube video {r.json().get('video_id')}"


UPLOADERS = {"youtube": upload_youtube, "telegram": upload_telegram, "vk": upload_vk, "rutube": upload_rutube}


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
