"""The shared layer under every self-contained HTML page gigaku renders.

Three pages (plots, the weekly report card, Netflix titles) each carried their own copy of
the same three things: the sentinel-substitution writer, the categorical palette, and the
theme/reset CSS. Copies drift — the palette existed twice with nothing pinning them equal,
and "German is blue" is a promise the phone card and the page must keep together. So the
copies live here, once, and each page keeps only what is genuinely its own: its markup and
its chart code (deliberately NOT shared — a page you can read top to bottom in one file is
a property worth more than deduplicated JavaScript).

Pages stay fully self-contained: everything here is *inlined at render time* via
sentinels, never fetched. `__DATA__` carries the JSON payload, `__BASE_CSS__` the CSS
below; a page that doesn't carry a sentinel simply doesn't get that substitution.
"""
import json
import os

# The validated categorical palette (dataviz skill, references/palette.md). A language owns
# its slot for good — lib/vocab/plots._slots assigns them, and filtering or hiding one
# never repaints the others. One row per theme; pages index by resolved theme, the report
# card (a dark-only PNG) takes the dark row.
PALETTE = {
    "light": ["#2a78d6", "#1baf7a", "#eda100", "#008300",
              "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"],
    "dark": ["#3987e5", "#199e70", "#c98500", "#008300",
             "#9085e9", "#e66767", "#d55181", "#d95926"],
}

# What every themed page shares and nothing may fork: the theme variables (light is the
# default, dark rides [data-theme='dark'] — pages resolve "auto" in JS and always stamp
# the attribute, so one mechanism serves the system theme and a manual override alike)
# and the [hidden] fix. Layout, type and component CSS stay per page — they genuinely
# differ, and forcing them shared would trade real divergence for fake unity.
BASE_CSS = """\
  :root {
    --surface: #fcfcfb;
    --page: #f9f9f7;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11, 11, 11, 0.1);
    --wash: rgba(11, 11, 11, 0.04);
    color-scheme: light;
  }
  :root[data-theme='dark'] {
    --surface: #1a1a19;
    --page: #0d0d0d;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.1);
    --wash: rgba(255, 255, 255, 0.06);
    color-scheme: dark;
  }

  /* A flex/grid container's `display` outranks the `hidden` attribute, so hiding one by
     setting .hidden = true silently does nothing without this. */
  [hidden] { display: none !important; }
"""


def json_payload(data):
    """A payload safe to sit inside a <script> block: `</` would end the element mid-JSON
    (the classic `</script>` injection), so it is escaped — JSON reads `<\\/` as `</`."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render(template_path, out_path, subs):
    """Fill a template's sentinels and write the page. `subs` maps sentinel → replacement,
    verbatim (run data through `json_payload` first). A sentinel the template doesn't
    carry is skipped; one it carries and `subs` doesn't fill is an error the tests catch
    (the rendered page must contain no `__`-sentinels)."""
    with open(template_path, encoding="utf-8") as f:
        page = f.read()
    for sentinel, value in subs.items():
        page = page.replace(sentinel, value)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
