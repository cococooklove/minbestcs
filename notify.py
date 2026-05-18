"""
Telegram 알림 모듈 — 주요 실패 이벤트를 봇으로 푸시.

환경변수:
  TELEGRAM_BOT_TOKEN  — BotFather 가 발급한 봇 토큰
  TELEGRAM_CHAT_ID    — 알림 받을 채팅(나/그룹) ID

둘 다 설정되지 않으면 알림은 무음(no-op). 외부 의존성 없음(urllib).
"""
from __future__ import annotations
import os, time, threading, traceback, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# 동일 (제목+첫 필드) 60초 내 중복 차단 — 폭주 방지
_recent: "dict[str, float]" = {}
_lock = threading.Lock()

_KST = timezone(timedelta(hours=9))


def _enabled() -> bool:
    return bool(_BOT_TOKEN and _CHAT_ID)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _send_async(text: str) -> None:
    def _post():
        try:
            data = urllib.parse.urlencode({
                "chat_id": _CHAT_ID,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
                data=data, method="POST",
            )
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            print(f"[notify] 전송 실패: {e}", flush=True)
    threading.Thread(target=_post, daemon=True).start()


def alert(title: str, **fields) -> None:
    """주요 이벤트 알림. 60초 내 동일 키는 중복 차단."""
    if not _enabled():
        return
    first_val = next(iter(fields.values()), "") if fields else ""
    key = f"{title}:{first_val}"[:200]
    now = time.monotonic()
    with _lock:
        last = _recent.get(key, 0)
        if now - last < 60:
            return
        _recent[key] = now
        if len(_recent) > 200:
            _recent.clear()

    ts = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"<b>{_escape(title)}</b>", f"시각: {ts} KST"]
    for k, v in fields.items():
        v_str = "-" if v is None else str(v)
        if len(v_str) > 1500:
            v_str = v_str[:1500] + "…"
        lines.append(f"{_escape(k)}: <code>{_escape(v_str)}</code>")
    _send_async("\n".join(lines))


def alert_exception(title: str, exc: BaseException, **fields) -> None:
    """예외 알림 — 타입/메시지/트레이스백을 함께 전송."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    alert(
        title,
        type=type(exc).__name__,
        error=str(exc),
        traceback=tb[-1500:],
        **fields,
    )
