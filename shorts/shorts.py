#!/usr/bin/env python3
"""Нарезка YouTube-видео на вертикальные шортсы и публикация в соцсети.

Пример:
    python shorts.py "https://youtu.be/XXXX" --count 5 --upload youtube,telegram
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ---------- 1. Скачивание ----------

VIDEO_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"


def ytdlp_opts(**extra):
    # Сервер GitHub в США: без этого YouTube отдаёт названия в автопереводе на английский
    opts = {"quiet": True, "noprogress": True, "http_headers": {"Accept-Language": "ru-RU,ru;q=0.9"},
            "extractor_args": {"youtube": {"lang": ["ru"]}}}
    # Файл cookies.txt из браузера: без него YouTube часто не отдаёт видео облачным серверам
    if os.environ.get("YTDLP_COOKIES") and Path(os.environ["YTDLP_COOKIES"]).exists():
        opts["cookiefile"] = os.environ["YTDLP_COOKIES"]
    opts.update(extra)
    return opts


def download(url, workdir, audio_only=False):
    import yt_dlp

    name = "audio" if audio_only else "source"
    opts = ytdlp_opts(outtmpl=str(workdir / f"{name}.%(ext)s"),
                      format="ba/b" if audio_only else VIDEO_FORMAT)
    if not audio_only:
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = workdir / f"{name}.mp4"
    if not path.exists():
        path = next(workdir.glob(f"{name}.*"))
    return path, info.get("title", ""), info.get("duration") or probe_duration(path)


def download_section(url, start, end, path):
    """Скачивает только нужный кусок видео (для облака: не тянуть весь ролик ради минуты)."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    opts = ytdlp_opts(format=VIDEO_FORMAT, merge_output_format="mp4",
                      outtmpl=str(path.with_suffix(".%(ext)s")),
                      download_ranges=download_range_func([], [(start, end)]),
                      force_keyframes_at_cuts=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    return path if path.exists() else next(path.parent.glob(path.stem + ".*"))


def render_from_source(clip, workdir, layout="blur", subs=True):
    """Рендер клипа, у которого есть только таймкоды и ссылка: качает кусок и делает mp4."""
    name = f"{slug(clip['title'])}_{int(clip['start'])}"
    part = download_section(clip["source_url"], clip["start"], clip["end"], workdir / f"{name}_src.mp4")
    sub_path = None
    if subs and clip.get("words"):
        sub_path = workdir / f"{name}.ass"
        write_subs(clip["words"], clip["start"], clip["end"], sub_path)
    out = workdir / f"{name}.mp4"
    render(part, {"start": 0, "end": clip["end"] - clip["start"]}, out, sub_path, layout)
    part.unlink(missing_ok=True)
    return out


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# ---------- 2. Расшифровка речи ----------

def transcribe(video, workdir, model_size, language):
    cache = workdir / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel

    print(f"Расшифровка речи (whisper {model_size})...")
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(video), language=language, word_timestamps=True, vad_filter=True)
    words = []
    lines = []
    for seg in segments:
        lines.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        for w in seg.words or []:
            words.append({"start": w.start, "end": w.end, "text": w.word.strip()})
    data = {"segments": lines, "words": words}
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ---------- 3. Выбор фрагментов ----------

SENTENCE_END = ("." , "!", "?", "…")


def build_sentences(words, pause=1.0, max_sentence=20.0):
    """Склеивает слова в предложения: конец — знак препинания или долгая пауза.

    Клипы собираются только из целых предложений, поэтому не могут
    начаться или закончиться на полуслове.
    """
    sentences, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = nxt["start"] - w["end"] if nxt else 0
        too_long = w["end"] - cur[0]["start"] > max_sentence
        if not nxt or w["text"].endswith(SENTENCE_END) or gap >= pause or too_long:
            sentences.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                              "text": " ".join(x["text"] for x in cur)})
            cur = []
    return sentences


