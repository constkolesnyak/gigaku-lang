"""Netflix browse-gallery ripping and IMDb enrichment (`gigaku titles`).

Imports nothing, deliberately — same policy as lib/subs/__init__.py. `gallery` needs pyobjc
(it drives Chrome); `imdb`, `store` and the matching logic are pure stdlib and are tested
directly, so importing them must not drag the browser stack in.
"""
