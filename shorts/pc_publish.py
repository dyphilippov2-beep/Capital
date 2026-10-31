#!/usr/bin/env python3
"""Публикация готовых шортсов в VK и Rutube с домашнего ПК через браузер.

    python pc_publish.py login               # открыть VK и Rutube, войти один раз по коду
    python pc_publish.py post [--to vk,rutube] [--private]   # выложить следующий клип (по умолчанию в VK)
    python pc_publish.py post --test ФАЙЛ.mp4  # проверка: один клип только для себя, без отметки
    python pc_publish.py status              # что уже выложено с ПК

Почему браузер: у Rutube вход только по коду (нет пароля для API), а токены VK живут около часа.
Вход хранится в профиле браузера shorts/.browser-profile (не в git, cookies никуда не копируются).
Клипы берутся из shorts/archive (render кладёт туда копии: из ready/ GitHub удаляет их после Telegram),
порядок — очередь из shorts/state/auto_state.json в origin/main, отметки — shorts/state/pc_posted.json.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import publish  # noqa: E402

PROFILE = HERE / ".browser-profile"
ARCHIVE = HERE / "archive"
POSTED = HERE / "state" / "pc_posted.json"
LOG = HERE / "pc_publish.log"
VK_GROUP = "titanhockeylab"
VK_GROUP_ID = 201384871  # «Титан - лаборатория хоккея»; у пользователя есть и другие сообщества
# Канал для шортсов в MAX (max.ru/join/GBaeEmx1FHM0uV-VKpkk24V4es8Sep9gxN55spwc6Qg) в веб-версии
MAX_CHAT, MAX_TITLE = "https://web.max.ru/-70968141949580", "Лаборатория хоккея «Титан»"
MAX_FAVORITES = "https://web.max.ru/0"
RUTUBE_CHANNEL = 23890442  # «Лаборатория хоккея ТИТАН»; у пользователя есть и другой аккаунт Rutube
LOGIN_URLS = ["https://vk.com/" + VK_GROUP, "https://rutube.ru/multipass/login/", "https://web.max.ru/"]


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def browser(p, headless=False):
    """Постоянный профиль: вход в VK и Rutube сохраняется между запусками."""
    opts = dict(user_data_dir=str(PROFILE), headless=headless, viewport={"width": 1400, "height": 900},
                locale="ru-RU", chromium_sandbox=True)  # без --no-sandbox: в профиле вход в VK и Rutube
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **opts)
    except Exception:  # нет Google Chrome — встроенный Chromium Playwright
        return p.chromium.launch_persistent_context(**opts)


# ---------- очередь ----------

def queue():
    """Очередь клипов из origin/main: её ведёт GitHub, рабочую копию не трогаем."""
    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=HERE, check=False)
    out = subprocess.run(["git", "show", "origin/main:shorts/state/auto_state.json"], cwd=HERE,
                         capture_output=True, check=True).stdout
    return json.loads(out.decode("utf-8"))["queue"]


def archive_path(clip):
    return ARCHIVE / f"{clip.get('video_id', '')}_{clip['start']:.1f}.mp4"


def load_posted():
    return json.loads(POSTED.read_text(encoding="utf-8")) if POSTED.exists() else {}


def save_posted(posted):
    POSTED.write_text(json.dumps(posted, ensure_ascii=False, indent=2), encoding="utf-8")


def next_clip(platform, posted):
    for clip in queue():
        if platform not in posted.get(clip["id"], {}) and archive_path(clip).is_file():
            return clip
    return None


# ---------- площадки ----------

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]


def vk_pick(page, label, value):
    """Выпадающий список календаря VK: поле с aria-label, пункт с точным текстом."""
    page.locator(f"input[aria-label*='{label}']").last.click()
    page.get_by_role("option", name=value, exact=True).last.click()
    page.wait_for_timeout(300)


def upload_vk(page, clip, private):
    # «Создать» на вкладке клипов сообщества: кнопка «+» в шапке VK Видео грузит в другое сообщество
    page.goto(f"https://vk.ru/{VK_GROUP}#section=short_videos", wait_until="domcontentloaded", timeout=90000)
    tab = page.get_by_role("tab", name="Клипы", exact=True)
    tab.first.wait_for(timeout=60000)
    tab.first.click()
    # «Создать» в ленте сообщества открывает меню: Пост, История, Клип…
    page.get_by_role("button", name="Создать", exact=True).first.click()
    page.get_by_text("Клип", exact=True).last.click()
    page.locator("input[type=file][accept*=video]").last.set_input_files(str(archive_path(clip)))
    text = page.locator("textarea[placeholder='О чём ваш клип?']")
    text.wait_for(timeout=120000)
    text.fill(f"{clip['title']}\n\n{publish.caption(clip, 'vk')}")
    link = page.get_by_text(re.compile(r"/clip-\d+_\d+")).first
    link.wait_for(timeout=120000)
    link = re.sub(r"\s+", "", link.inner_text())
    if f"clip-{VK_GROUP_ID}_" not in link:
        raise RuntimeError(f"VK: клип грузится не в то сообщество ({link}), не публикую")
    if private:
        # Проверка: откладываем публикацию на месяц вперёд — до этого клип никто не видит
        when = datetime.now() + timedelta(days=30)
        page.get_by_text("Когда опубликовать").first.click()
        page.wait_for_timeout(800)
        vk_pick(page, "месяц", MONTHS[when.month - 1])
        page.get_by_role("gridcell").filter(has_text=f", {when.day} {MONTHS_GEN[when.month - 1]}").first.click()
        vk_pick(page, "час", "10")
        vk_pick(page, "минут", "00")
        page.mouse.click(5, 5)  # закрыть календарь: он перекрывает кнопку
        name, done = "Запланировать публикацию", f"отложен до {when:%d.%m.%Y} 10:00"
    else:
        name, done = "Опубликовать", "опубликован"
    button = page.get_by_role("button", name=name, exact=True).last
    deadline = time.time() + 600  # кнопка активна только после обработки клипа
    while not button.is_enabled() and time.time() < deadline:
        page.wait_for_timeout(2000)
    button.click()
    page.wait_for_timeout(5000)
    page.screenshot(path=str(LOG.with_name("pc_publish_vk.png")))
    return f"{link} ({done})"


def pick_option(page, combobox, pattern):
    """Открывает выпадающий список формы Rutube и выбирает пункт по регулярному выражению."""
    combobox.click()
    page.wait_for_timeout(800)
    options = page.get_by_role("option")
    texts = [t.strip() for t in options.all_inner_texts()]
    for i, t in enumerate(texts):
        if re.search(pattern, t, re.I):
            options.nth(i).click()
            page.wait_for_timeout(1200)  # иначе фокус уходит в поиск по списку
            return t
    raise RuntimeError(f"Rutube: в списке нет пункта /{pattern}/, есть: {texts}")


def upload_rutube(page, clip, private):
    page.goto("https://studio.rutube.ru/", wait_until="domcontentloaded", timeout=90000)
    # У пользователя несколько аккаунтов Rutube: грузим, только если студия открыта на нужном канале
    page.locator("header img").last.click()
    channel = page.get_by_text("Перейти на канал").first.evaluate("e => (e.closest('a') || {}).href || ''")
    page.keyboard.press("Escape")
    if f"/channel/{RUTUBE_CHANNEL}/" not in channel:
        raise RuntimeError(f"Rutube: в профиле вход не в тот канал ({channel}), нужен {RUTUBE_CHANNEL} — "
                           "выполните pc_publish.py login и войдите в нужный аккаунт")
    page.get_by_role("button", name="Добавить").first.click()
    page.get_by_text("Загрузить видео или Shorts").first.click()
    page.locator("input[type=file]").set_input_files(str(archive_path(clip)))
    title = page.locator("input[name=title]")
    title.wait_for(timeout=120000)
    title.fill(clip["title"][:100])
    page.locator("textarea[name=description]").fill(publish.caption(clip, "rutube")[:5000])
    # Первый список формы — категория, у «Доступа» есть подпись
    pick_option(page, page.get_by_role("combobox").first, "^спорт")
    access = pick_option(page, page.get_by_role("combobox", name="Доступ"),
                         r"ссылк|только|мне" if private else r"для всех")
    link = page.get_by_text(re.compile(r"rutube\.ru/(video|shorts)/")).first.inner_text().strip()
    # «Опубликовать» срабатывает только после загрузки файла: ждём, пока кнопка станет активной
    button = page.get_by_role("button", name="Опубликовать")
    deadline = time.time() + 600
    while not button.is_enabled() and time.time() < deadline:
        page.wait_for_timeout(2000)
    button.click()
    page.wait_for_timeout(5000)
    page.screenshot(path=str(LOG.with_name("pc_publish_rutube.png")))
    return f"{link} ({access})"


def upload_max(page, clip, private):
    # Проверка уходит в «Избранное» (чат с самим собой): у каналов MAX нет режима «только для себя»
    chat = MAX_FAVORITES if private else MAX_CHAT
    page.goto(chat, wait_until="domcontentloaded", timeout=90000)
    attach = page.get_by_role("button", name="Загрузить файл")
    attach.wait_for(timeout=60000)
    if not private:
        # Тот ли канал: в шапке чата должно быть его название
        page.get_by_text(MAX_TITLE, exact=True).first.wait_for(timeout=30000)
    attach.click()
    with page.expect_file_chooser() as chooser:
        page.get_by_role("menuitem", name="Фото или видео").click()
    chooser.value.set_files(str(archive_path(clip)))
    page.get_by_role("button", name="Удалить вложение").wait_for(timeout=120000)
    page.get_by_role("textbox").last.click()
    # В MAX без блока соцсетей и хэштегов: название, ссылка на полное видео, описание
    parts = (clip["title"], publish.full_video_link(clip), clip.get("description", ""))
    page.keyboard.insert_text("\n\n".join(p for p in parts if p))
    page.get_by_role("button", name="Отправить сообщение").click()
    # Ждём, пока видео загрузится и вложение исчезнет из поля ввода
    page.get_by_role("button", name="Удалить вложение").wait_for(state="detached", timeout=600000)
    page.wait_for_timeout(15000)
    page.screenshot(path=str(LOG.with_name("pc_publish_max.png")))
    return f"{chat} ({'Избранное' if private else 'канал ' + MAX_TITLE})"


UPLOADERS = {"vk": upload_vk, "rutube": upload_rutube, "max": upload_max}


# ---------- команды ----------

def chrome_exe():
    for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if exe.is_file():
            return str(exe)
    raise SystemExit("Не найден Google Chrome")


def cmd_login(args):
    # Обычный Chrome без управления Playwright: страницы входа Rutube не открываются под автоматизацией
    PROFILE.mkdir(exist_ok=True)
    subprocess.run([chrome_exe(), f"--user-data-dir={PROFILE}", "--no-first-run", *LOGIN_URLS])
    print("Войдите в VK и Rutube в открывшемся окне, затем закройте окно.")


def cmd_post(args):
    from playwright.sync_api import sync_playwright

    posted = load_posted()
    # --test ФАЙЛ: один клип из archive только для себя, без отметки в pc_posted.json
    test = next((c for c in queue() if archive_path(c).name == args.test), None) if args.test else None
    if args.test and not test:
        raise SystemExit(f"Клипа {args.test} нет в очереди или в shorts/archive")
    private = args.private or bool(test)
    with sync_playwright() as p:
        ctx = browser(p, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for platform in args.to.split(","):
            clip = test or next_clip(platform, posted)
            if not clip:
                log(f"{platform}: нечего публиковать")
                continue
            log(f"{platform}: публикую «{clip['title']}» ({archive_path(clip).name})"
                + (" — только для себя" if private else ""))
            try:
                link = UPLOADERS[platform](page, clip, private)
            except Exception as e:  # одна площадка не должна мешать другой
                page.screenshot(path=str(LOG.with_name(f"pc_publish_{platform}_error.png")))
                log(f"{platform}: ОШИБКА {e}")
                continue
            log(f"{platform}: готово {link}")
            if not test:
                posted.setdefault(clip["id"], {})[platform] = {"at": f"{datetime.now():%Y-%m-%d %H:%M}",
                                                                "link": link, "private": private}
                save_posted(posted)
        ctx.close()


def cmd_status(args):
    posted = load_posted()
    for clip in queue():
        marks = ", ".join(f"{k}: {v['link']}" for k, v in posted.get(clip["id"], {}).items())
        print(f"{clip['title']} — {marks or 'не выложено'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)
    sub.add_parser("login", help="войти в VK и Rutube").set_defaults(func=cmd_login)
    p = sub.add_parser("post", help="выложить следующий клип")
    # Rutube выключен, пока в профиле не выполнен вход в аккаунт канала 23890442
    p.add_argument("--to", default="vk")
    p.add_argument("--private", action="store_true", help="только для себя (проверка)")
    p.add_argument("--headless", action="store_true", help="без окна браузера")
    p.add_argument("--test", metavar="ФАЙЛ", help="проверка: этот клип из archive, только для себя, без отметки")
    p.set_defaults(func=cmd_post)
    sub.add_parser("status", help="что выложено с ПК").set_defaults(func=cmd_status)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
