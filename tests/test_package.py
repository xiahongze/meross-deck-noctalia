import pathlib
import subprocess
import tarfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class PackageTests(unittest.TestCase):
    def test_archive_includes_runtime_and_excludes_local_state(self):
        subprocess.run([str(ROOT / "scripts/package.sh")], cwd=ROOT, check=True, capture_output=True)
        with tarfile.open(ROOT / "dist/meross-deck-noctalia.tar.gz", "r:gz") as archive:
            names = archive.getnames()
        for name in ("catalog.toml", "meross-deck/plugin.toml", "meross-deck/bridge.py", "meross-deck/src/meross_deck.py"):
            self.assertIn(name, names)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") or name.endswith("session.json") or name.endswith("devices.json") for name in names))

if __name__ == "__main__":
    unittest.main()
