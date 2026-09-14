"""Audio Trimmer retunes (was browser_jk_nav's trimmer half): preview on h, an end marker
that opens pre-placed on the clip's dead end, symmetric grab zones over a padded track,
and focus handed back to the card list on close.

The decision core of the preset lives in ``trim_rules.end_cut`` (pure, pinned by pytest);
this module supplies the pydub detector and the widget writes. The trimmer's module
constants (marker colours, widths, tolerance) are **snapshotted once at patch time** —
the old code dereferenced them at *paint* time, so a trimmer update renaming one constant
would have raised inside a paint event; now a missing constant skips the zone retune at
install, loudly, and painting can never be the thing that discovers it.
"""
import sys

from aqt import gui_hooks
from aqt.browser import Browser
from aqt.qt import QBrush, QColor, QPoint, QPointF, Qt, QTimer

from .. import trim_rules
from ..core import addons
from ..core.browser import focus_card_list
from ..core.log import log

# The trimmer constants the zone retune needs; patching is skipped if any is missing.
_NEEDED = ("MARKER_HIT_TOLERANCE_PX", "MARKER_COLOR", "MARKER_COLOR_HOVER",
           "MARKER_ZONE_ALPHA", "MARKER_ZONE_ALPHA_HOVER",
           "MARKER_LINE_WIDTH", "MARKER_LINE_WIDTH_HOVER")


def _preset_end_marker(dialog):
    """Open the trimmer with the dead end of the clip already cut off.

    Either a trailing word the speaker left after a pause, or plain trailing silence —
    either way the edit was the same drag to the same place, so the handle starts there
    and Enter is the whole interaction. The waveform still shows exactly what is about to
    go, so a wrong guess costs one drag rather than a mistake. The ladder, the guards and
    every measured number live in trim_rules.

    Silence is relative to this clip's own loudness — a quiet recording has quiet pauses,
    and a fixed dBFS threshold finds none of them."""
    try:
        from pydub.silence import detect_nonsilent
    except Exception as exc:  # noqa: BLE001 — trimmer bundles pydub; without it, don't
        log(f"trimmer: no pydub, end marker left alone ({exc.__class__.__name__})")
        return
    audio = getattr(dialog, "audio", None)
    widget = getattr(dialog, "waveform_widget", None)
    if audio is None or widget is None or not len(audio):
        return
    loudness = audio.dBFS
    threshold = max(loudness - 16, -50) if loudness != float("-inf") else -50

    def speech(min_silence):
        return detect_nonsilent(audio, min_silence_len=min_silence,
                                silence_thresh=threshold, seek_step=5)

    offer = trim_rules.end_cut(speech, len(audio))
    if offer is None:
        log("trimmer: nothing worth cutting — end marker left alone")
        return
    cut_ms, reason = offer

    points = len(widget.waveform_data)
    marker = int(round(cut_ms / len(audio) * points)) - 1
    marker = max(widget.start_marker_x + 1, min(points - 1, marker))
    if marker >= points - 1:
        return  # rounding landed on the end: nothing would be cut
    widget.end_marker_x = marker
    widget.update()  # wrapped by the dialog to refresh the trim label too
    try:
        # What the preset decided, in the one place every dialog already has: the title.
        # Both numbers are already computed; this only says them out loud.
        keep, cut = cut_ms / 1000, (len(audio) - cut_ms) / 1000
        dialog.setWindowTitle(
            f"{dialog.windowTitle()} — keep {keep:.2f}s · cut {cut:.2f}s (auto: {reason})")
    except Exception:  # noqa: BLE001 — cosmetic
        pass
    log(f"trimmer: {reason} — end marker preset to {cut_ms}ms of {len(audio)}ms")


def _grab_zone(marker_px, tolerance):
    """The pixel span [lo, hi] that grabs a marker at `marker_px` — symmetric, always.

    Nothing is clamped to the widget here; that is what `_pad_track` is for: the track
    carries a gutter of exactly `tolerance` at each end, so a marker parked at either
    extreme still has its outer half to be grabbed by. What must never come back is the
    version that kept the full width by sliding the zone *inward*: a click 40px into the
    audio then grabbed a marker sitting at the edge."""
    return marker_px - tolerance, marker_px + tolerance


