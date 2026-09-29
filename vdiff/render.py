""" Draw a parsed diff into a review view: the file text, coloured line regions, gutter marks, header phantom """
import bisect
import html
import logging
import os
from collections import namedtuple

import sublime
import sublime_plugin

from . import palette, parse
from .git import STATUS_NAMES
from .state import VIEW_FLAG
from .utils import file_count, settings, status

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

# visual bell: a whole-buffer tint, erased on a timer. Deliberately not in REGION_KEYS, so the
# re-render a wrap triggers does not wipe it before it has been seen.
BELL_KEY = "vdiff.bell"
BELL_MS = 90

# above this much changed text, skip per-token colouring: a diff that large is scrolled, not read,
# and tokenising every changed line would cost more than the colours are worth
TOKEN_COLOUR_MAX_CHARS = 200000

# a block phantom anchored at point 0 is drawn below row 0, so row 0 stays empty and the file starts at row 1.
# Only index_point / caret_index know about it.
_HEADER_ROWS = 1

# gutter dots use the scope's foreground
SCOPE_DEL_MARK = "markup.deleted"
SCOPE_INS_MARK = "markup.inserted"
# ST4 defines region.<colour>ish in every bundled scheme for exactly this kind of transient marking
SCOPE_BELL = "region.yellowish"

# sublime only loads an override it has not seen before on a restart
NEEDS_RESTART = "restart Sublime Text to finish enabling syntax highlighting on changed lines"

# one source for the status-bar hints and the ? popup.
# actions: the ACTIONS names the row covers; a single one makes the row a link. short: status-bar
# wording, "" keeps the row out of it. The group flag is False for groups needing a working-tree file.
Key = namedtuple("Key", "keys label short actions")

KEY_GROUPS = [
    (
        "Navigate",
        True,
        [
            Key("] [", "next / previous change (wraps)", "change", ("next_change", "prev_change")),
            Key("n p", "next / previous file (wraps)", "file", ("next_file", "prev_file")),
            Key("o \u23ce", "open the real file at this line", "open", ("open",)),
            Key("j k", "down / up one line (vim)", "", ("line_down", "line_up")),
        ],
    ),
    (
        "Panels",
        True,
        [
            Key("f", "changed files", "files", ("files",)),
            Key("c", "recent commits", "commits", ("commits",)),
        ],
    ),
    (
        "Actions",
        False,
        [
            Key("s", "stage", "stage", ("stage",)),
            Key("u", "unstage", "unstage", ("unstage",)),
            Key("d", "discard", "discard", ("discard",)),
            Key("C", "commit the staged files", "commit", ("commit",)),
            Key("m", "amend the last commit", "amend", ("amend",)),
        ],
    ),
    (
        "View",
        True,
        [
            Key("r", "refresh", "refresh", ("refresh",)),
            Key("?", "this list", "keys", ("help",)),
            Key("esc", "close the view", "", ("close",)),
        ],
    ),
]

KEYS = [key for _, _, group in KEY_GROUPS for key in group]

KEY_HINTS = "   ".join(f"{k.keys} {k.short}" for k in KEYS if k.short)

# the popup font is the editor's, so the key column is padded to a fixed width in characters
_KEY_WIDTH = max(len(k.keys) for k in KEYS)

HEADER_CSS = """
body { margin: 0; padding: 0.5rem 0.8rem; background-color: color(var(--background) blend(var(--foreground) 93%)); }
.path { font-weight: bold; }
.old { color: color(var(--foreground) alpha(0.6)); }
.del { color: var(--redish); font-weight: bold; }
.ins { color: var(--greenish); font-weight: bold; }
.state { color: var(--orangish); }
.actions { margin-left: 1rem; }
.summary { margin-bottom: 0.3rem; color: color(var(--foreground) alpha(0.75)); }
.when { color: color(var(--foreground) alpha(0.6)); }
a { text-decoration: none; padding: 0.15rem 0.5rem; margin-left: 0.3rem; border-radius: 3px;
    background-color: color(var(--foreground) alpha(0.12)); color: var(--foreground); }
.danger { color: var(--redish); }
.hash { color: var(--bluish); font-weight: bold; }
.body { color: color(var(--foreground) alpha(0.6)); }
"""

