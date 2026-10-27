.PHONY: test lint package install

test:
	PYTHONPATH=meross-deck python3 -m unittest discover -s tests -v
	lua tests/test_service.lua
	lua tests/test_panel.lua
	python3 tests/test_metadata.py
lint:
	python3 -m compileall -q meross-deck tests
	@if command -v noctalia >/dev/null; then noctalia plugins lint meross-deck; else echo 'Noctalia unavailable; skipping plugin lint'; fi
	@set -e; for f in meross-deck/*.luau; do luac -p "$$f"; done
package:
	./scripts/package.sh
install:
	./scripts/install.sh
