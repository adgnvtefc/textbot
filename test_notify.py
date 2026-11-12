"""Tests for notify.py. Run: python3 -m unittest test_notify

Nothing here touches Messages, catseye, or the real contacts.json: each test gets a fake send,
a fake report, throwaway tokens, and its own temp contacts file.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import imessage
import notify

JEWELS, OTHER = "test-token-jewels", "test-token-other"


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sent, self.reported = [], []
        self.real = imessage.send, notify.report, notify.CONTACTS
        imessage.send = lambda handle, text: self.sent.append((handle, text))
        notify.report = lambda app, name, result: self.reported.append((app, name, result))
        notify.CONTACTS = self.dir / "contacts.json"
        notify.CONTACTS.write_text(json.dumps({"jewels": {"alice": "+15550001", "carol": "+15550003"},
                                               "other": {"bob": "+15550002"}}))
        self.server = notify.make_server(0, {JEWELS: "jewels", OTHER: "other"})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        imessage.send, notify.report, notify.CONTACTS = self.real

    def post(self, body, token=JEWELS, path="/notify", raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, headers={"Authorization": f"Bearer {token}"})
        return self.open(req)

    def open(self, req):
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_sends_to_the_named_contact(self):
        self.assertEqual(self.post({"to": "Alice ", "text": "bob sent you a message"})[0], 200)
        self.assertEqual(self.sent, [("+15550001", "bob sent you a message")])

    def test_rejects_bad_requests_without_sending(self):
        cases = [
            ("wrong token", self.post({"to": "alice", "text": "x"}, token="nope"), 401),
            ("non-ascii token", self.post({"to": "alice", "text": "x"}, token="ü"), 401),
            ("other app's contact", self.post({"to": "bob", "text": "x"}), 404),
            ("raw phone number", self.post({"to": "+15550002", "text": "x"}), 404),
            ("text too long", self.post({"to": "alice", "text": "x" * 301}), 400),
            ("control chars only", self.post({"to": "alice", "text": "\x00\x07"}), 400),
            ("not JSON", self.post(None, raw=b"hello"), 400),
            ("JSON list", self.post(["alice"]), 400),
            ("wrong path", self.post({"to": "alice", "text": "x"}, path="/x"), 404),
        ]
        for name, (status, _), want in cases:
            self.assertEqual(status, want, name)
        self.assertEqual(self.sent, [])

    def test_rate_limit_holds_even_when_requests_arrive_together(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.post({"to": "alice", "text": "hi"})[0]))
                   for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 429, 429, 429, 429])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.post({"to": "carol", "text": "hi"})[0], 200)   # the limit is per person

    def test_health(self):
        self.assertEqual(self.open(urllib.request.Request(self.url + "/health")), (200, "ok"))

    def test_every_attempt_is_reported_without_numbers_or_text(self):
        self.post({"to": "alice", "text": "secret words"})
        self.post({"to": "alice", "text": "again"})
        self.post({"to": "x", "text": "x"}, token="nope")
        self.post({"to": "+15550002", "text": "x"})
        self.assertEqual(self.reported, [("jewels", "alice", "sent"), ("jewels", "alice", "limited"),
                                         (None, None, "bad token"), ("jewels", "+15550002", "unknown contact")])
        # The only number that shows up is one the caller typed itself, never one from contacts.json.
        self.assertNotIn("+15550001", repr(self.reported))
        self.assertNotIn("secret", repr(self.reported))


if __name__ == "__main__":
    unittest.main()
