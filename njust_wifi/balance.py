"""Read-only campus balance; no credentials, account data or replies are persisted."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import urllib.request

from .network import PORTAL, automatic_opener
from .transport import DirectPathUnavailable


class BalanceUnavailable(RuntimeError):
    pass


def parse_balance(reply: dict) -> float:
    try:
        if not isinstance(reply, dict) or reply.get("reply_code") != 0:
            raise ValueError()
        rows = reply["results"]["rows"]
        if not isinstance(rows, list) or not rows or len(rows) > 100:
            raise ValueError()
        total = Decimal(0)
        for row in rows:
            value = row["balance"]
            if type(value) not in (int, float) or not Decimal(str(value)).is_finite():
                raise ValueError()
            total += Decimal(str(value))
        if abs(total) > Decimal("1000000000000"):
            raise ValueError()
        # portal.js sums account rows and divides by 1000 (login uses another unit).
        return float(total / 1000)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise BalanceUnavailable("余额暂不可用") from None


class BalanceClient:
    def __init__(self):
        self.opener = automatic_opener()

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(PORTAL + path, headers={
            "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
            "Referer": PORTAL + "/portal/index.html", "Cache-Control": "no-cache",
            "User-Agent": "NJUSTWiFi/2"})
        try:
            with self.opener.open(request, timeout=4) as response:
                data = response.read(65537)
            if len(data) > 65536:
                raise ValueError()
            reply = json.loads(data)
            if not isinstance(reply, dict) or reply.get("reply_code") != 0:
                raise ValueError()
            return reply
        except (OSError, ValueError, DirectPathUnavailable):
            raise BalanceUnavailable("请连接校园网后刷新") from None

    def _verify_account(self, username: str):
        result = self._get("/api/selfservice/v1/userinfo").get("results")
        if not isinstance(result, dict) or not username or result.get("username") != username:
            raise BalanceUnavailable("在线账号与配置不一致")

    def fetch(self, username: str) -> float:
        # IP-bound campus session: verify identity before and after the read.
        self._verify_account(username)
        balance = parse_balance(self._get("/api/selfservice/v1/account"))
        self._verify_account(username)
        return balance
