"""課堂 token 的簽發與無狀態驗證。"""

import base64
import binascii
import hashlib
import hmac
import re
import unicodedata
from datetime import datetime, timedelta, timezone


PREFIX = "leapkey-hwdc26-"
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")  # 固定 UTC+8；不用 ZoneInfo，避免 Windows 缺 tzdata
DATE = re.compile(r"[0-9]{8}\Z")
# YYYYMMDD(8) + "-" + 至少 2 字元暱稱編碼 + "-" + 22 字元簽章
MIN_REST = 8 + 1 + 2 + 1 + 22
MAX_TOKEN_LENGTH = 256


def expiry_day(value: str) -> datetime:
    if not DATE.fullmatch(value):
        raise ValueError("TOKEN_EXPIRES_ON 必須是 YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=TAIPEI)
    except ValueError as exc:
        raise ValueError("TOKEN_EXPIRES_ON 日期無效") from exc


def expires_at(day: str) -> datetime:
    return expiry_day(day) + timedelta(days=1) - timedelta(seconds=1)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issue(nickname: str, day: str, secret: str) -> str:
    expiry_day(day)
    if not nickname or len(nickname) > 20 or nickname != nickname.strip() or any(
        unicodedata.category(c).startswith("C") for c in nickname
    ):
        raise ValueError("暱稱格式無效")
    unsigned = f"{PREFIX}{day}-{_encode(nickname.encode('utf-8'))}"
    signature = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()[:16]
    return f"{unsigned}-{_encode(signature)}"


def verify(token: str, secret: str, *, now: datetime | None = None) -> str:
    if "." in token and not token.startswith(PREFIX):
        raise ValueError("舊格式 token 已停用，請重新 make claim")
    if not token.startswith(PREFIX):
        raise ValueError("token 過期或無效")
    # 畸形輸入一律回傳明確拒絕：過短、過長、非字串內容都不得拋出 IndexError。
    if len(token) < len(PREFIX) + MIN_REST or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("token 過期或無效")
    try:
        rest = token[len(PREFIX):]
        day = rest[:8]
        if rest[8] != "-" or rest[-23] != "-":
            raise ValueError
        encoded_nick, encoded_sig = rest[9:-23], rest[-22:]
        expiry = expires_at(day)
        nick_bytes = base64.b64decode(encoded_nick + "=" * (-len(encoded_nick) % 4), altchars=b"-_", validate=True)
        nickname = nick_bytes.decode("utf-8")
        if not nickname or len(nickname) > 20 or nickname != nickname.strip() or any(
            unicodedata.category(c).startswith("C") for c in nickname
        ):
            raise ValueError
        if _encode(nick_bytes) != encoded_nick:
            raise ValueError
        signature = base64.b64decode(encoded_sig + "=" * (-len(encoded_sig) % 4), altchars=b"-_", validate=True)
        unsigned = f"{PREFIX}{day}-{encoded_nick}"
        expected = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()[:16]
        if len(signature) != 16 or _encode(signature) != encoded_sig or not hmac.compare_digest(signature, expected):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error, IndexError) as exc:
        raise ValueError("token 過期或無效") from exc
    current = now or datetime.now(timezone.utc)
    if current.astimezone(TAIPEI) >= expiry + timedelta(seconds=1):
        raise ValueError("token 過期或無效")
    return nickname
