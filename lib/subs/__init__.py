"""Netflix subtitle ripping (`gigaku subs`) and Excel-subs → SRT conversion (`gigaku srt`).

- `subs` drives Language Reactor to rip a whole season (needs pyobjc/AppleScript).
- `excel_to_srt` converts LR's xlsx exports into aligned Primary/Secondary SRT pairs
  (pure logic — openpyxl only).
- `naming` is the shared title → filename logic both produce, so a season lands in one
  directory however it was obtained.
- `episode_range` is `gigaku subs [N-M]`'s argument — pure, like `naming`, so a typo'd
  range is answered by the CLI without loading the browser-driving module at all.

This `__init__` deliberately imports **nothing**: `gigaku srt` and the tests import
`excel_to_srt`/`naming` directly, and pulling the browser-driving `subs` module in here
would drag pyobjc into that pure-logic path. Import the flow explicitly with
``from lib.subs import subs``.
"""
