""" Draw a parsed diff into a review view: the file text, coloured line regions, gutter marks, header phantom """
import html
import json
import logging
import os

import sublime
import sublime_plugin

from . import parse
from .git import STATUS_NAMES
from .state import VIEW_FLAG
from .utils import settings

LOG = logging.getLogger("vdiff")

KEY_DEL = "vdiff.del"
KEY_INS = "vdiff.ins"
KEY_DEL_GUTTER = "vdiff.del.gutter"
KEY_INS_GUTTER = "vdiff.ins.gutter"
KEY_DEL_CHAR = "vdiff.del.char"
KEY_INS_CHAR = "vdiff.ins.char"
REGION_KEYS = (KEY_DEL, KEY_INS, KEY_DEL_GUTTER, KEY_INS_GUTTER, KEY_DEL_CHAR, KEY_INS_CHAR)

PHANTOM_KEY = "vdiff.header"
STATUS_KEY = "vdiff"

# a block phantom anchored at point 0 is drawn below row 0, so row 0 stays empty and the file starts at row 1.
# Only index_point / caret_index know about it.
_HEADER_ROWS = 1

# scopes with a background in every bundled colour scheme (Mariana, Monokai, Breakers, Celeste, Sixteen)
SCOPE_DEL = "diff.deleted"
SCOPE_INS = "diff.inserted"
SCOPE_DEL_CHAR = "diff.deleted.char"
SCOPE_INS_CHAR = "diff.inserted.char"
# gutter dots use the scope's foreground
SCOPE_DEL_MARK = "markup.deleted"
SCOPE_INS_MARK = "markup.inserted"

# rules written into a colour-scheme override when the active scheme lacks the scopes above
FALLBACK_RULES = [
    {"scope": SCOPE_DEL, "background": "hsla(357, 45%, 60%, 0.15)", "foreground_adjust": "l(+ 5%)"},
    {"scope": SCOPE_DEL_CHAR, "background": "hsla(357, 60%, 60%, 0.30)", "foreground_adjust": "l(+ 10%)"},
    {"scope": SCOPE_INS, "background": "hsla(114, 31%, 68%, 0.15)", "foreground_adjust": "l(+ 5%)"},
    {"scope": SCOPE_INS_CHAR, "background": "hsla(114, 45%, 68%, 0.30)", "foreground_adjust": "l(+ 10%)"},
]

KEY_HINTS = "f files   c commits   s stage   u unstage   d discard   o open   ] [ change   n p file   r refresh"

HEADER_CSS = """
body { margin: 0; padding: 0.5rem 0.8rem; background-color: color(var(--background) blend(var(--foreground) 93%)); }
.path { font-weight: bold; }
.old { color: color(var(--foreground) alpha(0.6)); }
.del { color: var(--redish); font-weight: bold; }
.ins { color: var(--greenish); font-weight: bold; }
.state { color: var(--orangish); }
.actions { margin-left: 1rem; }
a { text-decoration: none; padding: 0.15rem 0.5rem; margin-left: 0.3rem; border-radius: 3px;
    background-color: color(var(--foreground) alpha(0.12)); color: var(--foreground); }
.danger { color: var(--redish); }
.commit { margin-top: 0.4rem; color: color(var(--foreground) alpha(0.75)); }
.hash { color: var(--bluish); font-weight: bold; }
.body { color: color(var(--foreground) alpha(0.6)); }
"""


class VdiffReplaceCommand(sublime_plugin.TextCommand):
    """Replace the whole buffer; the only writer of a review view"""

    def run(self, edit, text):
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)


def init_view(view):
    """Settings a review view needs, applied once when it is created or restored"""
    s = view.settings()
    s.set(VIEW_FLAG, True)
    # native line numbers would be wrong: deleted lines have none
    s.set("line_numbers", False)
    s.set("word_wrap", False)
    s.set("gutter", True)
    s.set("highlight_line", True)
    s.set("scroll_past_end", False)
    s.set("detect_indentation", False)
    s.set("translate_tabs_to_spaces", False)
    s.set("show_definitions", False)
    s.set("mini_diff", False)
    s.set("spell_check", False)
    view.set_scratch(True)
    view.set_read_only(True)
    if not view.name():
        view.set_name("vDiff")
    view.set_status(STATUS_KEY, KEY_HINTS)
    ensure_scheme_scopes(view)


def index_point(view, index, col=0):
    """Text point of column `col` on the row showing st.lines[index]"""
    return view.text_point(index + _HEADER_ROWS, col)