# minihtml stops applying a stylesheet at the first selector it cannot parse, and its documentation
# covers properties only, not selectors: every rule here sticks to one bare element or one class,
# the forms HEADER_CSS proves work. The first group's margin stands in for body's top padding, and
# .kdim takes its colour from the .dim row around it.
HELP_CSS = """
body { margin: 0; padding: 0 0.8rem 0.6rem 0.8rem; }
.group { margin-top: 0.6rem; color: var(--bluish); font-weight: bold; }
.note { color: color(var(--foreground) alpha(0.5)); font-weight: normal; }
.row { color: color(var(--foreground) alpha(0.85)); }
.dim { color: color(var(--foreground) alpha(0.35)); }
.k { color: var(--orangish); font-weight: bold; }
.kdim { font-weight: bold; }
a { text-decoration: none; color: color(var(--foreground) alpha(0.85)); }
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
    # a read-only viewer, so vim's insert mode is meaningless: open in command mode with the block
    # caret. vdiff's own keys carry no command_mode context, so they fire in either mode regardless.
    s.set("command_mode", True)
    s.set("inverse_caret_state", True)
    view.set_scratch(True)
    view.set_read_only(True)
    if not view.name():
        view.set_name("vDiff")
    view.set_status(STATUS_KEY, KEY_HINTS)


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


def flash(view):
    """Visual bell: tint the buffer for a moment, for feedback that has nothing to say in words"""
    view.add_regions(BELL_KEY, [sublime.Region(0, view.size())], _bell_scope(view), flags=sublime.DRAW_NO_OUTLINE)
    # unconditional erase: overlapping flashes may cut one short, but none can leave the tint stuck
    sublime.set_timeout(lambda: view.is_valid() and view.erase_regions(BELL_KEY), BELL_MS)


def _bell_scope(view):
    """region.yellowish, or a scope with a background we know of when a scheme lacks it"""
    if palette.scope_background(view, SCOPE_BELL):
        return SCOPE_BELL
    # an invisible bell is a silent failure, so borrow the insert tint
    return palette.SCOPE_INS


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
        # here rather than in init_view: a session restore runs that while sublime is still settling,
        # and the scheme colours we borrow read as absent that early
        palette.ensure(view)
        _paint_regions(view, st)
        if palette.claim_restart_notice(view):
            status(NEEDS_RESTART)

    ins, dele = parse.counts(lines)
    _finish(view, st, ins, dele, f"⟐ {os.path.basename(st.change.path)}")
    view.sel().clear()
    view.sel().add(sublime.Region(index_point(view, 0)))
    view.set_viewport_position((0, 0), False)


def _paint_regions(view, st):
    """The line tint, the gutter dots, the word-diff marks, and the syntax colours on top of them"""
    lines = st.lines
    starts = _row_starts(lines)
    deleted, inserted = _line_regions(lines, starts)
    view.add_regions(KEY_DEL, deleted, palette.SCOPE_DEL, flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_INS, inserted, palette.SCOPE_INS, flags=sublime.DRAW_NO_OUTLINE)
    mark_flags = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
    del_marks = [sublime.Region(starts[i]) for i, line in enumerate(lines) if line.kind == "-"]
    ins_marks = [sublime.Region(starts[i]) for i, line in enumerate(lines) if line.kind == "+"]
    view.add_regions(KEY_DEL_GUTTER, del_marks, SCOPE_DEL_MARK, icon="dot", flags=mark_flags)
    view.add_regions(KEY_INS_GUTTER, ins_marks, SCOPE_INS_MARK, icon="dot", flags=mark_flags)
    del_chars, ins_chars = [], []
    if settings().get("word_diff", True):
        del_chars, ins_chars = _char_regions(lines, st.blocks, starts)
        view.add_regions(KEY_DEL_CHAR, del_chars, palette.SCOPE_DEL_CHAR, flags=sublime.DRAW_NO_OUTLINE)
        view.add_regions(KEY_INS_CHAR, ins_chars, palette.SCOPE_INS_CHAR, flags=sublime.DRAW_NO_OUTLINE)
    _paint_tokens(view, lines, starts, del_chars + ins_chars)


def _paint_tokens(view, lines, starts, char_regions):
    """Repaint each run of same-coloured tokens with a scope carrying the tint *and* that colour

    add_regions draws the text it covers in the region scope's own foreground, so the tints above have
    just flattened every changed line to one colour. Nothing gives back the file's own highlighting
    except handing each run the colour it should have had.
    """
    if not palette.tokens_resolved(view):
        return
    changed = [i for i, line in enumerate(lines) if line.kind != " "]
    if sum(len(lines[i].text) for i in changed) > TOKEN_COLOUR_MAX_CHARS:
        # a huge diff would spend longer being tokenised than read; the flat tint still shows it
        return

    stronger = sorted((r.a, r.b) for r in char_regions)
    starts_of = [a for a, _ in stronger]
    groups = {}
    colours = {}  # scope name -> colour, resolved once per render
    for i in changed:
        kind = "del" if lines[i].kind == "-" else "ins"
        end = starts[i] + len(lines[i].text)
        # only this line's word-diff ranges, so a diff with thousands of them stays linear
        on_line = stronger[bisect.bisect_left(starts_of, starts[i]):bisect.bisect_left(starts_of, end)]
        for region, colour in _colour_runs(view, starts[i], lines[i].text, colours):
            # a word-diff range cuts across token boundaries, so the run splits on it
            for part, inside in _split(region, on_line):
                # a colour outside the scheme's probed palette keeps the plain tint under it
                scope = palette.scope_for(kind + ".char" if inside else kind, colour)
                if scope:
                    groups.setdefault(scope, []).append(part)
    for scope, regions in groups.items():
        view.add_regions(palette.token_key(scope), regions, scope, flags=sublime.DRAW_NO_OUTLINE)


def _split(region, intervals):
    """(part, inside) covering `region`, cut wherever one of the sorted `intervals` starts or ends"""
    if not intervals:
        return [(region, False)]
    parts = []
    pos = region.a
    for a, b in intervals:
        a, b = max(a, region.a), min(b, region.b)
        if b <= pos:
            continue
        if a >= region.b:
            break
        if a > pos:
            parts.append((sublime.Region(pos, a), False))
        parts.append((sublime.Region(a, b), True))
        pos = b
    if pos < region.b or not parts:
        parts.append((sublime.Region(pos, region.b), False))
    return parts


def _colour_runs(view, start, text, colours):
    """(region, colour) per run of same-coloured tokens on one line, adjacent equal colours merged"""
    runs = []
    for region, scope in view.extract_tokens_with_scopes(sublime.Region(start, start + len(text))):
        if scope not in colours:
            colours[scope] = palette.foreground(view, scope)
        colour = colours[scope]
        if not colour:
            continue
        if runs and runs[-1][1] == colour and runs[-1][0].b == region.a:
            runs[-1] = (sublime.Region(runs[-1][0].a, region.b), colour)
        else:
            runs.append((region, colour))
    return runs


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
    # the token layer's keys vary with the palette, so they are listed rather than fixed
    for key in list(REGION_KEYS) + palette.token_keys():
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
    for kind, start, end in parse.runs(lines, lambda line: line.kind):
        if kind in regions:
            # starts has a sentinel at len(lines): the last line stops at the buffer end
            regions[kind].append(sublime.Region(starts[start], starts[end]))
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


def _totals(files):
    """Summed numstat counts; binary files carry None counts and add nothing"""
    return sum(c.insertions or 0 for c in files), sum(c.deletions or 0 for c in files)


def _summary_html(st):
    """Line above the header: the whole commit on one line, or how much of the tree is staged"""
    if not st.files and not st.commit:
        return ""
    if st.in_commit:
        text = " · ".join(_commit_parts(st))
    else:
        # staged means "has something staged": a partially staged file counts as staged only
        staged = sum(1 for c in st.files if c.staged)
        insertions, deletions = _totals(st.files)
        text = (
            f"{staged} staged, {len(st.files) - staged} unstaged"
            f' · <span class="del">-{deletions}</span> <span class="ins">+{insertions}</span>'
        )
    return f'<div class="summary">{text}</div>'


def _commit_parts(st):
    """hash, author, date, totals and message — everything about the reviewed commit, in that order"""
    info = st.commit
    parts = []
    if info:
        parts.append(f'<span class="hash">{html.escape(info.short)}</span>')
        parts.append(html.escape(info.author))
        parts.append(f'<span class="when">{html.escape(info.date)}</span>')
    # binary files add nothing to the line totals but still count toward the file total
    insertions, deletions = _totals(st.files)
    parts.append(f"{file_count(len(st.files))} changed" f' <span class="del">-{deletions}</span> <span class="ins">+{insertions}</span>')
    if info:
        message = html.escape(info.subject)
        body = info.body.strip().splitlines()
        if body:
            message += f' <span class="body">— {html.escape(body[0])}</span>'
        parts.append(message)
    return parts


def header_html(st, ins, dele):
    """The minihtml header: the summary line, then the shown file's path, counts, state and action links"""
    change = st.change
    if change is None:
        title = '<span class="path">vDiff</span>'
    else:
        title = f'<span class="path">{html.escape(change.path)}</span>'
        if change.old_path:
            title += f' <span class="old">← {html.escape(change.old_path)}</span>'
        title += f'&nbsp;&nbsp;<span class="del">-{dele}</span> <span class="ins">+{ins}</span>'
        if not st.in_commit:
            state = "staged" if change.staged else STATUS_NAMES.get(change.status, "")
            if state:
                title += f'&nbsp;&nbsp;<span class="state">{state}</span>'

    actions = []
    if change is not None and not st.in_commit:
        actions.append('<a href="stage">Stage</a>')
        actions.append('<a href="unstage">Unstage</a>')
        actions.append('<a href="discard" class="danger">Discard</a>')
    if change is not None:
        actions.append('<a href="open">Open</a>')
    actions.append('<a href="files">Files</a>')
    actions.append('<a href="commits">Commits</a>')
    actions.append('<a href="help" title="keys">?</a>')

    return (
        f'<body id="vdiff-header"><style>{HEADER_CSS}</style>{_summary_html(st)}'
        f'<div>{title}<span class="actions">{"".join(actions)}</span></div></body>'
    )