def clip_bounds(sentences, first, last, duration):
    """Время клипа по номерам предложений + небольшие поля, не залезая в соседние фразы."""
    start = sentences[first]["start"] - 0.3
    end = sentences[last]["end"] + 0.6
    if first > 0:
        start = max(start, (sentences[first - 1]["end"] + sentences[first]["start"]) / 2)
    if last + 1 < len(sentences):
        end = min(end, (sentences[last]["end"] + sentences[last + 1]["start"]) / 2)
    return max(start, 0.0), min(end, duration)


def pick_clips_ai(title, sentences, duration, count, min_len, max_len):
    import anthropic
    from pydantic import BaseModel

    class Clip(BaseModel):
        first_sentence: int
        last_sentence: int
        why: str
        title: str
        description: str
        hashtags: list[str]

    class Clips(BaseModel):
        clips: list[Clip]

    text = "\n".join(f"#{i} [{s['start']:.1f}–{s['end']:.1f}] {s['text']}" for i, s in enumerate(sentences))
    prompt = f"""Видео: «{title}». Ниже расшифровка, разбитая на пронумерованные предложения с таймкодами в секундах.

{text}

Выбери до {count} фрагментов для коротких вертикальных видео (Shorts / Reels / TikTok). Фрагмент задаётся номерами первого и последнего предложения (включительно), длина {min_len}–{max_len} секунд по таймкодам.

Главное требование — каждый фрагмент должен быть понятен зрителю, который не видел остального видео:
- Начало: первое предложение само по себе вводит тему или цепляет (вопрос, сильное утверждение, начало истории). Не начинай с предложения, которое продолжает предыдущую мысль или ссылается на сказанное раньше («и поэтому», «как я уже говорил», «это», «вот он» без пояснения, ответ на вопрос, которого нет во фрагменте).
- Конец: последнее предложение завершает мысль — вывод, ответ, кульминация, панчлайн. Не обрывай на «и вот…», на перечислении или до ответа на поставленный вопрос.
- Внутри: одна цельная мысль или история, без резких переходов на другую тему.

Лучше вернуть меньше фрагментов, чем включить фрагмент, который логически не начинается или не заканчивается. Фрагменты не пересекаются.

Для каждого фрагмента: why — одной фразой, чем он начинается и чем завершается; title — кликабельный заголовок до 70 символов; description — 1–2 предложения; hashtags — 3–6 хэштегов без «#». Пиши на языке видео."""

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-5-5",
        max_tokens=16000,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Clips,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Claude не вернул фрагменты (stop_reason={response.stop_reason})")

    clips, used = [], set()
    for c in response.parsed_output.clips:
        a, b = c.first_sentence, c.last_sentence
        if not (0 <= a <= b < len(sentences)) or used & set(range(a, b + 1)):
            print(f"  пропущен некорректный фрагмент #{a}–#{b}")
            continue
        start, end = clip_bounds(sentences, a, b, duration)
        if not (min_len - 5 <= end - start <= max_len + 15):
            print(f"  пропущен фрагмент #{a}–#{b}: длина {end - start:.0f} c вне диапазона")
            continue
        used |= set(range(a, b + 1))
        clips.append({"start": start, "end": end, "title": c.title, "description": c.description,
                      "hashtags": c.hashtags, "why": c.why,
                      "text": " ".join(s["text"] for s in sentences[a:b + 1])})
    return sorted(clips, key=lambda c: c["start"])


