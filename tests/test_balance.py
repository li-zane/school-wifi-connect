import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from njust_wifi.balance import BalanceClient, BalanceUnavailable, parse_balance
from njust_wifi.service import Service
from njust_wifi.storage import Store


class BalanceTests(unittest.TestCase):
    def test_units_aggregate_zero_and_negative(self):
        for rows, expected in (([12345, 655], 13), ([0, 0], 0), ([-1250], -1.25)):
            self.assertEqual(parse_balance({"reply_code": 0, "results": {"rows": [{"balance": x} for x in rows]}}), expected)

    def test_missing_or_invalid_never_becomes_zero(self):
        for reply in ({}, {"reply_code": 1, "reply_msg": "private"},
                      {"reply_code": 0, "results": {"rows": []}},
                      *({"reply_code": 0, "results": {"rows": [{"balance": value}]}}
                        for value in (None, True, "100", float("nan"), float("inf")))):
            with self.subTest(reply=reply), self.assertRaises(BalanceUnavailable) as caught:
                parse_balance(reply)
            self.assertNotIn("private", str(caught.exception))

    def test_read_only_protocol_checks_account_twice(self):
        client = BalanceClient()
        replies = [{"reply_code": 0, "results": {"username": "fixture"}},
                   {"reply_code": 0, "results": {"rows": [{"balance": 12000}]}},
                   {"reply_code": 0, "results": {"username": "fixture"}}]
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 4)
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps(replies.pop(0)).encode()
            return response
        client.opener.open = open_request
        self.assertEqual(client.fetch("fixture"), 12)
        self.assertTrue(all(r.get_method() == "GET" and r.data is None for r in requests))
        self.assertEqual(len(requests), 3)

    def test_wrong_account_and_network_change_reject_value(self):
        for replies in ([{"results": {"username": "other"}}],
                        [{"results": {"username": "fixture"}},
                         {"reply_code": 0, "results": {"rows": [{"balance": 12000}]}},
                         {"results": {"username": "other"}}]):
            client = BalanceClient()
            client._get = Mock(side_effect=replies)
            with self.assertRaises(BalanceUnavailable):
                client.fetch("fixture")

    def service(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name))
        store.update({"username": "fixture", "password": "fixture-secret"})
        client = Mock()
        service = Service(store, balance_factory=lambda: client)
        self.addCleanup(service.close)
        return service, client

    def test_cache_deduplicates_refresh_and_never_logs_balance(self):
        service, client = self.service()
        entered, release = threading.Event(), threading.Event()
        def fetch(username):
            entered.set()
            release.wait(3)
            return 123.45
        client.fetch.side_effect = fetch
        self.assertIsNone(service.balance()["balance"])
        client.fetch.assert_not_called()
        service.refresh_balance()
        self.assertTrue(entered.wait(1))
        for _ in range(5):
            self.assertEqual(service.balance()["state"], "loading")
            service.refresh_balance()
        release.set()
        service.balance_worker.join(2)
        self.assertEqual(client.fetch.call_count, 1)
        self.assertEqual(service.balance()["balance"], 123.45)
        service.refresh_balance(automatic=True)
        self.assertEqual(client.fetch.call_count, 1)
        self.assertNotIn("123.45", json.dumps(service.diagnostics()))
        self.assertNotIn("123.45", (service.store.root / "events.jsonl").read_text('utf-8'))
        snapshot = service.balance()
        snapshot["balance"] = -1
        self.assertEqual(service.balance()["balance"], 123.45)

    def test_account_change_discards_inflight_and_forget_clears_cache(self):
        service, client = self.service()
        release = threading.Event()
        client.fetch.side_effect = lambda _: (release.wait(3), 123.45)[1]
        service.refresh_balance()
        service.configure({"username": "different", "password": "different-secret"})
        release.set()
        service.balance_worker.join(2)
        self.assertIsNone(service.balance()["balance"])
        client.fetch.side_effect = None
        client.fetch.return_value = 0
        service.refresh_balance()
        service.balance_worker.join(2)
        self.assertEqual(service.balance()["balance"], 0)
        service.forget()
        self.assertIsNone(service.balance()["balance"])

    def test_upstream_error_does_not_leak_response(self):
        service, client = self.service()
        client.fetch.side_effect = ValueError("fixture-secret raw reply")
        service.refresh_balance()
        service.balance_worker.join(2)
        self.assertEqual(service.balance()["message"], "余额暂不可用")
        self.assertIsNone(service.balance()["balance"])


if __name__ == "__main__":
    unittest.main()
