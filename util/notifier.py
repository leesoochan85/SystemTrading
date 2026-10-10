import json
import hashlib
import re
import threading
import time
from collections import OrderedDict
import requests
from util.const import Telegram_bot_TOKEN, Telegram_chat_ID


ERROR_REPEAT_INTERVAL_SECONDS = 600
ERROR_CACHE_MAX_ENTRIES = 512
_error_notifications = OrderedDict()
_error_notification_lock = threading.Lock()


# Traceback filenames remain useful, but their parent directories are private.
_TRACEBACK_FILE = re.compile(
    r'''(\bFile\s+["'])([^"'\r\n]+)(["'],\s+line\s+\d+)'''
)
_QUOTED_PATH = re.compile(
    r'''(?P<quote>["'])(?:[A-Za-z]:[\\/]|\\\\|//|/|~[\\/]|\.{1,2}[\\/])'''
    r'''[^"'\r\n]*(?P=quote)'''
)
_UNQUOTED_PATH = re.compile(
    # Match web URLs first so their slashes are never treated as local paths.
    r'''(?P<url>https?://[^\s<>"']+)'''
    r'''|(?:file|sqlite)://[^\s<>"']+'''
    r'''|(?<!\w)(?:[A-Za-z]:[\\/]|\\\\|//)[^\r\n"'<>|,;()]*'''
    # Require a directory separator after the first POSIX component. This
    # preserves Telegram commands such as /status and numeric ratios.
    r'''|(?<![\w:/\\])(?:/(?!/)|~[\\/]|\.{1,2}[\\/])'''
    r'''[^\s"'<>|,;:()\[\]{}\\/]+[\\/][^\r\n"'<>|,;:()\[\]{}]*'''
)


def sanitize_telegram_message(message):
    """Hide local paths in the outgoing copy; never change console logging.

    Traceback entries keep only the filename, line number and function name.
    Other recognized filesystem paths become [경로 숨김]. This is path
    redaction, not filtering of credentials or other personal information.
    """
    text = str(message)
    text = _TRACEBACK_FILE.sub(
        lambda match: (
            match.group(1)
            + re.split(r"[\\/]", match.group(2))[-1]
            + match.group(3)
        ),
        text,
    )
    text = _QUOTED_PATH.sub(
        lambda match: match.group("quote") + "[경로 숨김]" + match.group("quote"),
        text,
    )
    return _UNQUOTED_PATH.sub(
        lambda match: match.group("url") or "[경로 숨김]", text
    )


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
                "text": sanitize_telegram_message(message),
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


def send_error(message, *, event_key, error_identity=None, token=Telegram_bot_TOKEN, chat_id=Telegram_chat_ID):
    """Log every error locally; throttle identical outgoing error attempts.

    Scope includes the caller's event key, error identity, bot and destination.
    Plain messages use their exact text. Tracebacks use their header, unique
    frame locations and final exception line: re-raising one exception can
    grow its traceback without representing a different error. Broker codes
    can override identity so changing prices do not bypass the limit.
    The interval starts on an attempt, even if Telegram delivery fails, so a
    network outage cannot cause a blocking request on every trading tick.
    Suppressed calls return None. State is bounded and resets on process restart.
    """
    original = str(message)
    print(original)
    if not token or not chat_id:
        return None

    if error_identity is None:
        header, separator, traceback_text = original.partition("Traceback (most recent call last):")
        frames = re.findall(r'^\s*File ["\'][^\r\n]+, line \d+, in [^\r\n]+', traceback_text, re.MULTILINE)
        if separator and frames:
            error_identity = json.dumps([
                header,
                list(dict.fromkeys(frame.strip() for frame in frames)),
                traceback_text.rstrip().splitlines()[-1],
            ], ensure_ascii=False)
        else:
            error_identity = original

    key = (
        hashlib.sha256(str(token).encode("utf-8")).digest(),
        str(chat_id),
        str(event_key),
        hashlib.sha256(str(error_identity).encode("utf-8")).digest(),
    )
    with _error_notification_lock:
        now = time.monotonic()
        previous = _error_notifications.get(key)
        if previous is not None and now - previous["attempted_at"] < ERROR_REPEAT_INTERVAL_SECONDS:
            previous["suppressed"] += 1
            _error_notifications.move_to_end(key)
            return None

        suppressed = previous["suppressed"] if previous is not None else 0
        _error_notifications[key] = {"attempted_at": now, "suppressed": 0}
        _error_notifications.move_to_end(key)
        while len(_error_notifications) > ERROR_CACHE_MAX_ENTRIES:
            _error_notifications.popitem(last=False)

    outgoing = original
    if suppressed:
        outgoing += f"\n\n[반복 오류] 이전 전송 시도 이후 동일 오류 {suppressed}회 추가 발생 (CMD에 기록)"
    return send_message(outgoing, token=token, chat_id=chat_id)


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
            timeout=1,
        )
        result = response.json()

        if not response.ok or not result.get("ok", False):
            print(f"[Telegram getUpdates failed] {result}")
            return []

        return result.get("result", [])

    except Exception as e:
        print(f"[Telegram getUpdates error] {e}")
        return []
