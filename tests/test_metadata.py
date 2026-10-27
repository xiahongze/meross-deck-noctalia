from pathlib import Path
import json
import tomllib

root = Path(__file__).resolve().parents[1]
manifest = tomllib.loads((root / "meross-deck/plugin.toml").read_text())
catalog = tomllib.loads((root / "catalog.toml").read_text())["plugin"][0]
for key in ("id", "name", "version", "plugin_api"):
    assert manifest[key] == catalog[key], key
assert manifest["panel"][0]["placement"] == "attached"
assert manifest["panel"][0]["open_near_click"] is True
for kind in ("service", "widget", "desktop_widget", "panel"):
    for entry in manifest.get(kind, []):
        assert (root / "meross-deck" / entry["entry"]).is_file(), entry
json.loads((root / "meross-deck/translations/en.json").read_text())
print("manifest and catalog: ok")
