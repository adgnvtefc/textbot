"""textbot notify: a tiny local service that other apps call to text someone.

    POST http://127.0.0.1:8765/notify
    Authorization: Bearer <that app's token from .env>
    {"to": "alice", "text": "bob sent you a message in Pixel Game"}

    GET  /health    "ok", for monitor-hub's Services list

An app can only text names listed under it in contacts.json, never raw phone numbers.
Each attempt is reported to catseye as a "notify" event (which app, which name, the result;
never numbers or text), and monitor-hub shows them like any site's events.
Run: python3 notify.py
"""
import hmac
import json
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import imessage

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
CONTACTS = HERE / "contacts.json"
CATSEYE = "http://127.0.0.1:8790/v1/e"
HOST, PORT = "127.0.0.1", 8765      # localhost only: nothing off this Mac can reach it
MAX_BODY = 4096                     # bytes of JSON we're willing to read
MAX_TEXT = 300                      # characters per notification
EVERY_SECONDS = 300                 # at most one text per person per 5 minutes
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")   # control characters, except line breaks
RESULTS = {200: "sent", 429: "limited", 502: "failed", 401: "bad token", 404: "unknown contact", 400: "bad request"}


def load_env(path):
    """Read KEY=VALUE lines as plain text. Never run the file, never print values."""
    env = {}
    for line in path.read_text(errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            env[key.strip()] = value.strip().strip("'\"")
    return env


def tokens_from(env):
    """NOTIFY_TOKEN_JEWELS=abc... becomes {"abc...": "jewels"}: the token says which app is calling."""
    prefix = "NOTIFY_TOKEN_"
    return {value: key[len(prefix):].lower() for key, value in env.items() if key.startswith(prefix) and value}


def report(app, name, result):
    """Send one attempt to catseye, in the background: catseye being slow or down never affects a notification."""
    body = json.dumps({"site": "textbot", "name": "notify", "data": {"app": app, "to": name, "result": result}}).encode()

    def post():
        try:
            urllib.request.urlopen(urllib.request.Request(CATSEYE, data=body), timeout=5).read()
        except Exception as e:
            print(f"catseye report failed: {e}")

    threading.Thread(target=post, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, "ok", "text/plain")
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        self.app = self.name = None         # filled in by notify() as far as it gets
        status, body = self.notify()
        if self.path == "/notify":
            report(self.app, self.name, RESULTS.get(status, str(status)))
        self.reply(status, body)

    def reply(self, status, body, content_type="application/json"):
        data = (json.dumps(body) if content_type == "application/json" else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def notify(self):
        """Check everything, then send. Returns (HTTP status, JSON body)."""
        if self.path != "/notify":
            return 404, {"error": "not found"}
        self.app = self.calling_app()
        if not self.app:
            return 401, {"error": "bad token"}
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_BODY:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            name = str(data["to"]).strip().lower()
            text = CONTROL.sub("", str(data["text"])).strip()
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "send JSON {\"to\": name, \"text\": message}"}
        self.name = name[:64]               # what gets reported; long junk names are cut short
        if not 0 < len(text) <= MAX_TEXT:
            return 400, {"error": f"text must be 1 to {MAX_TEXT} characters"}

        # Read contacts on every request, so edits take effect without a restart.
        handle = json.loads(CONTACTS.read_text()).get(self.app, {}).get(name)
        if not handle:
            return 404, {"error": f"no contact {name!r} for {self.app}"}
        key = (self.app, name)
        # One send at a time from here on, so two requests at once can't both slip past the rate limit.
        with self.server.send_lock:
            if time.monotonic() - self.server.last_sent.get(key, -EVERY_SECONDS) < EVERY_SECONDS:
                return 429, {"error": "already notified recently"}
            try:
                imessage.send(handle, text)
            except Exception as e:
                print(f"send to {self.app}/{name} failed: {getattr(e, 'stderr', '') or e}")
                return 502, {"error": "sending failed"}
            self.server.last_sent[key] = time.monotonic()
        print(f"sent {self.app}/{name} {len(text)} chars")
        return 200, {"ok": True}

    def calling_app(self):
        """The app whose token is in the Authorization header, or None."""
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        for known, app in self.server.tokens.items():
            # compare_digest takes the same time whether the first or last character differs,
            # so timing can't be used to guess a token one character at a time.
            if token and hmac.compare_digest(token.encode(), known.encode()):
                return app
        return None


def make_server(port, tokens):
    # Threading, so /health still answers while a send waits on Messages (up to 30 s).
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.tokens, server.last_sent = tokens, {}
    server.send_lock = threading.Lock()
    return server


def main():
    tokens = tokens_from(load_env(ENV_FILE)) if ENV_FILE.exists() else {}
    if not tokens:
        sys.exit(f"no NOTIFY_TOKEN_<APP>=... lines in {ENV_FILE}")
    server = make_server(PORT, tokens)
    print(f"notify listening on http://{HOST}:{PORT}/notify for {sorted(set(tokens.values()))}")
    server.serve_forever()


if __name__ == "__main__":
    main()
