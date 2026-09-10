"""macOS host glue, shared across domains: AppleScript, Chrome JS-exec, display power.

Domain-agnostic system primitives — nothing here knows about words, subtitles or the
Apple TV. `subs` drives Chrome through them today; a future GUI or another
browser-automation feature reuses the same wrappers rather than reinventing them.
"""
