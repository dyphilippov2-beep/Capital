#!/usr/bin/env python3
"""Нарезка YouTube-видео на вертикальные шортсы и публикация в соцсети.

Пример:
    python shorts.py "https://youtu.be/XXXX" --count 5 --upload youtube,telegram
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ---------- 1. Скачивание ----------

def download(url, workdir):
    import yt_dlp

    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "quiet": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = workdir / "source.mp4"
    if not path.exists():
        path = next(workdir.glob("source.*"))
    return path, info.get("title", ""), info.get("duration") or probe_duration(path)


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
        return json.loads(cache.read_text())

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
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


# ---------- 3. Выбор фрагментов ----------

def pick_clips_ai(title, transcript, count, min_len, max_len):
    import anthropic
    from pydantic import BaseModel

    class Clip(BaseModel):
        start: float
        end: float
        title: str
        description: str
        hashtags: list[str]

    class Clips(BaseModel):
        clips: list[Clip]

    text = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in transcript["segments"])
    prompt = (
        f"Видео: «{title}». Ниже расшифровка с таймкодами в секундах.\n\n{text}\n\n"
        f"Выбери {count} самых цепляющих фрагментов для коротких вертикальных видео "
        f"(YouTube Shorts / Reels / TikTok). Каждый фрагмент — законченная мысль длиной "
        f"{min_len}–{max_len} секунд, начинается с сильной фразы-крючка, границы совпадают "
        f"с границами фраз из расшифровки. Фрагменты не должны пересекаться. Для каждого дай "
        f"короткий кликабельный заголовок (до 70 символов), описание в 1–2 предложения и 3–6 "
        f"хэштегов без символа #. Пиши на языке видео."
    )
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-5-5",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        output_format=Clips,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Claude не вернул фрагменты (stop_reason={response.stop_reason})")
    return [c.model_dump() for c in response.parsed_output.clips]


def parse_time(s):
    parts = [float(p) for p in s.strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def pick_clips_manual(spec, title):
    clips = []
    for i, rng in enumerate(spec.split(","), 1):
        a, b = rng.split("-")
        clips.append({"start": parse_time(a), "end": parse_time(b),
                      "title": f"{title} — часть {i}", "description": title, "hashtags": ["shorts"]})
    return clips


def pick_clips_even(duration, count, length, title):
    step = duration / count
    return [{"start": i * step, "end": min(i * step + length, duration),
             "title": f"{title} — часть {i + 1}", "description": title, "hashtags": ["shorts"]}
            for i in range(count)]


# ---------- 4. Рендер ----------

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,78,&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,520,1

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


def main():
    ap = argparse.ArgumentParser(description="YouTube → вертикальные шортсы → соцсети")
    ap.add_argument("url", help="ссылка на YouTube (или путь к локальному видео)")
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
    args = ap.parse_args()

    out_root = Path(args.out).resolve()
    if Path(args.url).exists():
        video = Path(args.url).resolve()
        title, duration = video.stem, probe_duration(video)
        workdir = out_root / slug(title)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", args.url)
        workdir = out_root / (m.group(1) if m else "video")
        workdir.mkdir(parents=True, exist_ok=True)
        print("Скачивание...")
        video, title, duration = download(args.url, workdir)
    print(f"Видео: {title} ({duration / 60:.1f} мин)")

    need_transcript = not args.no_subs or not (args.clips or args.no_ai)
    transcript = transcribe(video, workdir, args.whisper, args.lang) if need_transcript else None

    if args.clips:
        clips = pick_clips_manual(args.clips, title)
    elif args.no_ai:
        clips = pick_clips_even(duration, args.count, args.max_len, title)
    else:
        print("Выбор лучших моментов (Claude)...")
        try:
            clips = pick_clips_ai(title, transcript, args.count, args.min_len, args.max_len)
        except Exception as e:  # нет ключа, сеть, отказ — не бросаем работу
            print(f"  ИИ недоступен ({e}); режу равными кусками.")
            clips = pick_clips_even(duration, args.count, args.max_len, title)

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
        render(video, clip, out, subs, args.layout)
        clip["file"] = str(out)
        results.append(clip)

    (workdir / "clips.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nГотово: {len(results)} клипов в {workdir}")

    targets = [t.strip() for t in args.upload.split(",") if t.strip()]
    if targets:
        import publish
        publish.publish_all(results, targets, privacy=args.privacy)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
