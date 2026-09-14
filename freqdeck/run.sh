#!/bin/sh
# Run deck.py stages in sequence, detached, with a log — the shape every multi-hour leg
# of this pipeline runs in (`nohup … &` in a subshell reparents to launchd, PPID 1, so
# the run outlives the shell that started it).
#
#   freqdeck/run.sh 1000 sentences check translate tts apkg     # foreground
#   (nohup freqdeck/run.sh 1000 wordaudio > freqdeck/out/logs/wordaudio.log 2>&1 &)
#   LIST=yt freqdeck/run.sh 1000 sentences check translate tts   # a different source list
#
# Every stage skips what already exists, so a re-run after a failure costs nothing.
# COMMONS_PACE (seconds between Commons downloads, default 10) and LIST pass through.
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
N=${1:?words}; shift
mkdir -p freqdeck/out/logs
for stage in "$@"; do
  echo "== $stage $(date '+%H:%M:%S')"
  uv run --quiet --with genanki --with simplemma python freqdeck/deck.py "$stage" --n "$N" --list "${LIST:-words}" || { echo "FAILED at $stage"; exit 1; }
done
echo "== done $(date '+%H:%M:%S')"
