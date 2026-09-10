"""A colloquial German frequency list lemmatised IN CONTEXT.

Every list up to here was a list of word *forms* lemmatised one word at a time, and that
is what left `zähnen`, `hau`, `tränen`, `gnaden`, `geistern` in the output: each is a real
German headword, so no dictionary check can reject it, but the frequency attached to it
in a subtitle corpus belongs to a different word's inflection (`Zähnen` is the dative
plural of `Zahn`). A word list cannot tell those apart, because the evidence — the
sentence — was thrown away before the list was made.

So the list is rebuilt from the sentences. spaCy reads each subtitle line whole, and its
tagger has the surrounding words to work with: `Zähnen` after `mit den` is a noun form of
`Zahn`, `hau` after `ich` is `hauen`. The same pass answers the proper-name question for
free — PROPN in context is a judgement about this occurrence, not a guess about a
spelling, which is what no amount of case-checking could give.
"""
import gzip
import os
import re
from collections import Counter

D = os.path.dirname(os.path.abspath(__file__))
EVERY = 16              # spread the sample over all 41.6M lines, not one era of cinema
LOG = open(f"{D}/ctx2.log", "a", buffering=1)


def log(m):
    LOG.write(m + "\n")


def sentences():
    with gzip.open(f"{D}/os_de_sentences.txt.gz", "rt", encoding="utf-8",
                   errors="replace") as f:
        for i, line in enumerate(f):
            if i % EVERY:
                continue
            s = line.strip()
            if s.startswith("- "):          # subtitle dialogue dash, not punctuation
                s = s[2:]
            # Hearing-impaired sound labels and on-screen captions are not dialogue:
            # "(KICHERN)", "[Wiehern]", "(ächzt)" put `quietschen` at rank 836 and
            # `Kichern` beside `kichern`, and all-caps sign lines minted ZIMT/REH as
            # lemmas of their own. Parentheticals go; a line with no lowercase left
            # was a caption, not speech.
            s = re.sub(r"[(\[][^)\]]*[)\]]", " ", s).strip()
            if 1 < len(s) < 400 and any(c.islower() for c in s):
                yield s


def main():
    import spacy
    nlp = spacy.load("de_core_news_md", disable=["parser", "ner"])
    log(f"spaCy up, pipes={nlp.pipe_names}")

    # Keep the SURFACE FORM with the case the sentence actually gave it, plus the POS the
    # tagger read off the sentence — and fold to a lemma later, with simplemma.
    # Two things this gets that neither half could alone: spaCy's German lemmatiser is a
    # lookup table that leaves `willst`, `musst`, `geh`, `gib`, `bist` standing, which
    # simplemma folds; and simplemma needs the authentic case to tell `Zähnen` (dative
    # plural of Zahn) from the verb `zähnen`, which only a real sentence carries — the
    # frequency lists had already flattened or mangled it.
    # `initial` marks the first word of a sentence \u2014 the position whose capital letter is
    # the orthography's, not the word's. A capitalised verb there is indistinguishable
    # from a noun by case, and spaCy tags a share of them NOUN (`Denke ich...` \u2192 a
    # phantom noun `Denke` at rank 569), so the fold needs to know which sightings carry
    # no case evidence. Boundaries inside a line are approximated by punctuation (the
    # parser is off): .!?\u2026: open a sentence, a comma closes the ambiguity, a bare dash is
    # a subtitle speaker change, and a leading numeral absorbs the sentence capital.
    pair = Counter()
    toks = 0
    for n, doc in enumerate(nlp.pipe(sentences(), batch_size=256, n_process=7), 1):
        start = True
        for t in doc:
            if t.is_space:
                continue
            if t.is_punct:
                if t.text[-1:] in ".!?\u2026:" or t.text in ("-", "\u2013", "\u2014"):
                    start = True
                elif t.text[-1:] in ",;":
                    start = False
                continue
            if t.like_num or not any(c.isalpha() for c in t.text):
                start = False
                continue
            pair[(t.text, t.pos_, 1 if start else 0)] += 1
            start = False
            toks += 1
        if n % 200_000 == 0:
            log(f"{n:,} sentences \u00b7 {toks:,} tokens \u00b7 {len(pair):,} form/POS pairs")

    log(f"DONE {toks:,} tokens, {len(pair):,} pairs")
    bytot = Counter()
    for (w, p, i), c in pair.items():
        bytot[(w, p)] += c
    with open(f"{D}/os_forms.tsv", "w", encoding="utf-8") as f:
        f.write(f"#tokens\t{toks}\n")
        for (w, p, i), c in pair.most_common():
            if bytot[(w, p)] >= 2:
                f.write(f"{w}\t{p}\t{i}\t{c}\n")
    log("wrote os_forms.tsv")


if __name__ == "__main__":
    main()