def parse_time(s):
    parts = [float(p) for p in s.strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def pick_clips_manual(spec, title, sentences, duration):
    """Ручные таймкоды; если есть расшифровка — подтягиваются к ближайшим границам предложений."""
    clips = []
    for i, rng in enumerate(spec.split(","), 1):
        a, b = (parse_time(x) for x in rng.split("-"))
        if sentences:
            first = min(range(len(sentences)), key=lambda k: abs(sentences[k]["start"] - a))
            last = min(range(first, len(sentences)), key=lambda k: abs(sentences[k]["end"] - b))
            a, b = clip_bounds(sentences, first, last, duration)
        clips.append({"start": a, "end": b,
                      "title": f"{title} — часть {i}", "description": title, "hashtags": ["shorts"]})
    return clips


def first_phrase(text, limit=70):
    """Название клипа без ИИ: первая фраза, обрезанная по слову."""
    text = text.strip().rstrip(".…")
    if len(text) <= limit:
        return text[:1].upper() + text[1:]
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut[:1].upper() + cut[1:] + "…"


def pick_clips_by_sentences(sentences, duration, count, min_len, max_len, title):
    """Без ИИ: набирает подряд целые предложения до нужной длины, режет по самым длинным паузам."""
    clips, i = [], 0
    while i < len(sentences) and len(clips) < count:
        best = None
        for j in range(i, len(sentences)):
            length = sentences[j]["end"] - sentences[i]["start"]
            if length > max_len:
                break
            if length >= min_len:
                pause = (sentences[j + 1]["start"] - sentences[j]["end"]) if j + 1 < len(sentences) else 99
                if best is None or pause > best[1]:
                    best = (j, pause)
        if best is None:
            i += 1
            continue
        start, end = clip_bounds(sentences, i, best[0], duration)
        clips.append({"start": start, "end": end, "title": first_phrase(sentences[i]["text"]) or title,
                      "description": title, "hashtags": ["shorts"],
                      "text": " ".join(s["text"] for s in sentences[i:best[0] + 1])})
        i = best[0] + 1
    return clips


# ---------- 4. Рендер ----------

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,78,&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_subs(words, start, end, path, words_per_line=3):
    """Крупные субтитры по 2–4 слова, как в типичных шортсах."""
    chunk = [w for w in words if w["end"] > start and w["start"] < end]
    events = []
    for i in range(0, len(chunk), words_per_line):
        group = chunk[i:i + words_per_line]
        text = " ".join(w["text"] for w in group).replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{ass_time(group[0]['start'] - start)},"
                      f"{ass_time(group[-1]['end'] - start)},Default,,0,0,0,,{text.upper()}")
    path.write_text(ASS_HEADER + "\n".join(events) + "\n", encoding="utf-8")


def render(video, clip, out_path, subs_path, layout):
    if layout == "blur":
        # Всё горизонтальное видео по центру, сверху и снизу — размытая копия.
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=30:5[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        vf = "crop=ih*9/16:ih,scale=1080:1920"
    if subs_path:
        vf += f",ass={subs_path.name}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{clip['start']:.2f}", "-to", f"{clip['end']:.2f}", "-i", str(video.resolve()),
        "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path.name,
    ]
    # cwd = папка клипа, чтобы путь к .ass не надо было экранировать для ffmpeg
    subprocess.run(cmd, check=True, cwd=out_path.parent)


# ---------- main ----------

def slug(s):
    return re.sub(r"[^\w-]+", "_", s, flags=re.UNICODE).strip("_")[:40] or "clip"


def build_parser():
    ap = argparse.ArgumentParser(description="YouTube → вертикальные шортсы → соцсети")
    ap.add_argument("url", help="ссылка на YouTube/Rutube (или путь к локальному видео)")
    ap.add_argument("--count", type=int, default=5, help="сколько клипов сделать")
    ap.add_argument("--min", type=int, default=20, dest="min_len", help="мин. длина клипа, сек")
    ap.add_argument("--max", type=int, default=59, dest="max_len", help="макс. длина клипа, сек")
    ap.add_argument("--clips", help='ручные таймкоды: "1:05-1:40,12:00-12:45"')
    ap.add_argument("--no-ai", action="store_true", help="не звать Claude, резать равными кусками")
    ap.add_argument("--layout", choices=["crop", "blur"], default="crop",
                    help="crop — обрезать центр в 9:16; blur — вписать целиком на размытом фоне")
    ap.add_argument("--no-subs", action="store_true", help="без вшитых субтитров")
    ap.add_argument("--whisper", default="small", help="модель whisper: tiny/base/small/medium/large-v3")
    ap.add_argument("--lang", default=None, help="язык речи (ru, en...), по умолчанию автоопределение")
    ap.add_argument("--out", default="output", help="папка для результата")
    ap.add_argument("--upload", default="", help="куда выложить: youtube,telegram,vk")
    ap.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"],
                    help="видимость на YouTube (по умолчанию private — проверить перед публикацией)")
    return ap


