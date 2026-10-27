#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v noctalia >/dev/null || { echo "Noctalia 5 is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "Python 3.11 or newer is required" >&2; exit 1; }
python3 -c "import sys; assert sys.version_info >= (3, 11)" || { echo "Python 3.11 or newer is required" >&2; exit 1; }
noctalia msg status >/dev/null || { echo "Start Noctalia first" >&2; exit 1; }
source_list="$(noctalia msg plugins source list)"
found=0
while IFS= read -r source_line; do
  case "$source_line" in
    "meross-deck-local path $repo_dir") found=1 ;;
    "meross-deck-local "*) echo "The meross-deck-local source already points elsewhere; choose a different source name." >&2; exit 1 ;;
  esac
done <<< "$source_list"
if [[ "$found" = 0 ]]; then noctalia msg plugins source add meross-deck-local path "$repo_dir"; fi
noctalia msg plugins enable xiahongze/meross-deck
printf "Meross Deck enabled. Add its bar widget from Noctalia settings.\n"
