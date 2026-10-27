import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "meross-deck"
spec = importlib.util.spec_from_file_location("bridge", PLUGIN / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_checked_commands_reject_unsafe_values(self):
        request = {"uuid": "unit", "kind": "door", "channel": 0, "value": True, "expires": int(time.time()) + 6}
        self.assertEqual(json.loads(base64.b64decode(bridge.checked_request(json.dumps(request)))), request)
        for field, bad in (("kind", "garage"), ("channel", -1), ("channel", True), ("value", "false"), ("uuid", "")):
            item = dict(request, **{field: bad})
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                bridge.checked_request(json.dumps(item))
        with self.assertRaises(ValueError):
            bridge.checked_request(json.dumps(dict(request, expires=int(time.time()) - 1)))
        for expiry in (float("nan"), float("inf"), int(time.time()) + 60):
            with self.assertRaises(ValueError):
                bridge.checked_request(json.dumps(dict(request, expires=expiry)))
        with self.assertRaises(ValueError):
            bridge.checked_request(json.dumps({"uuid": "unit", "kind": "switch", "channel": 0, "value": True, "expires": int(time.time()) - 1}))
        for value in (0, 101, 1.1, True):
            with self.assertRaises(ValueError):
                bridge.checked_request(json.dumps({"uuid": "unit", "kind": "luminance", "channel": 0, "value": value}))

    def test_distinct_mqtt_client_id_is_persistent_and_private(self):
        with tempfile.TemporaryDirectory() as folder:
            first = bridge.client_id(folder)
            self.assertEqual(len(first), 32)
            self.assertEqual(bridge.client_id(folder), first)
            self.assertEqual((pathlib.Path(folder) / "mqtt-client-id").stat().st_mode & 0o777, 0o600)

    def test_reuses_session_without_copying_and_seeds_isolated_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = root / "config"
            cache = root / "cache"
            (config / "meross-deck").mkdir(parents=True)
            (cache / "meross-deck").mkdir(parents=True)
            session_path = config / "meross-deck" / "session.json"
            session_path.write_text(json.dumps({"token": "secret-token", "key": "secret-key", "email": "fixture@example.test"}))
            original = cache / "meross-deck" / "devices.json"
            original.write_text(json.dumps({"devices": {}, "listed": {}, "order": [], "listedAt": 0}))
            env = dict(os.environ, XDG_CONFIG_HOME=str(config), XDG_CACHE_HOME=str(cache))
            result = subprocess.run([sys.executable, str(PLUGIN / "bridge.py"), "cached"], env=env, capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(result.stdout)["ok"])
            self.assertNotIn("secret-token", result.stdout)
            self.assertNotIn("secret-key", result.stdout)
            isolated = cache / "meross-deck-noctalia" / "meross-deck" / "devices.json"
            self.assertEqual(json.loads(isolated.read_text()), json.loads(original.read_text()))
            self.assertEqual(isolated.stat().st_mode & 0o777, 0o600)
            self.assertEqual((cache / "meross-deck-noctalia" / "meross-deck").stat().st_mode & 0o777, 0o700)
            self.assertFalse((config / "meross-deck-noctalia" / "session.json").exists())
            isolated.write_text(json.dumps({"devices": {}, "listed": {}, "order": [], "listedAt": 7}))
            subprocess.run([sys.executable, str(PLUGIN / "bridge.py"), "cached"], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(isolated.read_text())["listedAt"], 7)
            self.assertEqual(json.loads(original.read_text())["listedAt"], 0)


if __name__ == "__main__":
    unittest.main()
