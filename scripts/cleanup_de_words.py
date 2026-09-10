"""One-off: purge the German words the export→import loop fabricated (2026-08-06..08).

The de known-morphs CSV exported spaCy lemmas with their capitals while the import lowered
what AnkiMorphs read back, so every word whose lemma differs from itself returned as a
brand-new "word" at the year 1 — 594 of them in three days (alarmsyst, elefan, aborigin…),
some through multi-cycle chains. The loop is closed in code now (words._de_exported_forms);
this script removes what it left behind:

  * de words at UNKNOWN_DATE absent from Migaku's full dump (any status) — fabricated;
  * the junk DE_LEMMA_CACHE values spaCy answered with punctuation (akt → "---") — fixed
    to identity so the next CSV write exports the word, not the punctuation;
  * the CSV itself, regenerated clean.

DE_LEMMA_CACHE keys are deliberately NOT pruned: the cache is the record of what was ever
exported, and the import-side echo filter needs it for the window until the next recalc,
while the Morphs db still holds the retired lemmas.

Supervised procedure (in this order):
  1. gigaku backup                      # pre-cleanup commit + today's migaku/de.csv
  2. uv run python scripts/cleanup_de_words.py [--dry-run]
  3. press R in Anki                    # recalc drops the fabricated lemmas
  4. gigaku backup                      # post-cleanup commit (gate: ~2% words, ~8% CSV)

Rollback: cp ~/Library/Caches/gigaku/words.json.pre-cleanup ~/Library/Caches/gigaku/words.json
(and the same for de_lemmas.json), or restore from the vocab-backup commit of step 1.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import config
from lib.config import UserError, settings
from lib.vocab import words as words_mod
from lib.vocab.words import UNKNOWN_DATE


def _migaku_forms(backup_dir):
    """Every dictForm Migaku has ever met (any status), lowered — the truth set.

    The full dump, not just KNOWN: a word Migaku holds at any status is a real word the
    user once met, and this script only claims to remove what Migaku has never seen."""
    path = os.path.join(backup_dir, "migaku", "de.csv")
    manifest = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(path):
        raise UserError(f"no Migaku dump at {path} — run `gigaku backup` first")
    with open(manifest, encoding="utf-8") as f:
        stamped = json.load(f).get("date")
    if stamped != date.today().isoformat():
        raise UserError(
            f"the backup is from {stamped}, not today — run `gigaku backup` first, "
            f"so the truth set and the rollback point are both this morning's"
        )
    dirty = subprocess.run(["git", "-C", backup_dir, "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        raise UserError(f"{backup_dir} has uncommitted changes — commit or clean it first")
    with open(path, encoding="utf-8", newline="") as f:
        return {row["dictForm"].lower() for row in csv.DictReader(f)}


def main(dry_run):
    forms = _migaku_forms(settings.BACKUP_DIR)
    print(f"Migaku truth set: {len(forms):,} de forms (all statuses).")

    cached = words_mod.read_cache()
    doomed = [w for w in cached
              if w.language == "de" and w.date == UNKNOWN_DATE
              and w.word.lower() not in forms]
    kept = [w for w in cached if w not in doomed]
    de_total = sum(1 for w in cached if w.language == "de")
    print(f"words.json: {len(cached):,} total, {de_total:,} de — removing "
          f"{len(doomed):,} fabricated (UNKNOWN_DATE, unknown to Migaku).")
    print("  sample:", ", ".join(sorted(w.word for w in doomed)[:10]))

    with open(config.DE_LEMMA_CACHE, encoding="utf-8") as f:
        lemmas = json.load(f)
    junk = {w: lem for w, lem in lemmas.items()
            if not any(ch.isalpha() for ch in lem)}
    print(f"{config.DE_LEMMA_CACHE}: fixing {len(junk)} junk value(s) to identity "
          f"({', '.join(sorted(junk))}).")

    if dry_run:
        print("Dry run — nothing written.")
        return

    for path in (config.WORDS_CACHE, config.DE_LEMMA_CACHE):
        shutil.copyfile(path, path + ".pre-cleanup")
        print(f"Saved {path}.pre-cleanup")

    lemmas.update({w: w for w in junk})
    tmp = config.DE_LEMMA_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lemmas, f, ensure_ascii=False)
    os.replace(tmp, config.DE_LEMMA_CACHE)

    words_mod.write_cache(kept)
    words_mod.save_known_morphs("de", kept)

    new_de = sum(1 for w in kept if w.language == "de")
    print(f"Done: de {de_total:,} → {new_de:,}. Now press R in Anki, then `gigaku backup`.")


if __name__ == "__main__":
    try:
        main(dry_run="--dry-run" in sys.argv)
    except UserError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        sys.exit(1)