def video_key(url):
    """Имя рабочей папки: id видео YouTube/Rutube, иначе короткий хэш ссылки.

    Своя папка у каждого видео нужна, чтобы не подхватить расшифровку от другого.
    """
    m = (re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url)
         or re.search(r"rutube\.ru/(?:video|shorts)/([0-9a-f]{32})", url))
    return m.group(1) if m else hashlib.md5(url.encode()).hexdigest()[:12]


def make_shorts(args, render_clips=True):
    """Скачивает, расшифровывает, выбирает и рендерит клипы. Возвращает список клипов.

    render_clips=False (режим для облака): качается только звук, клипы не рендерятся,
    а в каждый клип записываются ссылка на видео и слова для субтитров.
    """
    out_root = Path(args.out).resolve()
    if Path(args.url).exists():
        video = Path(args.url).resolve()
        title, duration = video.stem, probe_duration(video)
        workdir = out_root / slug(title)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = out_root / video_key(args.url)
        workdir.mkdir(parents=True, exist_ok=True)
        print("Скачивание...")
        video, title, duration = download(args.url, workdir, audio_only=not render_clips)
    print(f"Видео: {title} ({duration / 60:.1f} мин)")

    transcript = transcribe(video, workdir, args.whisper, args.lang)
    sentences = build_sentences(transcript["words"])

    if args.clips:
        clips = pick_clips_manual(args.clips, title, sentences, duration)
    elif args.no_ai:
        clips = pick_clips_by_sentences(sentences, duration, args.count, args.min_len, args.max_len, title)
    else:
        print("Выбор законченных по смыслу фрагментов (Claude)...")
        try:
            clips = pick_clips_ai(title, sentences, duration, args.count, args.min_len, args.max_len)
        except Exception as e:  # нет ключа, сеть, отказ — не бросаем работу
            print(f"  ИИ недоступен ({e}); режу по границам предложений.")
            clips = pick_clips_by_sentences(sentences, duration, args.count, args.min_len, args.max_len, title)

    if not render_clips:
        for clip in clips:
            clip["end"] = min(clip["end"], duration)
            clip["source_url"] = args.url
            clip["words"] = [w for w in transcript["words"] if w["end"] > clip["start"] and w["start"] < clip["end"]]
        video.unlink(missing_ok=True)
        return clips

    results = []
    for i, clip in enumerate(clips, 1):
        clip["end"] = min(clip["end"], duration)
        name = f"{i:02d}_{slug(clip['title'])}"
        out = workdir / f"{name}.mp4"
        subs = None
        if transcript and not args.no_subs:
            subs = workdir / f"{name}.ass"
            write_subs(transcript["words"], clip["start"], clip["end"], subs)
        print(f"[{i}/{len(clips)}] {clip['start']:.0f}s–{clip['end']:.0f}s  {clip['title']}")
        if clip.get("text"):
            words = clip["text"].split()
            print(f"      «{' '.join(words[:8])} … {' '.join(words[-8:])}»")
        render(video, clip, out, subs, args.layout)
        clip["file"] = str(out)
        results.append(clip)

    (workdir / "clips.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово: {len(results)} клипов в {workdir}")
    return results


def main():
    args = build_parser().parse_args()
    results = make_shorts(args)
    targets = [t.strip() for t in args.upload.split(",") if t.strip()]
    if targets:
        import publish
        publish.publish_all(results, targets, privacy=args.privacy)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    from envfile import load_env
    load_env()
    main()
