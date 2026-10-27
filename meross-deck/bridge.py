#!/usr/bin/env python3
"""Adapt the shared Meross helper to Noctalia with an isolated cache."""
import base64
import fcntl
import json
import math
import os
import secrets
import shutil
import signal
import time
import sys
import tempfile

from src import meross_deck


def checked_request(raw):
    request = json.loads(raw)
    if not isinstance(request, dict) or not isinstance(request.get("uuid"), str) or not request["uuid"]:
        raise ValueError("Device identifier is missing")
    kind, channel, value = request.get("kind"), request.get("channel"), request.get("value")
    if kind not in ("door", "switch", "luminance", "temperature"):
        raise ValueError("Unsupported command")
    if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255:
        raise ValueError("Invalid channel")
    expiry = request.get("expires")
    if kind == "door" or expiry is not None:
        max_wait = 6 if kind == "door" else 15
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or not int(time.time()) <= expiry <= int(time.time()) + max_wait:
            raise ValueError("Command expired; check the device state and try again")
    if kind in ("door", "switch") and not isinstance(value, bool):
        raise ValueError("Expected an on/off value")
    if kind in ("luminance", "temperature") and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100):
        raise ValueError("Level must be an integer from 1 to 100")
    return base64.b64encode(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()).decode()


def isolated_cache():
    original = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    os.environ["XDG_CACHE_HOME"] = os.path.join(original, "meross-deck-noctalia")
    folder = meross_deck.cache_dir()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return original, folder


def seed_cache(original, folder):
    source = os.path.join(original, "meross-deck", "devices.json")
    dest = os.path.join(folder, "devices.json")
    if os.path.exists(dest) or not os.path.isfile(source):
        return
    fd, tmp = tempfile.mkstemp(prefix=".seed-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def client_id(folder):
    path = os.path.join(folder, "mqtt-client-id")
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            return value
    except (OSError, UnicodeError):
        pass
    value = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(value)
    return value


def main(argv):
    os.umask(0o077)
    if not argv:
        return meross_deck.main(argv)
    original, folder = isolated_cache()
    lock = os.path.join(folder, ".lock")
    with open(lock, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        seed_cache(original, folder)
        # Share the saved session, but never the MQTT client id: a second
        # client using Plasma’s id would disconnect its existing connection.
        old_load_session = meross_deck.load_session
        def load_noctalia_session():
            session = dict(old_load_session())
            session["appId"] = client_id(folder)
            return session
        meross_deck.load_session = load_noctalia_session
        if argv[0] == "set-json":
            try:
                argv = ["set", checked_request(argv[1]), *argv[2:]]
            except (ValueError, IndexError, TypeError) as error:
                print(json.dumps({"ok": False, "kind": "usage", "error": str(error)}))
                return 1
        return meross_deck.main(argv)


def _timeout(_signum, _frame):
    raise TimeoutError("Meross request timed out; check device state before retrying")


if __name__ == "__main__":
    # Noctalia streams this single JSON result; prevent a stalled request from
    # holding the shared cache lock indefinitely. Interactive login has no timer.
    if len(sys.argv) > 1 and sys.argv[1] != "login":
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(75)
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as error:
        print(json.dumps({"ok": False, "kind": "network", "error": str(error) or "Meross helper failed"}), flush=True)
        sys.exit(1)
