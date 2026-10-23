"""Проверка подключения Telegram-бота и отправка тестового сообщения в канал.
Токен и канал читаются из файла .env (не хранится в git)."""
import json
import pathlib
import sys
import urllib.parse
import urllib.request


def load_env(path=pathlib.Path(__file__).with_name(".env")):
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def call(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    with urllib.request.urlopen(url, data=data, timeout=15) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        sys.exit(f"{method} failed: {result}")
    return result["result"]


def main():
    env = load_env()
    token, channel = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHANNEL"]
    me = call(token, "getMe")
    print(f"Бот: @{me['username']} (id {me['id']})")
    member = call(token, "getChatMember", chat_id=channel, user_id=me["id"])
    print(f"Статус в {channel}: {member['status']}")
    text = sys.argv[1] if len(sys.argv) > 1 else "Проверка связи"
    msg = call(token, "sendMessage", chat_id=channel, text=text)
    print(f"Сообщение отправлено, message_id={msg['message_id']}")


if __name__ == "__main__":
    main()