def caret_index(view):
    """Index into st.lines of the caret's row (the header row maps to -1)"""
    if not len(view.sel()):
        return 0
    return view.rowcol(view.sel()[0].begin())[0] - _HEADER_ROWS


def fingerprint(st):
    """Cheap "did anything change" key for working-tree mode: mtimes of the file and of the git index"""
    if st.in_commit or st.change is None:
        return None
    return (_mtime(os.path.join(st.repo, st.change.path)), _mtime(os.path.join(st.repo, ".git", "index")))


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def render_message(view, st, message):
    """Show a one-line notice instead of a file (nothing to review, binary, ...)"""
    st.change = None
    st.lines = []
    st.blocks = []
    st.binary = False
    _replace(view, message)
    view.assign_syntax("scope:text.plain")
    _finish(view, st, 0, 0, "vDiff")


def render(view, st):
    """Draw st.lines for st.change into the view"""
    lines = st.lines
    if st.binary:
        text = "(binary file)"
    elif not lines:
        text = "(no textual changes)"
    else:
        text = "\n".join(line.text for line in lines)

    _replace(view, text)
    path = os.path.join(st.repo, st.change.path)
    _apply_syntax(view, path if lines and not st.binary else None)

    if lines and not st.binary:
        starts = _row_starts(lines)
        deleted, inserted = _line_regions(lines, starts)
        view.add_regions(KEY_DEL, deleted, SCOPE_DEL, flags=sublime.DRAW_NO_OUTLINE)
        view.add_regions(KEY_INS, inserted, SCOPE_INS, flags=sublime.DRAW_NO_OUTLINE)
        mark_flags = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
        del_marks = [sublime.Region(starts[i]) for i, line in enumerate(lines) if line.kind == "-"]
        ins_marks = [sublime.Region(starts[i]) for i, line in enumerate(lines) if line.kind == "+"]
        view.add_regions(KEY_DEL_GUTTER, del_marks, SCOPE_DEL_MARK, icon="dot", flags=mark_flags)
        view.add_regions(KEY_INS_GUTTER, ins_marks, SCOPE_INS_MARK, icon="dot", flags=mark_flags)
        if settings().get("word_diff", True):
            del_chars, ins_chars = _char_regions(lines, st.blocks, starts)
            view.add_regions(KEY_DEL_CHAR, del_chars, SCOPE_DEL_CHAR, flags=sublime.DRAW_NO_OUTLINE)
            view.add_regions(KEY_INS_CHAR, ins_chars, SCOPE_INS_CHAR, flags=sublime.DRAW_NO_OUTLINE)

    ins, dele = parse.counts(lines)
    _finish(view, st, ins, dele, "⟐ {}".format(os.path.basename(st.change.path)))
    view.sel().clear()
    view.sel().add(sublime.Region(index_point(view, 0)))
    view.set_viewport_position((0, 0), False)


def _finish(view, st, ins, dele, name):
    """Header, tab name, status bar and bookkeeping shared by render and render_message"""
    _header(view, st, ins, dele)
    view.set_name(name)
    view.set_status(STATUS_KEY, KEY_HINTS)
    st.persist(view)
    st.fingerprint = fingerprint(st)


def _replace(view, text):
    view.set_read_only(False)
    try:
        view.run_command("vdiff_replace", {"text": "\n" * _HEADER_ROWS + text})
    finally:
        view.set_read_only(True)
    for key in REGION_KEYS:
        view.erase_regions(key)


def _apply_syntax(view, path):
    """The real file's syntax, so the diff is highlighted like the file itself"""
    syntax = None
    if path:
        try:
            syntax = sublime.find_syntax_for_file(path)
        except Exception:
            LOG.debug("find_syntax_for_file failed", exc_info=True)
    view.assign_syntax(syntax.path if syntax else "scope:text.plain")


def _row_starts(lines):
    """Text point where each line starts; computed from the text we wrote, no view round-trips"""
    starts = []
    point = _HEADER_ROWS
    for line in lines:
        starts.append(point)
        point += len(line.text) + 1
    starts.append(point - 1)  # end of the buffer (the last line has no newline)
    return starts


def _line_regions(lines, starts):
    """Full-line regions of deleted and inserted rows, consecutive rows merged"""
    regions = {"-": [], "+": []}
    run_kind = None
    run_start = 0
    for i in range(len(lines) + 1):
        kind = lines[i].kind if i < len(lines) else None
        if kind == run_kind:
            continue
        if run_kind in regions:
            # min(): the last line's region stops at the buffer end instead of one past it
            regions[run_kind].append(sublime.Region(starts[run_start], min(starts[i], starts[-1])))
        run_kind, run_start = kind, i
    return regions["-"], regions["+"]


