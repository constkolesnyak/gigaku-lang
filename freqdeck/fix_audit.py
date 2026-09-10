#!/usr/bin/env python3
"""Apply the findings of audit.py to the deck — and to the caches the apkg is built from.

    uv run python freqdeck/fix_audit.py            # reads freqdeck/out/audit.tsv

Per category:
  DEF / SENSE  regenerate the English definition with the add-on's own prompt + model
               (definitions.py) plus one line naming the reviewer's objection, strip a
               leading «Wort → » / «Wort means», re-TTS it, replace both fields.
  RU           re-translate the sentence (Opus, the translate rubric + the objection),
               write Notes and translations.tsv.
  DE           rewrite the sentence minimally (Opus: the sentence rubric + the objection,
               keep the target and the situation), re-TTS Sentence Audio under the SAME
               file name, re-translate, write Sentence/Notes/chosen.tsv/translations.tsv.
Everything is written note by note; the caches are rewritten in place so a later
`apkg` + re-import does not bring the old text back.
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import deck  # noqa: E402
import definitions as defs  # noqa: E402
from lib.claude import ask  # noqa: E402

AUDIT = os.path.join(deck.OUT, "audit.tsv")


def rewrite_tsv(path, updates):
    rows = deck.read_tsv(path)
    out = [(r[0], updates.get(r[0], r[1])) for r in rows]
    with open(path + ".part", "w", encoding="utf-8") as f:
        for w, v in out:
            f.write(f"{w}\t{v}\n")
    os.replace(path + ".part", path)


def note_for(word):
    nids = deck.anki("findNotes", query=f'deck:"{deck.DECK}" Word:{word}')
    notes = deck.anki("notesInfo", notes=nids)
    return next(n for n in notes if n["fields"]["Word"]["value"].strip() == word)


def f(n, k):
    return n["fields"][k]["value"]


def new_definition(cfg, word, sentence, objection):
    user = cfg["prompt"].format(word=word, sentence=sentence, context="") + (
        f"\n\nA reviewer rejected the previous definition: {objection}. Define the sense the "
        f"sentence uses. Output only the gloss itself — never the German word, no arrow.")
    text = defs.chat(cfg, cfg["system"], user).strip().strip('"').strip()
    text = re.sub(r"^\W*" + re.escape(word) + r"\W*(→|means|is|:|-|—)\s*", "", text, flags=re.I).strip()
    return text[0].upper() + text[1:] if text else text


def set_definition(note, text, key):
    """Write one bilingual definition and its TTS, dropping the audio it replaces.

    Module-level on purpose: BOTH branches need it — a DEF/SENSE finding rewrites the
    definition directly, and a DE rewrite has to re-derive it because the definition is
    read out of the sentence. It lived inside main() once and the DE branch called a name
    that was not in scope, which crashed the whole run on its first rewritten sentence
    (2026-08-29) after the caches had already been updated.
    """
    data = deck.tts(text, key)
    stored = defs.store(f"mvj-bilingual-definition-{int(time.time() * 1000)}.mp3", data)
    old_files = re.findall(r"\[audio:(.+?)\]", f(note, "Definition Audio"))
    deck.anki("updateNoteFields", note={"id": note["noteId"], "fields": {
        "Definition": defs.definition_block(text), "Definition Audio": defs.audio_block(text, stored)}})
    for name in old_files:
        try:
            deck.anki("deleteMediaFile", filename=name)
        except Exception:  # noqa: BLE001 — an unremovable old file is not worth the fix
            pass


def main():
    findings = deck.read_tsv(AUDIT)
    cfg = defs.config()
    chosen_upd, trans_upd = {}, {}
    # DE first: a new sentence needs a new translation, and the definition may follow it.
    de = [(w, what) for w, cat, what in findings if cat == "DE"]
    ru = [(w, what) for w, cat, what in findings if cat == "RU"]
    dd = [(w, cat, what) for w, cat, what in findings if cat in ("DEF", "SENSE")]

    if de:
        text = "\n".join(f"{i}: {w} | {f(note_for(w), 'Sentence')} | REVIEWER: {what}"
                         for i, (w, what) in enumerate(de, 1))
        system = deck.SYSTEM + (
            "\n\n## This request\n\nEach line is `id: TARGET | sentence | REVIEWER: objection`. "
            "Rewrite the sentence with the smallest change that answers the objection, keeping "
            "the TARGET, the situation and the register. Answer `id: sentence`, one per line."
            "\n\n**Grammar outranks the form rule.** Some words simply cannot stand in their "
            "dictionary form in a sentence — an attributive-only adjective (`restlich`, "
            "`fehlend`, `damalig`), a bound compound element (`vorder-`), a word that lives in "
            "one fixed idiom (`ungut` in `nichts für ungut`). Forcing the dictionary form on "
            "those produces German no one says, which is what the reviewer caught. For such a "
            "word write the natural sentence in whatever form the language requires; for every "
            "other word the form rule still holds.")
        reply, _, _ = ask(text, system, model="opus", what="DE fixes", effort="high")
        for m in re.finditer(r"^\s*(\d+)\s*[:.]\s*(\S.*?)\s*$", reply or "", re.M):
            i = int(m.group(1))
            if 1 <= i <= len(de):
                w = de[i - 1][0]
                chosen_upd[w] = m.group(2)
                ru.append((w, "sentence was rewritten — translate the new one"))
                deck.note(f"  DE {w}: {m.group(2)}")
        for name in set(deck.LISTS):
            deck.use_list(name)
            rewrite_tsv(deck.CHOSEN, {w: v for w, v in chosen_upd.items()
                                      if deck.locate(w)[0] == name})
        deck.use_list("words")
        key = deck.openai_key()
        for w, s in chosen_upd.items():
            lst, rank = deck.locate(w)
            name = deck.media_name(rank, w, prefix=deck.LISTS[lst]["prefix"])
            data = deck.tts(s, key)
            with open(os.path.join(deck.MEDIA, name), "wb") as fh:
                fh.write(data)
            stored = defs.store(name, data)
            deck.anki("updateNoteFields", note={"id": note_for(w)["noteId"],
                                                "fields": {"Sentence": s, "Sentence Audio": f"[audio:{stored}]"}})
            # The definition is derived from the sentence, so a rewritten sentence leaves
            # it describing text that is no longer there — measured twice (stürzen: "to
            # fall" under a sentence about overthrowing a king; zustoßen, Maler on the
            # next round). Re-derive it here rather than wait for the next audit.
            fresh = new_definition(cfg, w, s, "the sentence was rewritten")
            set_definition(note_for(w), fresh, key)

    if ru:
        chosen = {}
        for name in deck.LISTS:
            deck.use_list(name)
            chosen.update({r[0]: r[1] for r in deck.read_tsv(deck.CHOSEN)})
        deck.use_list("words")
        text = "\n".join(f"{i}: {w} | {chosen[w]} | ЗАМЕЧАНИЕ: {what}" for i, (w, what) in enumerate(ru, 1))
        system = deck.SYSTEM_RU + (
            "\n\nВ этом запросе после предложения стоит ЗАМЕЧАНИЕ рецензента к прежнему переводу — "
            "учти его; отвечай только строками `id: перевод`.")
        reply, _, _ = ask(text, system, model="opus", what="RU fixes", effort="high")
        for m in re.finditer(r"^\s*(\d+)\s*[:.]\s*(\S.*?)\s*$", reply or "", re.M):
            i = int(m.group(1))
            if 1 <= i <= len(ru):
                w = ru[i - 1][0]
                trans_upd[w] = m.group(2)
                deck.anki("updateNoteFields", note={"id": note_for(w)["noteId"], "fields": {"Notes": m.group(2)}})
                deck.note(f"  RU {w}: {m.group(2)}")
        for name in set(deck.LISTS):
            deck.use_list(name)
            rewrite_tsv(deck.TRANSLATIONS, {w: v for w, v in trans_upd.items()
                                            if deck.locate(w)[0] == name})
        deck.use_list("words")

    for w, cat, what in dd:
        n = note_for(w)
        text = new_definition(cfg, w, f(n, "Sentence"), what)
        set_definition(n, text, cfg["key"])
        deck.note(f"  {cat} {w}: {text}")
    deck.note(f"fixed: {len(de)} DE, {len(ru)} RU, {len(dd)} DEF/SENSE")


if __name__ == "__main__":
    main()
