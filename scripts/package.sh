#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
tar --exclude="__pycache__" --exclude="*.pyc" -czf dist/meross-deck-noctalia.tar.gz catalog.toml meross-deck LICENSE
printf "%s\n" dist/meross-deck-noctalia.tar.gz