class _Shifted:
    """A mouse event reporting a position `dx` to the left of the real one.

    The trimmer's own handlers work in waveform pixels — hit test, drag offsets,
    drag-to-select — so the gutter is subtracted on the way in and nothing downstream has
    to know it is there."""

    def __init__(self, event, dx):
        self._event, self._dx = event, dx

    def pos(self):
        point = self._event.pos()
        return QPoint(point.x() - self._dx, point.y())

    def position(self):
        point = self._event.position()
        return QPointF(point.x() - self._dx, point.y())

    def __getattr__(self, name):
        return getattr(self._event, name)


def _padding(gutter):
    """Total width a track has to gain to hold both grab zones whole.

    A gutter each side, plus **one more column on the right**: the end marker is drawn at
    `waveform_width`, one past the last waveform pixel, so its zone reaches one further
    than the start marker's does."""
    return 2 * gutter + 1


def _pad_track(widget_cls, gutter):
    """Give the waveform widget `gutter` blank pixels of track at each end.

    Padding the *track* is what lets the grab zone stay centred: the audio is untouched
    and every coordinate the trimmer computes stays in waveform space; the widget simply
    owns `2 * gutter` more pixels than the waveform drawn in it. Three seams: the widget
    asks for the extra width, the paint is offset into it, and mouse positions are offset
    back out. The dialog's fit-to-width is adjusted beside this (`_retune_dialog`), or the
    padded widget would no longer fit its own viewport."""
    original_zoom = widget_cls.set_zoom

    def set_zoom(self, pixels_per_point):
        original_zoom(self, pixels_per_point)
        self.setMinimumWidth(self.waveform_width + _padding(gutter))
        self.setMaximumWidth(self.waveform_width + _padding(gutter))

    def offset_paint(name):
        original = getattr(widget_cls, name)

        def draw(self, painter, *args):
            painter.translate(gutter, 0)
            try:
                original(self, painter, *args)
            finally:
                painter.translate(-gutter, 0)

        return draw

    def unshift_mouse(name):
        original = getattr(widget_cls, name)

        def handler(self, event):
            original(self, _Shifted(event, gutter))

        return handler

    widget_cls.set_zoom = set_zoom
    # _draw_marker is reached through _draw_markers, so it inherits the offset — wrapping
    # it too would apply the gutter twice.
    for name in ("_draw_waveform", "_draw_trim_region", "_draw_markers", "_draw_playhead"):
        setattr(widget_cls, name, offset_paint(name))
    for name in ("mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"):
        setattr(widget_cls, name, unshift_mouse(name))


