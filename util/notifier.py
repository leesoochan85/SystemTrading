import requests
from util.const import Telegram_bot_TOKEN, Telegram_chat_ID

def send_message(message, token=Telegram_bot_TOKEN, chat_id=Telegram_chat_ID):
    if not token or not chat_id:
        print(f"[Telegram skipped] {message}")
        return None

    Target_URL = "https://api.telegram.org/bot{}/sendMessage".format(token)

    try:
        response = requests.post(
            Target_URL,
            data={
                "chat_id": chat_id,
                "text": str(message)
            },
            timeout=10,
        )
        result = response.json()

        if not response.ok or not result.get("ok", False):
            print(f"[Telegram failed] {result}")
            return None

        return result

    except Exception as e:
        print(f"[Telegram error] {e}")
        return None