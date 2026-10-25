#!/usr/bin/env python3
"""Автопилот: самые популярные видео канала → шортсы → публикация по расписанию.

    python auto.py prepare            # нарезать самые популярные ещё не обработанные видео в очередь
    python auto.py post               # выложить следующий клип из очереди
    python auto.py status             # что в очереди и что уже вышло
    python auto.py top                # самые популярные видео канала
    python auto.py transcribe         # расшифровать популярные видео для ручного подбора моментов
    python auto.py enqueue            # поставить в очередь моменты, подобранные вручную (state/picks)

Канал и площадки берутся из .env (YOUTUBE_CHANNEL, PUBLISH_TO), их можно переопределить флагами.
По расписанию запускается из GitHub Actions (.github/workflows/shorts.yml)
или из Планировщика заданий Windows (schedule.bat).
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from envfile import load_env  # noqa: E402

# В облаке (GitHub Actions) состояние хранится в репозитории, путь задаёт AUTO_STATE
STATE = Path(os.environ.get("AUTO_STATE") or HERE / "output" / "auto_state.json")
# Ручной подбор: расшифровки видео и отобранные по ним моменты лежат рядом с состоянием
TRANSCRIPTS = STATE.parent / "transcripts"
PICKS = STATE.parent / "picks"
MAX_ATTEMPTS = 3  # после стольких ошибок площадка для клипа пропускается
PREPARE_DEFAULTS = {}  # заполняется в main(): параметры prepare для запуска из post


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
    from shorts import ytdlp_opts

    if "/watch" in channel or "youtu.be/" in channel or "/shorts/" in channel:
        with yt_dlp.YoutubeDL(ytdlp_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(channel, download=False)
        channel = info.get("channel_url") or info["uploader_url"]
    channel = channel.rstrip("/")
    for tab in ("/videos", "/shorts", "/streams", "/featured"):
        if channel.endswith(tab):
            channel = channel[: -len(tab)]
    return channel + "/videos"


def channel_listing(url, lang):
    import yt_dlp
    from shorts import ytdlp_opts

    opts = ytdlp_opts(extract_flat="in_playlist", extractor_args={"youtube": {"lang": [lang]}},
                      http_headers={"Accept-Language": lang})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return [e for e in ydl.extract_info(url, download=False).get("entries") or [] if e and e.get("id")]


def popular_videos(channel, min_duration):
    """Все видео канала, отсортированные по просмотрам (сначала популярные)."""
    url = channel_videos_url(channel)
    # Просмотры берём из английской страницы: «29 тыс. просмотров» yt-dlp читает как 29.
    # Названия — из русской, иначе сервер в США получает автоперевод на английский.
    titles = {e["id"]: e.get("title") for e in channel_listing(url, "ru")}
    videos = []
    for e in channel_listing(url, "en"):
        duration = e.get("duration")
        if duration and duration < min_duration:
            continue  # слишком короткое, резать нечего
        videos.append({"id": e["id"], "title": titles.get(e["id"]) or e.get("title") or e["id"],
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
            # В облаке клипы не рендерятся заранее: хранятся таймкоды, видео качается при публикации
            clips = shorts.make_shorts(opts, render_clips=not args.cloud)
        except Exception as e:  # одно битое видео не должно останавливать остальные
            print(f"  ОШИБКА: {e}")
            continue
        for c in clips:
            c.update(id=f"{v['id']}:{c['start']:.1f}", video_id=v["id"], posted={}, fails={})
        groups.append(clips)
        # Пока шла нарезка, post мог выложить клипы: перечитываем состояние перед записью.
        state = load_state()
        state["videos"][v["id"]] = {"title": v["title"], "views": v["views"], "clips": len(clips)}
        save_state(state)

    state = load_state()
    state["queue"] += interleave(groups)
    save_state(state)
    print(f"\nВ очередь добавлено клипов: {sum(len(g) for g in groups)}")


def cmd_transcribe(args):
    """Расшифровывает популярные видео, чтобы моменты для шортсов можно было выбрать по смыслу.

    Пишет state/transcripts/<id>.json (предложения и слова) и <id>.txt для чтения:
    «#номер [начало–конец] текст». Подобранные моменты кладутся в state/picks/<id>.json.
    """
    import shorts

    if not args.channel:
        sys.exit("Не указан канал: добавьте YOUTUBE_CHANNEL=... в .env или флаг --channel")
    state = load_state()
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    todo = [v for v in popular_videos(args.channel, args.min_duration)
            if v["id"] not in state["videos"] and not (TRANSCRIPTS / f"{v['id']}.json").exists()]
    for v in todo[: args.videos]:
        print(f"\n=== {v['title']} ({v['views']} просмотров)")
        workdir = HERE / "output" / v["id"]
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            audio, title, duration = shorts.download(v["url"], workdir, audio_only=True)
            words = shorts.transcribe(audio, workdir, args.whisper, args.lang)["words"]
        except Exception as e:  # одно битое видео не должно останавливать остальные
            print(f"  ОШИБКА: {e}")
            continue
        audio.unlink(missing_ok=True)
        sentences = shorts.build_sentences(words)
        data = {**v, "title": title or v["title"], "duration": duration, "sentences": sentences, "words": words}
        (TRANSCRIPTS / f"{v['id']}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        lines = [f"{data['title']} — {v['views']} просмотров — {v['url']}", ""]
        lines += [f"#{i} [{s['start']:.1f}–{s['end']:.1f}] {s['text']}" for i, s in enumerate(sentences)]
        (TRANSCRIPTS / f"{v['id']}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  расшифровано предложений: {len(sentences)}")


def cmd_enqueue(args=None):
    """Ставит в очередь моменты из state/picks/<id>.json.

    Формат файла: список {"first": N, "last": M, "title": ..., "description": ..., "hashtags": [...]},
    где first/last — номера предложений из расшифровки (включительно).
    """
    import shorts

    state = load_state()
    groups = []
    for path in sorted(PICKS.glob("*.json")) if PICKS.exists() else []:
        vid = path.stem
        if vid in state["videos"]:
            continue
        t = json.loads((TRANSCRIPTS / f"{vid}.json").read_text(encoding="utf-8"))
        sentences, clips = t["sentences"], []
        for p in json.loads(path.read_text(encoding="utf-8")):
            start, end = shorts.clip_bounds(sentences, p["first"], p["last"], t["duration"])
            clips.append({
                "start": start, "end": end, "title": p["title"], "description": p.get("description", ""),
                "hashtags": p.get("hashtags", []),
                "text": " ".join(s["text"] for s in sentences[p["first"]:p["last"] + 1]),
                "source_url": t["url"],
                "words": [w for w in t["words"] if w["end"] > start and w["start"] < end],
                "id": f"{vid}:{start:.1f}", "video_id": vid, "posted": {}, "fails": {},
            })
        state["videos"][vid] = {"title": t["title"], "views": t["views"], "clips": len(clips)}
        groups.append(clips)
        print(f"В очередь: {t['title']} — моментов {len(clips)}")
    if groups:
        state["queue"] += interleave(groups)
        save_state(state)


def postable(clip):
    return Path(clip.get("file", "")).is_file() or bool(clip.get("source_url"))


def cmd_post(args):
    import publish
    import shorts

    targets = [t.strip() for t in args.to.split(",") if t.strip()]

    def todo(state, t):
        return [c for c in state["queue"]
                if t not in c["posted"] and c["fails"].get(t, 0) < MAX_ATTEMPTS and postable(c)]

    cmd_enqueue()
    state = load_state()
    if args.prepare_if_empty and not any(todo(state, t) for t in targets):
        print("Очередь пуста, нарезаю следующее популярное видео.")
        cmd_prepare(argparse.Namespace(**{**PREPARE_DEFAULTS, "videos": 1, "cloud": args.cloud}))
        state = load_state()

    # Каждая площадка идёт по очереди сама: ошибка YouTube не задерживает Telegram.
    tried = 0
    tmp = HERE / "output" / "render"
    for t in targets:
        for clip in todo(state, t)[: args.per_run]:
            tried += 1
            print(f"Публикую в {t}: {clip['title']}")
            try:
                if not Path(clip.get("file", "")).is_file():
                    tmp.mkdir(parents=True, exist_ok=True)
                    clip["file"] = str(shorts.render_from_source(clip, tmp, layout=args.layout))
                link = publish.publish_all([clip], [t], privacy=args.privacy).get((0, t))
            except Exception as e:  # не скачалось или не отрендерилось
                print(f"  ОШИБКА подготовки клипа: {e}")
                link = None
            if link:
                clip["posted"][t] = link
            else:
                clip["fails"][t] = clip["fails"].get(t, 0) + 1
    if not tried:
        print("Нечего выкладывать: нужны новые подобранные моменты (transcribe → picks) или prepare.")

    # Записываем отметки в свежую копию состояния (prepare мог за это время дописать очередь).
    fresh = load_state()
    by_id = {clip_id(c): c for c in state["queue"]}
    fresh["queue"] = [by_id.get(clip_id(c), c) for c in fresh["queue"]]
    save_state(fresh)


def clip_id(c):
    return c.get("id") or c["file"]


def cmd_top(args):
    if not args.channel:
        sys.exit("Не указан канал: добавьте YOUTUBE_CHANNEL=... в .env или флаг --channel")
    done = load_state()["videos"]
    for i, v in enumerate(popular_videos(args.channel, 0)[: args.n], 1):
        mark = "  [нарезано]" if v["id"] in done else ""
        print(f"{i:2d}. {v['title']} — {v['views']} просмотров  {v['url']}{mark}")


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
    ap.add_argument("--cloud", action="store_true", default=os.environ.get("AUTO_CLOUD") == "1",
                    help="режим для GitHub Actions: не хранить видео, качать кусок при публикации")
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
    PREPARE_DEFAULTS.update({a.dest: a.default for a in p._actions if a.dest != "help"})

    p = sub.add_parser("post", help="выложить следующие клипы из очереди")
    p.add_argument("--to", default=os.environ.get("PUBLISH_TO", "telegram"),
                   help="площадки через запятую: telegram,youtube,vk,rutube")
    p.add_argument("--per-run", type=int, default=1, help="сколько клипов выложить за запуск")
    p.add_argument("--privacy", default=os.environ.get("YOUTUBE_PRIVACY", "public"),
                   choices=["private", "unlisted", "public"])
    p.add_argument("--layout", default="blur", choices=["crop", "blur"])
    p.add_argument("--prepare-if-empty", action="store_true",
                   help="если клипов не осталось, сначала нарезать следующее видео")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("transcribe", help="расшифровать популярные видео для ручного подбора моментов")
    p.add_argument("--channel", default=os.environ.get("YOUTUBE_CHANNEL"))
    p.add_argument("--videos", type=int, default=5, help="сколько новых видео расшифровать")
    p.add_argument("--lang", default="ru")
    p.add_argument("--whisper", default=os.environ.get("WHISPER_MODEL", "small"),
                   help="модель whisper: small быстрее, medium точнее")
    p.add_argument("--min-duration", type=int, default=120, help="пропускать видео короче, сек")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("enqueue", help="поставить в очередь моменты из state/picks")
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("top", help="самые популярные видео канала")
    p.add_argument("--channel", default=os.environ.get("YOUTUBE_CHANNEL"))
    p.add_argument("-n", type=int, default=20, help="сколько показать")
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("status", help="показать очередь")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