def _retune_marker_zones(widget_cls, consts):
    """Symmetric grab zones, drawn exactly where they are grabbed.

    Both methods are replaced together on purpose: the trimmer's own invariant is that the
    shaded band *is* what can be grabbed, and it holds that by having the hit test and the
    paint derive the same span from the same tolerance. Patching one alone would shade a
    lie. `consts` is the patch-time snapshot of the trimmer's module constants."""
    tolerance = consts["MARKER_HIT_TOLERANCE_PX"]

    def _marker_at(self, x):
        start_px = int(self.start_marker_x * self.pixels_per_point)
        end_px = int((self.end_marker_x + 1) * self.pixels_per_point)
        start_lo, start_hi = _grab_zone(start_px, tolerance)
        end_lo, end_hi = _grab_zone(end_px, tolerance)
        start_hit, end_hit = start_lo <= x <= start_hi, end_lo <= x <= end_hi
        if start_hit and end_hit:
            # Unchanged from the original: nearest wins, and an exact tie goes to
            # whichever marker the click sits on the outside of, so a collapsed pair
            # stays separable.
            start_distance, end_distance = abs(x - start_px), abs(x - end_px)
            if start_distance != end_distance:
                return "start" if start_distance < end_distance else "end"
            return "start" if x < start_px else "end"
        if start_hit:
            return "start"
        if end_hit:
            return "end"
        return None

    def _draw_marker(self, painter, marker_px, hovered):
        painter.setPen(Qt.PenStyle.NoPen)
        low, high = _grab_zone(marker_px, tolerance)
        zone = QColor(consts["MARKER_COLOR"])
        zone.setAlpha(consts["MARKER_ZONE_ALPHA_HOVER"] if hovered
                      else consts["MARKER_ZONE_ALPHA"])
        painter.setBrush(QBrush(zone))
        painter.drawRect(low, 0, high - low + 1, self.waveform_height)
        # Centred on the exact cut position and no longer clamped: the end marker sits one
        # column past the last waveform pixel — inside the gutter, not off the widget.
        width = (consts["MARKER_LINE_WIDTH_HOVER"] if hovered
                 else consts["MARKER_LINE_WIDTH"])
        painter.setBrush(QBrush(consts["MARKER_COLOR_HOVER"] if hovered
                                else consts["MARKER_COLOR"]))
        painter.drawRect(marker_px - width // 2, 0, width, self.waveform_height)

    widget_cls._marker_at = _marker_at
    widget_cls._draw_marker = _draw_marker


def _retune(_browser=None):
    """Patch the dialog and waveform classes, once, in place — the trimmer isn't in this
    repo, so an update would wipe a source edit; this survives it."""
    try:
        cls = addons.trimmer().WaveformEditorDialog
        waveform = sys.modules.get(cls.__module__)
        widget_cls = getattr(waveform, "WaveformWidget", None)
        gutter = getattr(waveform, "MARKER_HIT_TOLERANCE_PX", 24) if waveform else 24
        if widget_cls is not None and not getattr(widget_cls, "_gigaku_zones", False) \
                and not getattr(widget_cls, "_jk_zones_symmetric", False):
            missing = [name for name in _NEEDED if not hasattr(waveform, name)]
            if missing:
                # A renamed constant is discovered here, at install, not inside a paint.
                log(f"trimmer: constants missing ({', '.join(missing)}) — zones left alone")
            else:
                consts = {name: getattr(waveform, name) for name in _NEEDED}
                _retune_marker_zones(widget_cls, consts)
                try:
                    _pad_track(widget_cls, gutter)
                    log(f"trimmer: symmetric grab zones over a {gutter}px gutter each end")
                except Exception as exc:  # noqa: BLE001 — zones symmetric, just clipped
                    log(f"trimmer: track padding FAILED, zones clipped: {exc!r}")
                    gutter = 0
                widget_cls._gigaku_zones = True
        if getattr(cls, "_gigaku_retuned", False) or getattr(cls, "_jk_retuned", False):
            return
        original_setup, original_keys, original_done = cls.setup_ui, cls.keyPressEvent, cls.done
        original_levels = cls._build_zoom_levels

        def _build_zoom_levels(self, usable_width):
            """Fit the *padded* widget to the viewport, not the waveform — otherwise the
            fit level makes the widget wider than its own viewport and the trimmer opens
            with a horizontal scrollbar it never had."""
            original_levels(self, max(1, usable_width - _padding(gutter)))

        def done(self, result):
            """Hand focus back to the card list when the trimmer closes.

            The trimmer ends by reloading the note into the editor, which takes focus — so
            after one trim the browser's list keys were typing into a field instead of
            navigating. Queued rather than immediate because the reload happens *after*
            `exec()` returns: a zero-delay timer runs once that work is finished."""
            original_done(self, result)
            parent = self.parent()
            if isinstance(parent, Browser):
                QTimer.singleShot(0, lambda: focus_card_list(parent))

        def setup_ui(self):
            original_setup(self)
            try:
                self.preview_button.setText("Preview (H)")
            except Exception:  # noqa: BLE001 — cosmetic
                pass
            try:
                _preset_end_marker(self)
            except Exception as exc:  # noqa: BLE001 — never stop the trimmer opening
                log(f"trimmer: preset FAILED {exc!r}")

        def keyPressEvent(self, event):  # noqa: N802 — Qt's name
            if event.key() == Qt.Key.Key_H:
                self.preview_audio()
                event.accept()
                return
            if event.key() == Qt.Key.Key_P:
                event.accept()  # p was preview; it is h now, and p does nothing
                return
            original_keys(self, event)

        cls.setup_ui, cls.keyPressEvent, cls.done = setup_ui, keyPressEvent, done
        cls._build_zoom_levels = _build_zoom_levels
        cls._gigaku_retuned = True
        log("retuned the Audio Trimmer (preview h, end marker preset, focus handed back)")
    except Exception as exc:  # noqa: BLE001
        log(f"retune trimmer FAILED {exc!r}")


def install():
    gui_hooks.browser_will_show.append(_retune)
