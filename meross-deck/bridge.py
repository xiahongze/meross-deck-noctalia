#!/usr/bin/env python3
"""Adapt the shared Meross helper to Noctalia with an isolated cache."""
import base64
import fcntl
import json
import os
import shutil
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


def main(argv):
    os.umask(0o077)
    if not argv:
        return meross_deck.main(argv)
    original, folder = isolated_cache()
    lock = os.path.join(folder, ".lock")
    with open(lock, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        seed_cache(original, folder)
        if argv[0] == "set-json":
            try:
                argv = ["set", checked_request(argv[1]), *argv[2:]]
            except (ValueError, IndexError, TypeError) as error:
                print(json.dumps({"ok": False, "kind": "usage", "error": str(error)}))
                return 1
        return meross_deck.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
