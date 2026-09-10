# `anki/` — the Anki add-on

`anki/gigaku/` is the add-on; `anki/link.sh` symlinks every subdirectory of this folder into
Anki's `addons21/` as a directory symlink (Anki writes each add-on's runtime `meta.json` back
through it, which `.gitignore` keeps out), **prunes** symlinks whose repo target is gone —
which is how retired add-ons are removed in place — and migrates a real directory while
keeping its `meta.json`. The map is the repo-root `CLAUDE.md`; the CLI side of clarity
scoring is `lib/anki/CLAUDE.md`.

> The setup is shaped around the third-party *MvJ Japanese* add-on and its note type, which
> is **not in this repository** (no redistribution licence). Locally it is symlinked in at
> `anki/MvJ Japanese/` (gitignored), and `tests/test_addon_config.py` skips the checks that
> read its config when it is absent. Its German support, the shared card template and its
> importer are documented beside that add-on, not here: what follows is only what
> `anki/gigaku/` does. German and Japanese share one template, one tag convention and one
> importer since 2026-08-13, and every feature below serves both note types.

- **The package's load-bearing trick: `__init__` guards `import aqt`.** So the pure modules
  import outside Anki — `rules.py` (the sort key `{n:06d} {morph} {cl:03d} {all:04d}`,
  `whole_sentence`, `norm_morph`, the `my-learn` pick: highest clarity, full stop, then
  `am-all-morphs-count`, then lowest note id; `freq_order_cards`), `queries.py` (every search
  string built or recognised) and `trim_rules.py` (the trimmer's end-cut ladder and guards,
  all the measured numbers) — and `tests/test_addon_rules.py` pins them **against
  `lib/anki/{select,store}.py` implementation-to-implementation**. The old "one rule, two
  implementations, re-typed inside a test" arrangement is gone; drift now fails the suite.
- **`core/`** holds the one logger (`/tmp/gigaku-anki.log`, debug off, capped), the one
  config reader (per-feature sections; **AnkiMorphs' tags come from the profile's own
  `ankimorphs_profile_settings.json`**, formerly hardcoded in three places), the one
  MvJ/Trimmer locator, `patching.patch_once` (sentinel-guarded, legacy sentinels included so a
  mixed old/new state can never double-patch), and the browser machinery (the order set on the
  search rather than applied and re-searched, one landing consumed by the search it was asked
  for, anchor returns).
- **Every hotkey is a setting, and this add-on's own config is the only one.** `keys` holds
  the browse keys, `nav` holds J and L (K opens the German deck; `goto_de_query` empty means
  the my-learn 🇩🇪 queue); `known_toggle_hotkey` is **M**, because `I` belongs to MvJ's View
  Context. Reading MvJ's Settings ▸ Hotkeys rows for these keys, and seeding this add-on's
  defaults into them, were both tried and both taken back out in one evening: a filled row
  there is not a label, it is what makes *MvJ* bind its own version of the action, so every
  row naming a key this side already held put two claims on that key, and Qt answers two
  claims by firing neither. The rows that version seeded were cleared once, and only where a
  row still held exactly the seeded value — a row the user edited is never fought, the rule
  `browser_view` follows too. `_disable_rivals` sweeps the keys actually taken
  — QShortcuts **and QActions**, since MvJ binds its Tag as Known as a menu action; the entry
  keeps its menu item and loses only the key.
- **`features/` is a registry** that `entry.py` installs one by one behind try/except — one
  broken feature costs that feature, never the add-on; `Tools → Gigaku add-on status` names
  each one ok/off/failed, and every feature has a config off-switch. The my-learn pick groups
  **by word alone** (parity-measured before the change: zero picks moved); the counter groups
  by `(effective deck, word)` with the deck read as `odid or did` of the lowest card id (two
  silent nondeterminisms fixed in the port). `freq_order.py` replays the stored queue
  (`gigaku.freqOrder`, written by `freqdeck/order.py` and the sentence-mining skill) after
  every AnkiMorphs recalc, because recalc rewrites the `due` of every new card it manages.
- **`features/browser_view.py` is the "nothing extraneous, everything needed" layer, and its
  model is memory, not presets** (presets were tried and rejected by the user): the browser
  remembers the view — columns, their order, their widths, the sort column and direction —
  exactly as the user last shaped them *by hand*, and re-applies that ONE state
  (`gigaku.viewState` in the collection config) on arrival at the three gigaku views: the
  my-learn queue, L's alternates, and MvJ's O episode context. **The columns are one state
  everywhere; the order has one exception, and only one** — the episode context reads in the
  order the episode was watched (`conf.view_sort`: `noteCrt` ascending, since MvJ builds an
  episode's notes in order and the audio filename's timestamp is unpadded and comes in two
  formats, so text-sorting the field is worse). That order is put on the **search** and never
  into the sort state Anki persists, so "the other views keep the order I gave them" holds
  by construction. **A press costs exactly one search**: `browser_will_search` fires after
  Anki has resolved `context.order` from the sort state and before the query runs, so the
  order and the columns are set on the search that is already running (the old shape applied
  the sort and called `browser.search()` again — two to four full searches of a 460k-card
  collection per keypress); `browser_did_search` schedules the landing on a zero-delay timer,
  the first moment the view is final. **Capture is off the user's hands, never off a
  search**: a sort is remembered when the user *clicks a header* (`sectionClicked`, the one
  signal a programmatic sort does not emit), columns and widths off the header's move/resize
  signals, debounced; the header's *visual* order is the ground truth. A click inside the
  episode view is obeyed for that visit and deliberately not remembered. Columns are applied
  **wholesale in order** via `set_browser_card_columns`, never by `toggle_column` (which
  appends at the end — the preset era's one real bug), and with no model reset of their own:
  every apply runs inside the one reset Anki brackets a search with. Widths are applied
  *after* the search, since sections only exist once that reset has ended. MvJ's O used to
  re-sort the episode on a 0–100 ms timer — that call is swallowed at the source
  (`mvj_retunes._suppress_context_sort`: the O wrapper arms a short-lived flag and exactly one
  `show_and_sort_by_column` call is dropped; MvJ's other sorts are untouched). Outside the
  three views the user's own columns come back via the stash. `tests/test_addon_browser.py`
  pins all of it against a stubbed Anki whose load-bearing details were read off Anki's own
  bytecode.
- Also here: two saved searches seeded **once** behind a marker (a deletion is a decision,
  never fought); arrival tooltips ("my-learn · 8,163 words") on transitions only; a one-shot
  Tools action folding the bookkeeping fields (am-*, My Run, My Sort Key) in the editor; the
  trimmer for sentence audio, whose grab zones ride a padded track and whose foreign constants
  are snapshotted at patch time. The deep design history lives in the modules' own
  docstrings — each feature file carries the measurements that shaped it.
