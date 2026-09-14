#!/bin/bash
# Symlink every add-on in this folder into Anki's addons21 directory.
#
# Each subdirectory here is one Anki add-on package; this creates
#   ~/Library/Application Support/Anki2/addons21/<name>  ->  gigaku/anki/<name>
# as a directory symlink. Anki follows it, and writes each add-on's runtime meta.json back
# into the source dir — which .gitignore keeps out of the repo. Idempotent: re-run any time.
set -eu

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDONS="$HOME/Library/Application Support/Anki2/addons21"

if [ ! -d "$ADDONS" ]; then
  echo "Anki addons dir not found: $ADDONS" >&2
  exit 1
fi

# Prune first: a symlink pointing into this folder whose target is gone is an add-on that
# was deleted from the repo — left in place, Anki trips over it at startup.
for link in "$ADDONS"/*; do
  [ -L "$link" ] || continue
  case "$(readlink "$link")" in
    "$SRC"/*) [ -e "$link" ] || { rm "$link"; echo "prune  $(basename "$link")"; } ;;
  esac
done

for dir in "$SRC"/*/; do
  name="$(basename "$dir")"
  target="$ADDONS/$name"
  src="${dir%/}"

  if [ -L "$target" ]; then
    if [ "$(readlink "$target")" = "$src" ]; then
      echo "ok    $name (already linked)"
    else
      rm "$target"; ln -s "$src" "$target"; echo "relink $name"
    fi
  elif [ -e "$target" ]; then
    # A real directory (e.g. a previous non-symlink install). Don't destroy it blindly —
    # preserve its meta.json into the source, then replace with a symlink.
    if [ -f "$target/meta.json" ]; then cp "$target/meta.json" "$src/meta.json"; fi
    rm -rf "$target"; ln -s "$src" "$target"; echo "migrate $name (kept meta.json)"
  else
    ln -s "$src" "$target"; echo "link   $name"
  fi
done

echo "done. Restart Anki to (re)load add-ons."
