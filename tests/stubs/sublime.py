""" Minimal sublime API stub so the plugin modules import and header/regions logic runs offline """
import re

OP_EQUAL = 0
OP_NOT_EQUAL = 1
DRAW_NO_OUTLINE = 1
DRAW_NO_FILL = 2
LAYOUT_BLOCK = 2
ENCODED_POSITION = 1
KIND_ID_COLOR_REDISH = 1
KIND_ID_COLOR_ORANGISH = 2
KIND_ID_COLOR_YELLOWISH = 3
KIND_ID_COLOR_GREENISH = 4
KIND_ID_COLOR_CYANISH = 5
KIND_ID_COLOR_BLUISH = 6
KIND_ID_COLOR_PURPLISH = 7
KIND_ID_COLOR_PINKISH = 8
KIND_AMBIGUOUS = (0, "", "")


class Region:
    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def __eq__(self, other):
        return (self.a, self.b) == (other.a, other.b)

    def __repr__(self):
        return f"Region({self.a}, {self.b})"


class Edit:
    """Opaque edit token; only ever passed through, so the annotations have something to name"""


class Settings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value

    def add_on_change(self, tag, callback):
        pass


_settings = {}


def load_settings(name):
    return _settings.setdefault(name, Settings())


def set_timeout(fn, delay=0):
    fn()


def set_timeout_async(fn, delay=0):
    fn()


def status_message(text):
    pass


def packages_path():
    return "/tmp/Packages"


def windows():
    return []


class QuickPanelItem:
    def __init__(self, trigger, details="", annotation="", kind=KIND_AMBIGUOUS):
        self.trigger, self.details, self.annotation, self.kind = trigger, details, annotation, kind


class Phantom:
    def __init__(self, region, content, layout, on_navigate=None):
        self.region, self.content, self.layout, self.on_navigate = region, content, layout, on_navigate


class Window:
    """Captures the quick panel and the input panel so tests can drive their callbacks"""

    def __init__(self):
        self.panel = None
        self.input = None
        self.input_view = None
        self.commands = []

    def focus_view(self, view):
        pass

    def run_command(self, name, args=None):
        self.commands.append((name, args))

    def show_quick_panel(self, items, on_select, flags=0, selected=-1, on_highlight=None, placeholder=None):
        self.panel = (items, on_select, selected, on_highlight, placeholder)

    def show_input_panel(self, caption, initial_text, on_done, on_change, on_cancel):
        self.input = (caption, initial_text, on_done, on_change, on_cancel)
        self.input_view = View(initial_text)
        # the real panel hands its prefill back selected, which is why callers clear the selection
        self.input_view.sel().add(Region(0, len(initial_text)))
        return self.input_view


# a bundled scheme's styles: diff.* and region.* defined, the plugin's vdiff.* ones not. Nothing
# simulates sublime merging an override, so a test wanting those to resolve adds them itself.
DEFAULT_FG = "#ffffff"
DEFAULT_STYLES = {
    "diff.deleted": {"foreground": DEFAULT_FG, "background": "#4a2b2b"},
    "diff.deleted.char": {"foreground": DEFAULT_FG, "background": "#6a3535"},
    "diff.inserted": {"foreground": DEFAULT_FG, "background": "#2b4a3a"},
    "diff.inserted.char": {"foreground": DEFAULT_FG, "background": "#356a4a"},
    "region.yellowish": {"foreground": "#e6db74", "background": "#4a4a2b"},
    "markup.deleted": {"foreground": "#d64646"},
    "markup.inserted": {"foreground": "#46d646"},
    # a few of palette.PALETTE_PROBES, so the palette probe finds a working set of colours
    "source": {"foreground": DEFAULT_FG},
    "comment": {"foreground": "#7c8595"},
    "string": {"foreground": "#99c794"},
    "keyword": {"foreground": "#c594c5"},
    "constant.numeric": {"foreground": "#f99157"},
    "entity.name.function": {"foreground": "#6699cc"},
    "punctuation.definition": {"foreground": "#5fb3b3"},
}