def _char_regions(lines, blocks, starts):
    """Changed characters inside paired deleted/inserted lines"""
    deleted = []
    inserted = []
    for block in blocks:
        for del_row, ins_row in parse.paired_lines(lines, block):
            old_ranges, new_ranges = parse.char_ranges(lines[del_row].text, lines[ins_row].text)
            deleted.extend(sublime.Region(starts[del_row] + a, starts[del_row] + b) for a, b in old_ranges)
            inserted.extend(sublime.Region(starts[ins_row] + a, starts[ins_row] + b) for a, b in new_ranges)
    return deleted, inserted


def header_html(st, ins, dele):
    """The minihtml header: path, counts, state, action links and the commit line"""
    change = st.change
    if change is None:
        title = '<span class="path">vDiff</span>'
    else:
        title = '<span class="path">{}</span>'.format(html.escape(change.path))
        if change.old_path:
            title += ' <span class="old">← {}</span>'.format(html.escape(change.old_path))
        title += '&nbsp;&nbsp;<span class="del">-{}</span> <span class="ins">+{}</span>'.format(dele, ins)
        if not st.in_commit:
            state = "staged" if change.staged else STATUS_NAMES.get(change.status, "")
            if state:
                title += '&nbsp;&nbsp;<span class="state">{}</span>'.format(state)

    actions = []
    if change is not None and not st.in_commit:
        actions.append('<a href="stage">Stage</a>')
        actions.append('<a href="unstage">Unstage</a>')
        actions.append('<a href="discard" class="danger">Discard</a>')
    if change is not None:
        actions.append('<a href="open">Open</a>')
    actions.append('<a href="files">Files</a>')
    actions.append('<a href="commits">Commits</a>')

    commit = ""
    if st.in_commit and st.commit:
        info = st.commit
        commit = '<div class="commit"><span class="hash">{}</span> · {} · {} · {}'.format(
            html.escape(info.short), html.escape(info.author), html.escape(info.date), html.escape(info.subject)
        )
        body = info.body.strip().splitlines()
        if body:
            commit += ' <span class="body">— {}</span>'.format(html.escape(body[0]))
        commit += "</div>"

    return '<body id="vdiff-header"><style>{}</style><div>{}<span class="actions">{}</span></div>{}</body>'.format(
        HEADER_CSS, title, "".join(actions), commit
    )


def _header(view, st, ins, dele):
    """One block phantom above the text; redrawn from scratch, so no PhantomSet state to keep"""

    def navigate(href):
        view.run_command("vdiff_action", {"action": href})

    view.erase_phantoms(PHANTOM_KEY)
    view.add_phantom(PHANTOM_KEY, sublime.Region(0), header_html(st, ins, dele), sublime.LAYOUT_BLOCK, navigate)


# --- colour scheme fallback -------------------------------------------------

_scheme_checked = set()


def ensure_scheme_scopes(view):
    """Write a scheme override with diff backgrounds when the active scheme has none (third-party schemes)"""
    name = _scheme_override_name(view)
    if not name or name in _scheme_checked:
        return
    _scheme_checked.add(name)
    try:
        if view.style_for_scope(SCOPE_INS).get("background"):
            return
    except Exception:
        return
    path = os.path.join(sublime.packages_path(), "User", name)
    try:
        scheme = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                scheme = sublime.decode_value(f.read()) or {}
        rules = scheme.setdefault("rules", [])
        present = {rule.get("scope") for rule in rules}
        rules.extend(rule for rule in FALLBACK_RULES if rule["scope"] not in present)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scheme, f, indent=4)
        LOG.info("wrote diff scopes into colour scheme override %s", path)
    except Exception:
        LOG.warning("could not write colour scheme override %s", path, exc_info=True)


def _scheme_override_name(view):
    """File name in Packages/User that overrides the active scheme (same base name, .sublime-color-scheme)"""
    try:
        # already resolved through "auto" and dark/light settings by sublime
        name = sublime.ui_info()["color_scheme"]["resolved_value"]
    except Exception:
        name = view.settings().get("color_scheme") or ""
    if not name or name == "auto":
        return None
    root, _ = os.path.splitext(os.path.basename(name))
    return root + ".sublime-color-scheme"