def _run_action(view, href):
    """Dispatch a header or popup link through the same command the keymap uses"""
    view.run_command("vdiff_action", {"action": href})


def _header(view, st, ins, dele):
    """One block phantom above the text; redrawn from scratch, so no PhantomSet state to keep"""

    def navigate(href):
        _run_action(view, href)

    view.erase_phantoms(PHANTOM_KEY)
    view.add_phantom(PHANTOM_KEY, sublime.Region(0), header_html(st, ins, dele), sublime.LAYOUT_BLOCK, navigate)


# --- key help popup ---------------------------------------------------------


def help_html(st):
    """The minihtml key list; rows that need a working-tree file are dimmed while a commit is shown"""
    parts = []
    for title, applies, group in KEY_GROUPS:
        dim = st.in_commit and not applies
        heading = html.escape(title)
        if dim:
            heading += ' <span class="note">uncommitted only</span>'
        parts.append(f'<div class="group">{heading}</div>')
        # one class per element: a compound selector would silently drop the rest of HELP_CSS
        row_class, key_class = ("dim", "kdim") if dim else ("row", "k")
        for key in group:
            row = (
                f'<span class="{key_class}">{html.escape(key.keys)}</span>'
                f'{"&nbsp;" * (_KEY_WIDTH - len(key.keys) + 2)}{html.escape(key.label)}'
            )
            if len(key.actions) == 1 and not dim:
                row = f'<a href="{key.actions[0]}">{row}</a>'
            parts.append(f'<div class="{row_class}">{row}</div>')
    return f'<body id="vdiff-help"><style>{HELP_CSS}</style>{"".join(parts)}</body>'


def show_help(view, st):
    """Toggle the key list popup at the caret"""
    if view.is_popup_visible():
        view.hide_popup()
        return

    def navigate(href):
        view.hide_popup()
        _run_action(view, href)

    # minihtml sizes follow the editor font, so fixed pixel bounds would clip at a large font size
    em = view.em_width() or 8
    view.show_popup(help_html(st), location=-1, max_width=int(em * 56), max_height=int(em * 44), on_navigate=navigate)

