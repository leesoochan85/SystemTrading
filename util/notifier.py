import requests
from util.const import Telegram_bot_TOKEN, Telegram_chat_ID

def send_message(message, token=Telegram_bot_TOKEN, chat_id=Telegram_chat_ID):
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
        
        if not response.ok or not result.get("ok",False):
            raise Exception(f"Telegram send failed: {result}")

        return result
    
    except Exception as e:
        raise Exception(f"텔레그램 메시지 전송 실패: {e}")