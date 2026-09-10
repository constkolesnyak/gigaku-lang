"""Wiring: the feature registry, installed one feature at a time, each behind try/except.

One broken feature — an Anki update that moved a private attribute, a third-party add-on
that renamed a function — must cost that feature, never the add-on: the rest keep
installing, the failures are collected, and `Tools → Gigaku add-on status` names each one
with its error. Silence is reserved for health. A feature can also be switched off in the
config (`"<feature>": {"enabled": false}`) without uninstalling anything.
"""
from importlib import import_module

FEATURES = (
    ("counter", "features.counter"),
    ("am_toolbar", "features.am_toolbar"),
    ("recalc", "features.am_recalc"),
    ("freq_order", "features.freq_order"),
    ("nav", "features.nav"),
    ("keys", "features.keys"),
    ("merge", "features.merge"),
    ("mvj_retunes", "features.mvj_retunes"),
    ("hotkey_ui", "features.hotkey_ui"),
    ("trimmer", "features.trimmer"),
    ("cyrillic", "features.cyrillic"),
    ("browser_view", "features.browser_view"),
)

# name -> "ok" | "off" | "failed: <error>" — what the status dialog shows.
STATUS: dict[str, str] = {}


def install():
    from .core import conf

    for name, module_path in FEATURES:
        try:
            if not conf.enabled(name):
                STATUS[name] = "off"
                continue
            module = import_module(f"{__package__}.{module_path}")
            module.install()
            STATUS[name] = "ok"
        except Exception as exc:  # noqa: BLE001 — one feature must never take down the rest
            STATUS[name] = f"failed: {type(exc).__name__}: {exc}"
    _wire_status_menu()
    failures = [name for name, state in STATUS.items() if state.startswith("failed")]
    if failures:
        _announce(failures)


def _wire_status_menu():
    try:
        from aqt import gui_hooks

        gui_hooks.main_window_did_init.append(_add_status_menu)
    except Exception:  # noqa: BLE001
        pass


def _add_status_menu():
    try:
        from aqt import mw
        from aqt.qt import QAction

        action = QAction("Gigaku add-on status", mw)
        action.triggered.connect(_show_status)
        mw.form.menuTools.addAction(action)
    except Exception:  # noqa: BLE001
        pass


def _show_status():
    from aqt import mw
    from aqt.utils import showText

    from .core.log import LOG_PATH

    lines = [f"{'✓' if state == 'ok' else '·' if state == 'off' else '✗'} {name}: {state}"
             for name, state in STATUS.items()]
    lines.append("")
    lines.append(f"log: {LOG_PATH} (enable with \"debug\": true in the add-on config)")
    showText("\n".join(lines), parent=mw, title="Gigaku add-on status")


def _announce(failures):
    try:
        from aqt import gui_hooks
        from aqt.utils import tooltip

        names = ", ".join(failures)

        def say():
            tooltip(f"Gigaku add-on: {names} failed — see Tools → Gigaku add-on status")

        gui_hooks.main_window_did_init.append(say)
    except Exception:  # noqa: BLE001
        pass
