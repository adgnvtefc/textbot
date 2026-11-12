"""textbot: an iMessage bot answered by a local Qwen model.

Usage: python3 bot.py +15551234567 [more phone numbers or emails...]

    GET http://127.0.0.1:8766/health   "ok" while the loop is running, for monitor-hub's Services
Each reply is reported to catseye as a "reply" event: whether it worked and how long it took
(never who, never what was said).
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import imessage

ENV_FILE = Path(__file__).resolve().parent.parent / "harness" / ".env"
MODEL = "Qwen3.8-27B-MLX-8bit"
POLL_SECONDS = 2
HISTORY = 20                     # how many recent messages the model sees
HEALTH_PORT = 8766
STALE_SECONDS = 360              # healthy if the loop ran this recently (a model call can take 300 s)
CATSEYE = "http://127.0.0.1:8790/v1/e"
last_tick = time.monotonic()     # when the loop last went round; /health reads it

# Your own prompt goes in system_prompt.txt next to this file (gitignored, so it stays private).
PROMPT_FILE = Path(__file__).resolve().parent / "system_prompt.txt"
DEFAULT_PROMPT = (
    "You are textbot, a friendly assistant people talk to over iMessage. "
    "Be helpful, concise, and answer the relevant questions."
)
SYSTEM_PROMPT = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else DEFAULT_PROMPT


def load_env(path):
    """Read KEY=VALUE lines as plain text. Never run the file, never print values."""
    env = {}
    for line in path.read_text(errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            env[key.strip()] = value.strip().strip("'\"")
    return env


def history(db, handle, upto):
    """The conversation with `handle` as OpenAI-style messages, oldest first."""
    return [{"role": "assistant" if from_me else "user", "content": text}
            for from_me, text in imessage.conversation(db, handle, upto, HISTORY)]


def ask_model(messages, base_url, api_key):
    """One chat completion. No `tools` are sent, so the model can only answer with text."""
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps({"model": MODEL, "messages": messages}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        choice = json.load(resp)["choices"][0]
    if choice.get("finish_reason") == "length":
        print("warning: reply was cut off (finish_reason=length)")
    return (choice["message"].get("content") or "").strip()


def report(name, data):
    """Send one event to catseye, in the background: catseye being slow or down never affects a reply."""
    body = json.dumps({"site": "textbot", "name": name, "data": data}).encode()

    def post():
        try:
            urllib.request.urlopen(urllib.request.Request(CATSEYE, data=body), timeout=5).read()
        except Exception as e:
            print(f"catseye report failed: {e}")

    threading.Thread(target=post, daemon=True).start()


class Health(BaseHTTPRequestHandler):
    """Answers 200 only while the loop is alive, so a stuck bot shows as down, not just a dead one."""
    def do_GET(self):
        if self.path != "/health":
            status = 404
        else:
            status = 200 if time.monotonic() - last_tick < STALE_SECONDS else 503
        body = b"ok" if status == 200 else b"stuck" if status == 503 else b"not found"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass                        # the hub asks every minute; keep the terminal for messages


def start_health(port=HEALTH_PORT):
    """Serve /health on localhost in a background thread. Fails if the port is taken,
    which also stops a second copy of the bot from running and answering twice."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def answer(db, handle, rowid, base_url, api_key):
    """Ask the model and send its reply. Reports how it went to catseye, then re-raises any error."""
    started = time.monotonic()
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history(db, handle, rowid)
        reply = ask_model(messages, base_url, api_key)
        print(f"{handle}: {len(messages) - 1} msgs in, {len(reply)} chars out")
        if not reply:
            raise ValueError("the model returned an empty reply")
        imessage.send(handle, reply)
    except Exception as e:
        # Only the error's type: messages can hold the contact's number (osascript's often do).
        report("reply", {"ok": False, "seconds": round(time.monotonic() - started, 1), "error": type(e).__name__})
        raise
    report("reply", {"ok": True, "seconds": round(time.monotonic() - started, 1)})


def main():
    global last_tick
    allowed = set(sys.argv[1:])
    if not allowed:
        sys.exit("usage: python3 bot.py <phone-or-email> [...]")
    env = load_env(ENV_FILE)
    api_key = os.environ.get("OMLX_API_KEY") or env.get("OMLX_API_KEY")
    base_url = os.environ.get("OMLX_BASE_URL") or env.get("OMLX_BASE_URL") or "http://127.0.0.1:8000/v1"
    if not api_key:
        sys.exit(f"no OMLX_API_KEY in the environment or {ENV_FILE}")

    db = imessage.connect()
    last = imessage.latest_id(db)
    try:
        start_health()
    except OSError:
        sys.exit(f"port {HEALTH_PORT} is taken: is another bot.py already running?")
    print(f"watching from message {last}; answering {sorted(allowed)}; health on 127.0.0.1:{HEALTH_PORT}")
    while True:
        last_tick = time.monotonic()
        for rowid, handle, text in imessage.new_messages(db, last):
            last = rowid                    # advance even when we skip, so nothing repeats
            if handle not in allowed or not text:
                continue
            try:
                answer(db, handle, rowid, base_url, api_key)
            except Exception as e:          # log and skip; one bad message shouldn't kill the bot
                print(f"error on message {rowid}: {getattr(e, 'stderr', '') or e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