class View:
    """Text buffer with just the calls render.py makes"""

    def __init__(self, text=""):
        self.text = text
        self.phantoms = {}
        self.settings_ = Settings()
        self.regions = {}
        self.syntax = None
        self.read_only = True
        self.name_ = ""
        self.sel_ = []
        self.status = {}
        self.commands = []
        self.popup = None
        self.popup_navigate = None
        # what style_for_scope answers; a test reshapes it to model another colour scheme
        self.styles = {scope: dict(style) for scope, style in DEFAULT_STYLES.items()}
        # whether sublime has merged the override vdiff writes; it only does so on a restart
        self.override_loaded = False

    def id(self):
        return 1

    def settings(self):
        return self.settings_

    def size(self):
        return len(self.text)

    def replace(self, edit, region, text):
        self.text = self.text[: region.begin()] + text + self.text[region.end() :]

    def run_command(self, name, args=None):
        if name == "vdiff_replace":
            self.replace(None, Region(0, self.size()), args["text"])
        else:
            self.commands.append((name, args))

    def set_read_only(self, flag):
        self.read_only = flag

    def is_read_only(self):
        return self.read_only

    def assign_syntax(self, path):
        self.syntax = path

    def add_regions(self, key, regions, scope="", icon="", flags=0):
        self.regions[key] = (list(regions), scope, icon, flags)

    def erase_regions(self, key):
        self.regions.pop(key, None)

    def text_point(self, row, col):
        lines = self.text.split("\n")
        return sum(len(l) + 1 for l in lines[:row]) + col

    def rowcol(self, point):
        before = self.text[:point]
        row = before.count("\n")
        return row, len(before) - (before.rfind("\n") + 1)

    def full_line(self, point):
        start = self.text.rfind("\n", 0, point) + 1
        end = self.text.find("\n", point)
        end = len(self.text) if end == -1 else end + 1
        return Region(start, end)

    def erase_phantoms(self, key):
        self.phantoms.pop(key, None)

    def add_phantom(self, key, region, content, layout, on_navigate=None):
        self.phantoms.setdefault(key, []).append(Phantom(region, content, layout, on_navigate))
        return len(self.phantoms[key])

    def set_name(self, name):
        self.name_ = name

    def name(self):
        return self.name_

    def set_scratch(self, flag):
        pass

    def set_status(self, key, value):
        self.status[key] = value

    def set_viewport_position(self, xy, animate=True):
        pass

    def show_at_center(self, point):
        pass

    def show_popup(self, content, flags=0, location=-1, max_width=320, max_height=240, on_navigate=None, on_hide=None):
        self.popup, self.popup_navigate = content, on_navigate

    def hide_popup(self):
        self.popup = None

    def is_popup_visible(self):
        return self.popup is not None

    def em_width(self):
        return 8.0

    def extract_tokens_with_scopes(self, region):
        """One token per run of word characters and one per run of anything else, scoped by its shape

        Enough for the colouring tests: real sublime returns the syntax's tokens, but the painter only
        needs adjacent tokens to differ and each to map to a coloured scope.
        """
        tokens = []
        text = self.text[region.a:region.b]
        for match in re.finditer(r"\w+|\s+|[^\w\s]+", text):
            scope = "keyword" if match.group().isidentifier() else "punctuation.definition"
            tokens.append((Region(region.a + match.start(), region.a + match.end()), scope))
        return tokens

    def style_for_scope(self, scope):
        # a fresh dict per call, and no background for an unknown scope, like the real API
        if self.override_loaded and scope.startswith("vdiff.") and scope not in self.styles:
            # sublime has merged the override, so every rule vdiff wrote in it resolves
            return {"foreground": DEFAULT_FG, "background": "#333333"}
        return dict(self.styles.get(scope, {"foreground": DEFAULT_FG}))

    def is_valid(self):
        return True

    class _Sel(list):
        def clear(self):
            del self[:]

        def add(self, region):
            self.append(region)

    def sel(self):
        if not isinstance(self.sel_, View._Sel):
            self.sel_ = View._Sel(self.sel_)
        return self.sel_


def find_syntax_for_file(path, first_line=""):
    class Syntax:
        pass

    syntax = Syntax()
    syntax.path = "Packages/Python/Python.sublime-syntax" if path.endswith(".py") else "Packages/Text/Plain text.tmLanguage"
    return syntax


def ui_info():
    return {
        "system": {"style": "dark"},
        "color_scheme": {
            "value": "Mariana.sublime-color-scheme",
            "resolved_value": "Packages/Color Scheme - Default/Mariana.sublime-color-scheme",
        },
    }


def decode_value(text):
    import json

    return json.loads(text)
