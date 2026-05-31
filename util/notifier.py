import json
import requests
from util.const import Telegram_bot_TOKEN, Telegram_chat_ID


def send_message(message, token=Telegram_bot_TOKEN, chat_id=Telegram_chat_ID):
    if not token or not chat_id:
        print(f"[Telegram skipped] {message}")
        return None

    target_url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        response = requests.post(
            target_url,
            data={
                "chat_id": chat_id,
                "text": str(message),
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


def get_updates(offset=None, token=Telegram_bot_TOKEN):
    """
    Telegram 채팅방에서 새 메시지를 가져온다.

    주의:
    - StrategyManager의 QTimer 안에서 실행되므로 long polling을 사용하지 않는다.
    - timeout=0으로 즉시 결과만 받아 매매 루프 지연을 최소화한다.
    """
    if not token:
        return []

    target_url = f"https://api.telegram.org/bot{token}/getUpdates"

    params = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }

    if offset is not None:
        params["offset"] = offset

    try:
        response = requests.get(
            target_url,
            params=params,
            timeout=3,
        )
        result = response.json()

        if not response.ok or not result.get("ok", False):
            print(f"[Telegram getUpdates failed] {result}")
            return []

        return result.get("result", [])

    except Exception as e:
        print(f"[Telegram getUpdates error] {e}")
        return []