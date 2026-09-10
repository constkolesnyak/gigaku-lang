"""Guarded monkey-patching — every patch this add-on applies goes through here.

Two rules the old add-ons learned separately, now enforced in one place: a patch carries a
sentinel flag so it can never stack on itself, and it also checks the *old add-ons'*
sentinels (`legacy_flags`) — so during the migration window, a mixed state where an old
add-on already patched a target can never end up double-patched by the new one.
"""


def patch_once(target, attr, make_wrapper, flag, legacy_flags=()):
    """Replace `target.attr` with `make_wrapper(original)`, once.

    `target` is a module or a class. Returns True when the patch was applied now, False
    when this or a legacy sentinel says somebody already did."""
    for sentinel in (flag, *legacy_flags):
        if getattr(target, sentinel, False):
            return False
    original = getattr(target, attr)
    setattr(target, attr, make_wrapper(original))
    setattr(target, flag, True)
    return True
