#!/usr/bin/env python3
"""Автопилот: самые популярные видео канала → шортсы → публикация по расписанию.

    python auto.py prepare            # нарезать самые популярные ещё не обработанные видео в очередь
    python auto.py post               # выложить следующий клип из очереди
    python auto.py status             # что в очереди и что уже вышло

Канал и площадки берутся из .env (YOUTUBE_CHANNEL, PUBLISH_TO), их можно переопределить флагами.
Запуск по расписанию настраивает schedule.bat (Планировщик заданий Windows).
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from envfile import load_env  # noqa: E402

STATE = HERE / "output" / "auto_state.json"
MAX_ATTEMPTS = 3  # после стольких ошибок площадка для клипа пропускается


# ---------- состояние ----------

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"videos": {}, "queue": []}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


# ---------- список видео канала ----------

def channel_videos_url(channel):
    """Ссылка на вкладку «Видео» канала. Можно дать ссылку на канал или на любое его видео."""
    import yt_dlp

    if "/watch" in channel or "youtu.be/" in channel or "/shorts/" in channel:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(channel, download=False)
        channel = info.get("channel_url") or info["uploader_url"]
    channel = channel.rstrip("/")
    for tab in ("/videos", "/shorts", "/streams", "/featured"):
        if channel.endswith(tab):
            channel = channel[: -len(tab)]
    return channel + "/videos"


def popular_videos(channel, min_duration):
    """Все видео канала, отсортированные по просмотрам (сначала популярные)."""
    import yt_dlp

    url = channel_videos_url(channel)
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": "in_playlist"}) as ydl:
        info = ydl.extract_info(url, download=False)
    videos = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        duration = e.get("duration")
        if duration and duration < min_duration:
            continue  # слишком короткое, резать нечего
        videos.append({"id": e["id"], "title": e.get("title") or e["id"],
                       "views": e.get("view_count") or 0,
                       "url": f"https://www.youtube.com/watch?v={e['id']}"})
    return sorted(videos, key=lambda v: v["views"], reverse=True)


# ---------- команды ----------

def interleave(groups):
    """Клипы разных видео вперемешку: 1-й из каждого, потом 2-й и т.д."""
    out = []
    for i in range(max((len(g) for g in groups), default=0)):
        out += [g[i] for g in groups if i < len(g)]
    return out


def cmd_prepare(args):
    import shorts

    if not args.channel:
        sys.exit("Не указан канал: добавьте YOUTUBE_CHANNEL=... в .env или флаг --channel")
    state = load_state()
    print(f"Ищу популярные видео: {args.channel}")
    todo = [v for v in popular_videos(args.channel, args.min_duration) if v["id"] not in state["videos"]]
    todo = todo[: args.videos]
    if not todo:
        print("Все видео канала уже нарезаны.")
        return

    groups = []
    for v in todo:
        print(f"\n=== {v['title']} ({v['views']} просмотров)")
        opts = shorts.build_parser().parse_args([
            v["url"], "--count", str(args.count), "--layout", args.layout,
            "--out", str(HERE / "output"), *(["--lang", args.lang] if args.lang else []),
        ])
        try:
            clips = shorts.make_shorts(opts)
        except Exception as e:  # одно битое видео не должно останавливать остальные
            print(f"  ОШИБКА: {e}")
            continue
        for c in clips:
            c.update(video_id=v["id"], posted={}, fails={})
        groups.append(clips)
        # Пока шла нарезка, post мог выложить клипы: перечитываем состояние перед записью.
        state = load_state()
        state["videos"][v["id"]] = {"title": v["title"], "views": v["views"], "clips": len(clips)}
        save_state(state)

    state = load_state()
    state["queue"] += interleave(groups)
    save_state(state)
    print(f"\nВ очередь добавлено клипов: {sum(len(g) for g in groups)}")


def cmd_post(args):
    import publish

    targets = [t.strip() for t in args.to.split(",") if t.strip()]
    state = load_state()
    # Каждая площадка идёт по очереди сама: ошибка YouTube не задерживает Telegram.
    tried = 0
    for t in targets:
        batch = [c for c in state["queue"]
                 if t not in c["posted"] and c["fails"].get(t, 0) < MAX_ATTEMPTS and Path(c["file"]).exists()]
        for clip in batch[: args.per_run]:
            tried += 1
            print(f"Публикую в {t}: {clip['title']}")
            link = publish.publish_all([clip], [t], privacy=args.privacy).get((0, t))
            if link:
                clip["posted"][t] = link
            else:
                clip["fails"][t] = clip["fails"].get(t, 0) + 1
    if not tried:
        print("Нечего выкладывать: запустите prepare, чтобы нарезать новые видео.")

    # Записываем отметки в свежую копию состояния (prepare мог за это время дописать очередь).
    fresh = load_state()
    by_file = {c["file"]: c for c in state["queue"]}
    fresh["queue"] = [by_file.get(c["file"], c) for c in fresh["queue"]]
    save_state(fresh)


def cmd_status(args):
    state = load_state()
    queue = state["queue"]
    waiting = [c for c in queue if not c["posted"]]
    print(f"Нарезано видео: {len(state['videos'])}, клипов: {len(queue)}, ждут публикации: {len(waiting)}")
    for c in waiting[:10]:
        print(f"  — {c['title']}")
    if len(waiting) > 10:
        print(f"  … и ещё {len(waiting) - 10}")


def main():
    load_env()
    ap = argparse.ArgumentParser(description="Автонарезка популярных видео канала и публикация по расписанию")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="нарезать самые популярные ещё не обработанные видео")
    p.add_argument("--channel", default=os.environ.get("YOUTUBE_CHANNEL"),
                   help="ссылка на канал или на любое его видео")
    p.add_argument("--videos", type=int, default=3, help="сколько новых видео нарезать за запуск")
    p.add_argument("--count", type=int, default=5, help="клипов из одного видео")
    p.add_argument("--layout", default="blur", choices=["crop", "blur"])
    p.add_argument("--lang", default="ru")
    p.add_argument("--min-duration", type=int, default=120, help="пропускать видео короче, сек")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("post", help="выложить следующие клипы из очереди")
    p.add_argument("--to", default=os.environ.get("PUBLISH_TO", "telegram"),
                   help="площадки через запятую: telegram,youtube,vk")
    p.add_argument("--per-run", type=int, default=1, help="сколько клипов выложить за запуск")
    p.add_argument("--privacy", default=os.environ.get("YOUTUBE_PRIVACY", "public"),
                   choices=["private", "unlisted", "public"])
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("status", help="показать очередь")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
