"""Draw a parsed diff into a review view: file text, line tints, gutter marks, header phantom"""
import bisect
import html
import logging
import os
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

import sublime
import sublime_plugin

from . import git, palette, parse
from .git import FileChange, STATUS_NAMES
from .parse import Block, Line
from .state import MODE_BRANCHES, MODE_DIFF, MODE_SETTING, VIEW_FLAG, ViewState
from .utils import file_count, settings, status

LOG = logging.getLogger("vdiff")

KEY_DEL = "vdiff.del"
KEY_INS = "vdiff.ins"
KEY_DEL_GUTTER = "vdiff.del.gutter"
KEY_INS_GUTTER = "vdiff.ins.gutter"
KEY_DEL_CHAR = "vdiff.del.char"
KEY_INS_CHAR = "vdiff.ins.char"
KEY_FILE_HEADER = "vdiff.file.header"
KEY_FILE_DEL_COUNT = palette.SCOPE_FILE_DEL_COUNT
KEY_FILE_INS_COUNT = palette.SCOPE_FILE_INS_COUNT
KEY_FILE_STATUS = palette.SCOPE_FILE_STATUS
KEY_FILE_PREFIX = "vdiff.file.prefix"
KEY_BRANCH_SEL = "vdiff.branch.sel"
KEY_BRANCH_DEL = "vdiff.branch.del"
KEY_BRANCH_INS = "vdiff.branch.ins"
KEY_BRANCH_STANDING_DEL = "vdiff.branch.standing.del"
KEY_BRANCH_STANDING_INS = "vdiff.branch.standing.ins"
KEY_BRANCH_SEL_DEL = "vdiff.branch.sel.del"
KEY_BRANCH_SEL_INS = "vdiff.branch.sel.ins"
KEY_BRANCH_CURSOR = "vdiff.branch.cursor"
KEY_BRANCH_CURSOR_DEL = "vdiff.branch.cursor.del"
KEY_BRANCH_CURSOR_INS = "vdiff.branch.cursor.ins"
REGION_KEYS = (
    KEY_DEL,
    KEY_INS,
    KEY_DEL_GUTTER,
    KEY_INS_GUTTER,
    KEY_DEL_CHAR,
    KEY_INS_CHAR,
    KEY_FILE_HEADER,
    KEY_FILE_DEL_COUNT,
    KEY_FILE_INS_COUNT,
    KEY_FILE_STATUS,
    KEY_FILE_PREFIX,
    KEY_BRANCH_SEL,
    KEY_BRANCH_DEL,
    KEY_BRANCH_INS,
    KEY_BRANCH_STANDING_DEL,
    KEY_BRANCH_STANDING_INS,
    KEY_BRANCH_SEL_DEL,
    KEY_BRANCH_SEL_INS,
    KEY_BRANCH_CURSOR,
    KEY_BRANCH_CURSOR_DEL,
    KEY_BRANCH_CURSOR_INS,
)

PHANTOM_KEY = "vdiff.header"
STATUS_KEY = "vdiff"

# visual bell: a timed whole-buffer tint, kept out of REGION_KEYS so a re-render cannot erase it early
BELL_KEY = "vdiff.bell"
BELL_MS = 90

# above this much changed text the colours cost more than they are worth: such a diff is scrolled, not read
TOKEN_COLOUR_MAX_CHARS = 200000
# the hidden output panel the all-files view tokenises each file in
TOKEN_PANEL = "vdiff_tokens"

# a block phantom at point 0 draws below row 0, so file text starts at row 1
_HEADER_ROWS = 1

# gutter dots use the scope's foreground
SCOPE_DEL_MARK = "markup.deleted"
SCOPE_INS_MARK = "markup.inserted"
# ST4 defines region.<colour>ish in every bundled scheme for exactly this kind of transient marking
SCOPE_BELL = "region.yellowish"
SCOPE_FILE_HEADER = palette.SCOPE_FILE_HEADER

# sublime loads a newly created scheme override only after a restart
NEEDS_RESTART = "restart Sublime Text to finish enabling syntax highlighting on changed lines"
NEEDS_RESTART_BRANCHES = "restart Sublime Text to finish enabling the colours of the branches view"


# actions: the ACTIONS names the row runs, a link when there is one; short: status-bar label, "" omits it
class Key(NamedTuple):
    """One row of the status-bar hints and the ? popup"""

    keys: str
    label: str
    short: str
    actions: Tuple[str, ...]


# (group title, applies in a commit (False: needs a working-tree file), rows)
KEY_GROUPS: List[Tuple[str, bool, List[Key]]] = [
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
        "Actions",
        False,
        [
            Key("s / S", "stage / stage all", "stage", ("stage", "stage_all")),
            Key("u / U", "unstage / unstage all", "unstage", ("unstage", "unstage_all")),
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
            Key("f", "changed files", "files", ("files",)),
            Key("c", "recent commits", "commits", ("commits",)),
            Key("b", "branches", "branches", ("branches",)),
            Key("v", "toggle all-files view", "view", ("view",)),
            Key("left right", "collapse / expand file", "fold", ("collapse", "expand")),
            Key("z", "collapse / expand all files", "", ("fold_all",)),
            Key("esc", "close the view", "", ("close",)),
        ],
    ),
]

# the keys of the branches view; the "applies in a commit" flag has no meaning there
BRANCH_KEY_GROUPS: List[Tuple[str, bool, List[Key]]] = [
    (
        "Navigate",
        True,
        [
            Key("J K", "next / previous branch", "branch", ("next_branch", "prev_branch")),
            Key("j k", "next / previous commit", "commit", ("next_commit", "prev_commit")),
            Key("o v", "review the commit, or the changed files", "review", ("open_commit",)),
        ],
    ),
    (
        "Actions",
        True,
        [
            Key("⏎", "checkout remote branch", "checkout", ("checkout",)),
            Key("shift+⏎", "checkout a remote branch detached", "", ("checkout_detached",)),
            Key("L", "pull", "pull", ("pull",)),
            Key("P", "push, setting the upstream when missing", "push", ("push",)),
            Key("F", "fetch all remotes and prune", "fetch", ("fetch",)),
            Key("d", "delete the local branch", "delete", ("delete_branch",)),
        ],
    ),
    (
        "View",
        True,
        [
            Key("f", "changed files", "files", ("files",)),
            Key("c", "recent commits", "commits", ("commits",)),
            Key("b", "reload the branches", "", ("branches",)),
            Key("r", "refresh", "refresh", ("refresh",)),
            Key("esc", "close the view", "", ("close",)),
        ],
    ),
]

MODE_KEY_GROUPS: Dict[str, List[Tuple[str, bool, List[Key]]]] = {MODE_DIFF: KEY_GROUPS, MODE_BRANCHES: BRANCH_KEY_GROUPS}


def keys(mode: str = MODE_DIFF) -> List[Key]:
    """Every key row of a mode's table"""
    return [key for _, _, group in MODE_KEY_GROUPS[mode] for key in group]


def key_hints(mode: str = MODE_DIFF) -> str:
    """The status-bar line of a mode: its keys with a short label"""
    return "   ".join(f"{k.keys} {k.short}" for k in keys(mode) if k.short)


def _key_width(mode: str) -> int:
    """The popup font is the editor's, so the key column is padded to a fixed width in characters"""
    return max(len(k.keys) for k in keys(mode))


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

# minihtml drops the rest of a stylesheet at a selector it cannot parse, so each rule uses one bare element or class
# .group's top margin replaces body's top padding
HELP_CSS = """
body { margin: 0; padding: 0 0.8rem 0.6rem 0.8rem; }
.group { margin-top: 0.6rem; color: var(--bluish); font-weight: bold; }
.note { color: color(var(--foreground) alpha(0.5)); font-weight: normal; }
.row { color: color(var(--foreground) alpha(0.85)); }
.dim { color: color(var(--foreground) alpha(0.35)); }
.k { color: var(--orangish); font-weight: bold; }
a { text-decoration: none; color: color(var(--foreground) alpha(0.85)); }
"""


class VdiffReplaceCommand(sublime_plugin.TextCommand):
    """Replace the whole buffer; the only writer of a review view"""

    def run(self, edit: sublime.Edit, text: str) -> None:
        """Replace the buffer"""
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)


def _clear_external_diagnostics(view: sublime.View) -> None:
    """Erase LSP diagnostic regions painted before the view became a widget"""
    for prefix in ("lspLSP-pyright", "lspLSP-pylsp", "lspLSP"):
        for severity in range(1, 5):
            for mode in ("s", "m"):
                key = f"{prefix}d{mode}{severity}"
                for suffix in ("", "_underline", "_icon"):
                    view.erase_regions(key + suffix)


def init_view(view: sublime.View, mode: str = MODE_DIFF) -> None:
    """Settings a review view needs, applied once when it is created or restored"""
    s = view.settings()
    # mark the view as vdiff's, say which keys apply, and turn off editor chrome that does not fit a diff
    s.set(VIEW_FLAG, True)
    s.set(MODE_SETTING, mode)
    # native line numbers would be wrong: deleted lines have none
    s.set("line_numbers", False)
    s.set("word_wrap", False)
    s.set("gutter", True)
    s.set("highlight_line", True)
    s.set("scroll_past_end", False)
    s.set("detect_indentation", False)
    s.set("translate_tabs_to_spaces", False)
    s.set("show_definitions", False)
    # LSP ignores widget views; is_scratch alone does not stop every LSP version
    s.set("is_widget", True)
    s.set("mini_diff", False)
    s.set("spell_check", False)
    # keep linters and LSP off this synthetic buffer, whichever of these settings they read
    s.set("syntax_checking", False)
    s.set("sublime_linter_disable", True)
    s.set("lsp_diagnostics", False)
    _clear_external_diagnostics(view)
    # a read-only viewer, so vim's insert mode is meaningless: command mode, block caret. vdiff's
    # own keys carry no command_mode context, so they fire in either mode
    s.set("command_mode", True)
    s.set("inverse_caret_state", True)
    # a scratch, read-only buffer with the key hints in the status bar
    view.set_scratch(True)
    view.set_read_only(True)
    if not view.name():
        view.set_name("vDiff")
    view.set_status(STATUS_KEY, key_hints(mode))


def index_point(view: sublime.View, index: int, col: int = 0) -> int:
    """Text point of column `col` on the row showing st.lines[index]"""
    return view.text_point(index + _HEADER_ROWS, col)


def caret_index(view: sublime.View) -> int:
    """Index into st.lines of the caret's row (the header row maps to -1)"""
    if not len(view.sel()):
        return 0
    return view.rowcol(view.sel()[0].begin())[0] - _HEADER_ROWS


def fingerprint(st: ViewState) -> Optional[Tuple[Any, ...]]:
    """Cheap "did anything change" key of a working-tree diff: mtimes of the shown files and of the git index;
    None for a commit or nothing shown (the branches view keeps its own, see branches.fingerprint)"""
    if st.in_commit or st.change is None:
        return None
    return worktree_fingerprint(st.repo, st.files if st.all_mode else [st.change])


def worktree_fingerprint(repo: str, files: Sequence[FileChange]) -> Tuple[Optional[float], ...]:
    """mtimes of `files` and of the index they are staged in"""
    index = git.index_path(repo)
    return tuple(git.mtime(os.path.join(repo, c.path)) for c in files) + (git.mtime(index) if index else None,)


def flash(view: sublime.View) -> None:
    """Visual bell: briefly tint the whole buffer"""
    view.add_regions(BELL_KEY, [sublime.Region(0, view.size())], _bell_scope(view), flags=sublime.DRAW_NO_OUTLINE)
    # unconditional erase: overlapping flashes may cut one short, but none can leave the tint stuck
    sublime.set_timeout(lambda: view.is_valid() and view.erase_regions(BELL_KEY), BELL_MS)


def _file_header_scope(view: sublime.View) -> str:
    """Heading tint scope: vdiff's own, else the first fallback the scheme gives a background"""
    # vdiff's own scope, once the generated scheme override has loaded
    if palette.scope_background(view, SCOPE_FILE_HEADER):
        return SCOPE_FILE_HEADER
    for scope in ("region.blueish", "region.cyanish", SCOPE_BELL, "diff.inserted"):
        if palette.scope_background(view, scope):
            return scope
    return SCOPE_FILE_HEADER


def _bell_scope(view: sublime.View) -> str:
    """region.yellowish, or the insert tint when the scheme lacks it"""
    if palette.scope_background(view, SCOPE_BELL):
        return SCOPE_BELL
    # an invisible bell is a silent failure, so borrow the insert tint
    return palette.SCOPE_INS


def render_message(view: sublime.View, st: ViewState, message: str) -> None:
    """Show a one-line notice instead of a file (nothing to review, binary, ...)"""
    st.change = None
    st.lines = []
    st.blocks = []
    st.binary = False
    _replace(view, message)
    view.assign_syntax("scope:text.plain")
    _finish(view, st, MODE_DIFF, "vDiff", header_html(st, 0, 0))


def _trim_context(lines: Sequence[Line], context: int) -> List[Line]:
    """Changed lines plus up to `context` lines either side of each change"""
    if not lines:
        return []
    keep = set()
    for start, end in parse.change_blocks(lines):
        keep.update(range(max(0, start - context), min(len(lines), end + context)))
    return [line for i, line in enumerate(lines) if i in keep]


def _all_line_text(line: Line) -> str:
    """One all-files diff row: old and new line numbers, then the line's text"""
    old = f"{line.old_no:>4}" if line.old_no is not None else "    "
    new = f"{line.new_no:>4}" if line.new_no is not None else "    "
    return f"{old} {new}   {line.text}"


def render_all(view: sublime.View, st: ViewState, focus: Union[None, str, Tuple[str, int]] = None, center: bool = False) -> None:
    """Draw every file, with a compact collapsible file heading before its diff.

    Without `focus` the caret goes to the top. A path puts it on that file's heading, a (path, hunk)
    target on that change's first row; the scroll is kept unless the caret has left the screen, or
    the caret is centred with `center`
    """
    # keep the scroll only for a focused redraw; reset the rows and change-navigation tables
    viewport = view.viewport_position() if focus is not None else (0, 0)
    heading = 0
    rows: List[str] = []
    metadata: List[Tuple[Optional[FileChange], Optional[Line], bool]] = []
    st.all_rows = metadata
    st.all_change_rows = []
    st.all_change_targets = []
    st.all_change_row_targets = []
    # build the rows: a blank line and heading per file, then its trimmed diff unless folded
    for change in st.files:
        rows.append("")
        metadata.append((None, None, False))
        lines, _, binary = st.all_diffs.get(change.path, ([], [], False))
        context = settings().get("all_files_context", 5)
        trimmed = _trim_context(lines, max(0, int(context)) if context is not None else 5)
        # number hunks even when folded, so navigation can unfold to one
        hunk_index = 0
        in_change = False
        for line in trimmed:
            changed = line.kind != " "
            if changed and not in_change:
                st.all_change_targets.append((change.path, hunk_index))
                hunk_index += 1
            in_change = changed
        # the heading: fold marker, path, lowercase status letter (s when anything is staged) and the file's -/+ counts
        ins, dele = parse.counts(lines)
        marker = "▶" if change.path in st.collapsed else "▼"
        if change.path == focus:
            heading = len(rows)
        letter = "s" if change.staged else change.status.lower()
        rows.append(f"{marker} {change.path}  {letter} -{dele} +{ins}")
        metadata.append((change, None, True))
        # the diff rows, recording the row where each visible change starts
        if change.path not in st.collapsed:
            previous_changed = False
            visible_hunk = -1
            for line in trimmed:
                rows.append(_all_line_text(line))
                metadata.append((change, line, False))
                changed = line.kind != " "
                if changed and not previous_changed:
                    visible_hunk += 1
                    st.all_change_rows.append(len(rows) - 1)
                    st.all_change_row_targets.append((change.path, visible_hunk))
                previous_changed = changed
    # write the text and syntax
    text = "\n".join(rows) if rows else "(no changes)"
    _replace(view, text)
    # one buffer syntax would parse prefixes and headings as code: tokenise each file alone,
    # paint its colours as regions
    view.assign_syntax("scope:text.plain")
    palette.ensure(view)
    if palette.claim_restart_notice(view, palette.DIFF_SCOPES):
        status(NEEDS_RESTART)
    # the token colours only once their scheme rules are live; tokenising before the restart would be thrown away
    resolved = palette.tokens_resolved(view)
    if resolved:
        _all_colours(view, st, [c.path for c in st.files if c.path not in st.collapsed])
    # per-row regions: heading counts, number prefix, tint, token colours
    starts = _row_starts_for_text(rows)
    del_regions, ins_regions, file_regions, prefix_regions = [], [], [], []
    file_del_counts, file_ins_counts, file_statuses = [], [], []
    token_groups: Dict[str, List[sublime.Region]] = {}
    default_colour = palette.foreground(view, "text.plain")
    # context colours come from a later scheme rule, so they may still await a restart
    ctx_resolved = palette.tokens_resolved(view, ("ctx",))
    for i, (change, line, section) in enumerate(metadata):
        if section:
            file_regions.append(sublime.Region(starts[i], starts[i] + len(rows[i])))
            # colour just the status letter and counts over the heading tint; rfind, as a path may contain " -"
            minus = rows[i].rfind(" -") + 1
            plus = rows[i].rfind(" +") + 1
            if minus >= 2:
                # the letter sits just before the space before the deletion count
                file_statuses.append(sublime.Region(starts[i] + minus - 2, starts[i] + minus - 1))
            if minus and plus > minus:
                file_del_counts.append(sublime.Region(starts[i] + minus, starts[i] + plus - 1))
            if plus:
                file_ins_counts.append(sublime.Region(starts[i] + plus, starts[i] + len(rows[i])))
        if section or line is None:
            continue
        text_start = starts[i] + len(rows[i]) - len(line.text)
        # tint only the file text, leaving the line-number columns plain
        region = sublime.Region(text_start, starts[i] + len(rows[i]))
        # the last separator column of _all_line_text keeps the view background: a gap before the text
        prefix_regions.append(sublime.Region(starts[i], text_start - 1))
        if resolved and change is not None:
            kind = {"-": "del", "+": "ins"}.get(line.kind, "ctx")
            for a, b, colour in st.all_colours.get(change.path, {}).get((line.old_no, line.new_no), []):
                # context text in the default colour needs no region: plain text draws it already
                if kind == "ctx" and (colour == default_colour or not ctx_resolved):
                    continue
                scope = palette.scope_for(kind, colour)
                if scope:
                    token_groups.setdefault(scope, []).append(sublime.Region(text_start + a, text_start + b))
        if line.kind == "-":
            del_regions.append(region)
        elif line.kind == "+":
            ins_regions.append(region)
    # paint everything
    view.add_regions(KEY_DEL, del_regions, palette.line_scope(view, "del"), flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_INS, ins_regions, palette.line_scope(view, "ins"), flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_FILE_HEADER, file_regions, _file_header_scope(view), flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_FILE_DEL_COUNT, file_del_counts, KEY_FILE_DEL_COUNT, flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_FILE_INS_COUNT, file_ins_counts, KEY_FILE_INS_COUNT, flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_FILE_STATUS, file_statuses, KEY_FILE_STATUS, flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_FILE_PREFIX, prefix_regions, palette.line_numbers_scope(view), flags=sublime.DRAW_NO_OUTLINE)
    for scope, regions in token_groups.items():
        view.add_regions(palette.token_key(scope), regions, scope, flags=sublime.DRAW_NO_OUTLINE)
    _finish(view, st, MODE_DIFF, "⟐ all files", _all_header_html(st))
    # place the caret: on the focused change's first row, else on `heading` (a file's or the top)
    if isinstance(focus, tuple):
        heading = next((row for row, shown in zip(st.all_change_rows, st.all_change_row_targets) if shown == focus), 0)
    point = index_point(view, heading)
    view.sel().clear()
    view.sel().add(sublime.Region(point))
    if center:
        view.show_at_center(point)
    else:
        view.set_viewport_position(viewport, False)
        if focus is not None:
            view.show(point)


def _all_colours(view: sublime.View, st: ViewState, paths: Sequence[str]) -> None:
    """Fill st.all_colours for `paths`, tokenising each file with its own syntax in a hidden panel"""
    missing = [path for path in paths if path not in st.all_colours and path in st.all_diffs]
    window = view.window()
    if not missing or window is None:
        return
    # one hidden panel tokenises every missing file, then is destroyed
    panel = window.create_output_panel(TOKEN_PANEL, unlisted=True)
    colours: Dict[str, Optional[str]] = {}  # scope name -> colour, shared by every file
    try:
        for path in missing:
            lines = st.all_diffs[path][0]
            st.all_colours[path] = _file_colours(view, panel, os.path.join(st.repo, path), lines, colours)
    finally:
        window.destroy_output_panel(TOKEN_PANEL)


def _file_colours(
    view: sublime.View, panel: sublime.View, path: str, lines: Sequence[Line], colours: Dict[str, Optional[str]]
) -> Dict[Tuple[Optional[int], Optional[int]], List[Tuple[int, int, str]]]:
    """{(old_no, new_no): [(start, end, colour)]} for every line of one file's full-context diff

    The new side (context and insertions) and the old side (context and deletions) are each a whole,
    valid version of the file, so each is parsed on its own; interleaved they would confuse the parser
    """
    result: Dict[Tuple[Optional[int], Optional[int]], List[Tuple[int, int, str]]] = {}
    if sum(len(line.text) + 1 for line in lines) > TOKEN_COLOUR_MAX_CHARS:
        # too big to colour; the flat tint still shows it
        return result
    _apply_syntax(panel, path)
    for side in ("+", "-"):
        if side == "-" and not any(line.kind == "-" for line in lines):
            continue  # nothing deleted: the old side would only repeat the context rows
        stream = [line for line in lines if line.kind in (" ", side)]
        panel.run_command("vdiff_replace", {"text": "\n".join(line.text for line in stream)})
        point = 0
        for line in stream:
            # context rows come from the new side
            if line.kind == side or side == "+":
                runs = _colour_runs(panel, point, line.text, colours, view)
                result[(line.old_no, line.new_no)] = [(r.a - point, r.b - point, c) for r, c in runs]
            point += len(line.text) + 1
    return result


def _row_starts_for_text(rows: Sequence[str]) -> List[int]:
    """Text point where each all-files row starts, below the header rows"""
    starts: List[int] = []
    point = _HEADER_ROWS
    for row in rows:
        starts.append(point)
        point += len(row) + 1
    return starts


def all_change_at(view: sublime.View, st: ViewState) -> Optional[FileChange]:
    """File whose heading or diff row holds the caret, else None"""
    index = caret_index(view)
    if 0 <= index < len(st.all_rows):
        return st.all_rows[index][0]
    return None


def all_change_below(view: sublime.View, st: ViewState) -> Optional[FileChange]:
    """File of the first heading or diff row at or below the caret: the file a blank row or the top row precedes"""
    for change, _, _ in st.all_rows[max(0, caret_index(view)):]:
        if change is not None:
            return change
    return None


def follow_caret(view: sublime.View, st: ViewState) -> None:
    """In the all-files view, make the file under the caret current and redraw the header for it

    Only when the caret crossed into another file, and not while a render is pending: the rows then
    describe the old text
    """
    if st.in_branches or not st.all_mode or st.rendered_token != st.token:
        return
    change = all_change_at(view, st)
    if change is None or (st.change is not None and change.path == st.change.path):
        return
    st.change = change
    _header(view, st, _all_header_html(st))


def _all_header_html(st: ViewState) -> str:
    """The all-files header: the totals on the summary line, the current file's own counts in the title"""
    change = st.change
    return header_html(st, change.insertions if change else 0, change.deletions if change else 0)


def render(view: sublime.View, st: ViewState) -> None:
    """Draw st.lines for st.change into the view"""
    change = st.change
    # every caller with nothing to show goes to render_message instead, so there is a file here
    assert change is not None
    lines = st.lines
    # the text: a placeholder for a binary or empty diff, else the lines
    if st.binary:
        text = "(binary file)"
    elif not lines:
        text = "(no textual changes)"
    else:
        text = "\n".join(line.text for line in lines)

    # write the text and syntax
    _replace(view, text)
    path = os.path.join(st.repo, change.path)
    _apply_syntax(view, path if lines and not st.binary else None)

    if lines and not st.binary:
        # here rather than init_view: during a session restore the scheme colours still read as absent
        palette.ensure(view)
        _paint_regions(view, st)
        if palette.claim_restart_notice(view, palette.DIFF_SCOPES):
            status(NEEDS_RESTART)

    # header and bookkeeping, then the caret and scroll back to the top
    ins, dele = parse.counts(lines)
    _finish(view, st, MODE_DIFF, f"⟐ {os.path.basename(change.path)}", header_html(st, ins, dele))
    view.sel().clear()
    view.sel().add(sublime.Region(index_point(view, 0)))
    view.set_viewport_position((0, 0), False)


def _paint_regions(view: sublime.View, st: ViewState) -> None:
    """The line tint, the gutter dots, the word-diff marks, and the syntax colours on top of them"""
    lines = st.lines
    # full-line tint
    starts = _row_starts(lines)
    deleted, inserted = _line_regions(lines, starts)
    view.add_regions(KEY_DEL, deleted, palette.line_scope(view, "del"), flags=sublime.DRAW_NO_OUTLINE)
    view.add_regions(KEY_INS, inserted, palette.line_scope(view, "ins"), flags=sublime.DRAW_NO_OUTLINE)
    # gutter dots
    mark_flags = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
    del_marks = [sublime.Region(starts[i]) for i, line in enumerate(lines) if line.kind == "-"]
    ins_marks = [sublime.Region(starts[i]) for i, line in enumerate(lines) if line.kind == "+"]
    view.add_regions(KEY_DEL_GUTTER, del_marks, SCOPE_DEL_MARK, icon="dot", flags=mark_flags)
    view.add_regions(KEY_INS_GUTTER, ins_marks, SCOPE_INS_MARK, icon="dot", flags=mark_flags)
    # word-diff marks, when enabled
    del_chars: List[sublime.Region] = []
    ins_chars: List[sublime.Region] = []
    if settings().get("word_diff", True):
        del_chars, ins_chars = _char_regions(lines, st.blocks, starts)
        view.add_regions(KEY_DEL_CHAR, del_chars, palette.SCOPE_DEL_CHAR, flags=sublime.DRAW_NO_OUTLINE)
        view.add_regions(KEY_INS_CHAR, ins_chars, palette.SCOPE_INS_CHAR, flags=sublime.DRAW_NO_OUTLINE)
    # syntax colours on top of the tints
    _paint_tokens(view, lines, starts, del_chars + ins_chars)


def _paint_tokens(view: sublime.View, lines: Sequence[Line], starts: Sequence[int], char_regions: Sequence[sublime.Region]) -> None:
    """Repaint each run of same-coloured tokens with a scope carrying the tint *and* that colour

    add_regions draws text in the region scope's own foreground, so the tints above have just
    flattened every changed line; only handing each run its own colour gives the highlighting back
    """
    if not palette.tokens_resolved(view):
        return
    changed = [i for i, line in enumerate(lines) if line.kind != " "]
    if sum(len(lines[i].text) for i in changed) > TOKEN_COLOUR_MAX_CHARS:
        # too big to colour; the flat tint still shows it
        return

    # word-diff ranges sorted by start, so each line can bisect for its own
    stronger = sorted((r.a, r.b) for r in char_regions)
    starts_of = [a for a, _ in stronger]
    # group every changed line's colour runs by scope
    groups: Dict[str, List[sublime.Region]] = {}
    colours: Dict[str, Optional[str]] = {}  # scope name -> colour, resolved once per render
    for i in changed:
        kind = "del" if lines[i].kind == "-" else "ins"
        end = starts[i] + len(lines[i].text)
        # only this line's word-diff ranges, so a diff with thousands of them stays linear
        on_line = stronger[bisect.bisect_left(starts_of, starts[i]) : bisect.bisect_left(starts_of, end)]
        for region, colour in _colour_runs(view, starts[i], lines[i].text, colours):
            # a word-diff range cuts across token boundaries, so the run splits on it
            for part, inside in _split(region, on_line):
                # a colour outside the scheme's probed palette keeps the plain tint under it
                scope = palette.scope_for(kind + ".char" if inside else kind, colour)
                if scope:
                    groups.setdefault(scope, []).append(part)
    # one region set per scope
    for scope, regions in groups.items():
        view.add_regions(palette.token_key(scope), regions, scope, flags=sublime.DRAW_NO_OUTLINE)


def _split(region: sublime.Region, intervals: Sequence[Tuple[int, int]]) -> List[Tuple[sublime.Region, bool]]:
    """(part, inside) covering `region`, cut wherever one of the sorted `intervals` starts or ends"""
    if not intervals:
        return [(region, False)]
    # walk the intervals, emitting the gap before each and its overlap with the region
    parts: List[Tuple[sublime.Region, bool]] = []
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
    # the tail after the last interval, or the whole region when none overlapped
    if pos < region.b or not parts:
        parts.append((sublime.Region(pos, region.b), False))
    return parts


def _colour_runs(
    view: sublime.View, start: int, text: str, colours: Dict[str, Optional[str]], scheme_view: Optional[sublime.View] = None
) -> List[Tuple[sublime.Region, str]]:
    """(region, colour) per run of same-coloured tokens on one line, adjacent equal colours merged

    `scheme_view` resolves the colours when `view` is only a tokenising buffer
    """
    runs: List[Tuple[sublime.Region, str]] = []
    end = start + len(text)
    for region, scope in view.extract_tokens_with_scopes(sublime.Region(start, end)):
        # a token can run past the line's end, e.g. into the newline
        region = sublime.Region(max(region.begin(), start), min(region.end(), end))
        if region.b <= region.a:
            continue
        # resolve each scope's colour once, skipping tokens without one
        if scope not in colours:
            colours[scope] = palette.foreground(scheme_view or view, scope)
        colour = colours[scope]
        if not colour:
            continue
        # extend the previous run when the same colour continues it
        if runs and runs[-1][1] == colour and runs[-1][0].b == region.a:
            runs[-1] = (sublime.Region(runs[-1][0].a, region.b), colour)
        else:
            runs.append((region, colour))
    return runs


def _finish(view: sublime.View, st: ViewState, mode: str, name: str, header: str, key: Callable[[ViewState], Any] = fingerprint) -> None:
    """Mode, header, tab name, status bar and bookkeeping shared by every render: what was drawn is settled here

    `key` is the mode's fingerprint, the branches view's own one there
    """
    st.mode = mode
    _header(view, st, header)
    view.set_name(name)
    view.set_status(STATUS_KEY, key_hints(st.mode))
    view.settings().set(MODE_SETTING, st.mode)
    # the caret row's highlight; the branches view turns it off itself once it tints the caret's commit
    view.settings().set("highlight_line", True)
    st.persist(view)
    st.fingerprint = key(st)
    st.drawn = st.describe()


def _replace(view: sublime.View, text: str) -> None:
    """Write `text` below the header rows, then erase every vdiff region and token layer"""
    view.set_read_only(False)
    try:
        view.run_command("vdiff_replace", {"text": "\n" * _HEADER_ROWS + text})
    finally:
        view.set_read_only(True)
    # the token layer's keys vary with the palette, so they are listed rather than fixed
    for key in list(REGION_KEYS) + palette.token_keys():
        view.erase_regions(key)


def _apply_syntax(view: sublime.View, path: Optional[str]) -> None:
    """The real file's syntax, so the diff is highlighted like the file itself"""
    syntax = None
    if path:
        try:
            syntax = sublime.find_syntax_for_file(path)
        except Exception:
            LOG.debug("find_syntax_for_file failed", exc_info=True)
    view.assign_syntax(syntax.path if syntax else "scope:text.plain")


def _row_starts(lines: Sequence[Line]) -> List[int]:
    """Text point where each line starts; computed from the text we wrote, no view round-trips"""
    starts: List[int] = []
    point = _HEADER_ROWS
    for line in lines:
        starts.append(point)
        point += len(line.text) + 1
    starts.append(point - 1)  # end of the buffer (the last line has no newline)
    return starts


def _line_regions(lines: Sequence[Line], starts: Sequence[int]) -> Tuple[List[sublime.Region], List[sublime.Region]]:
    """Full-line regions of deleted and inserted rows, consecutive rows merged"""
    regions: Dict[str, List[sublime.Region]] = {"-": [], "+": []}
    for kind, start, end in parse.runs(lines, lambda line: line.kind):
        if kind in regions:
            # starts has a sentinel at len(lines): the last line stops at the buffer end
            regions[kind].append(sublime.Region(starts[start], starts[end]))
    return regions["-"], regions["+"]


def _char_regions(
    lines: Sequence[Line], blocks: Sequence[Block], starts: Sequence[int]
) -> Tuple[List[sublime.Region], List[sublime.Region]]:
    """Changed characters inside paired deleted/inserted lines"""
    deleted: List[sublime.Region] = []
    inserted: List[sublime.Region] = []
    for block in blocks:
        # word-diff each deleted/inserted pair and offset its ranges to buffer points
        for del_row, ins_row in parse.paired_lines(lines, block):
            old_ranges, new_ranges = parse.char_ranges(lines[del_row].text, lines[ins_row].text)
            deleted.extend(sublime.Region(starts[del_row] + a, starts[del_row] + b) for a, b in old_ranges)
            inserted.extend(sublime.Region(starts[ins_row] + a, starts[ins_row] + b) for a, b in new_ranges)
    return deleted, inserted


def _summary_html(st: ViewState) -> str:
    """Summary line above the title: the commit on one line, or the staged/unstaged split and totals"""
    if not st.files and not st.commit:
        return ""
    if st.in_commit:
        text = " · ".join(_commit_parts(st))
    else:
        # the same counts as the branches view's working-tree entry: a partially staged file is on both sides
        stat = git.worktree_stats(st.files)
        text = (
            f"{stat.staged} staged, {stat.unstaged} unstaged"
            f' · <span class="del">-{stat.deletions}</span> <span class="ins">+{stat.insertions}</span>'
        )
    return f'<div class="summary">{text}</div>'


def _commit_parts(st: ViewState) -> List[str]:
    """Hash, author, date, totals and message of the reviewed commit, in order"""
    info = st.commit
    parts: List[str] = []
    # the commit's identity: hash, author, date
    if info:
        parts.append(f'<span class="hash">{html.escape(info.short)}</span>')
        parts.append(html.escape(info.author))
        parts.append(f'<span class="when">{html.escape(info.date)}</span>')
    # binary files add nothing to the line totals but still count toward the file total
    stat = git.worktree_stats(st.files)
    parts.append(
        f"{file_count(stat.files)} changed" f' <span class="del">-{stat.deletions}</span> <span class="ins">+{stat.insertions}</span>'
    )
    # the subject, plus the body's first line
    if info:
        message = html.escape(info.subject)
        body = info.body.strip().splitlines()
        if body:
            message += f' <span class="body">— {html.escape(body[0])}</span>'
        parts.append(message)
    return parts


def header_html(st: ViewState, ins: Optional[int], dele: Optional[int]) -> str:
    """The minihtml header: the summary line, then the shown file's path, its counts (None for a binary file), state and action links"""
    change = st.change
    # the title: path, rename source, counts and working-tree state
    if change is None:
        title = '<span class="path">vDiff</span>'
    else:
        title = f'<span class="path">{html.escape(change.path)}</span>'
        if change.old_path:
            title += f' <span class="old">← {html.escape(change.old_path)}</span>'
        if ins is None or dele is None:
            title += '&nbsp;&nbsp;<span class="state">binary</span>'
        else:
            title += f'&nbsp;&nbsp;<span class="del">-{dele}</span> <span class="ins">+{ins}</span>'
        if not st.in_commit:
            state = "staged" if change.staged else STATUS_NAMES.get(change.status, "")
            if state:
                title += f'&nbsp;&nbsp;<span class="state">{state}</span>'

    # action links: stage, unstage and discard need a working-tree file, open needs any file
    actions: List[str] = []
    if change is not None and not st.in_commit:
        actions.append('<a href="stage">Stage</a>')
        actions.append('<a href="unstage">Unstage</a>')
        actions.append('<a href="discard" class="danger">Discard</a>')
    if change is not None:
        actions.append('<a href="open">Open</a>')
    return header_frame(_summary_html(st), title, actions)


def header_frame(summary: str, title: str, actions: Sequence[str]) -> str:
    """The header's body: the summary line, then the title with `actions` and the links every view has"""
    links = list(actions) + [
        '<a href="files">Files</a>',
        '<a href="commits">Commits</a>',
        '<a href="branches">Branches</a>',
        '<a href="help" title="keys">?</a>',
    ]
    return (
        f'<body id="vdiff-header"><style>{HEADER_CSS}</style>{summary}'
        f'<div>{title}<span class="actions">{"".join(links)}</span></div></body>'
    )


def _run_action(view: sublime.View, href: str) -> None:
    """Dispatch a header or popup link through the same command the keymap uses"""
    view.run_command("vdiff_action", {"action": href})


def _header(view: sublime.View, st: ViewState, html_text: str) -> None:
    """One block phantom above the text, redrawn from scratch unless the same one is still in place"""
    # keyed on the buffer too: replacing the text can drop the phantom while query_phantom still answers for it
    drawn = html_text, view.change_count()
    if st.header == drawn:
        return

    def navigate(href: str) -> None:
        """Run the clicked header link's action"""
        _run_action(view, href)

    view.erase_phantoms(PHANTOM_KEY)
    view.add_phantom(PHANTOM_KEY, sublime.Region(0), html_text, sublime.LAYOUT_BLOCK, navigate)
    st.header = drawn


# --- key help popup ---------------------------------------------------------


def help_html(st: ViewState) -> str:
    """The minihtml key list of the shown mode; rows that need a working-tree file are dimmed while a commit is shown"""
    parts: List[str] = []
    width = _key_width(st.mode)
    # per group a heading, dimmed with a note when it does not apply in a commit
    for title, applies, group in MODE_KEY_GROUPS[st.mode]:
        dim = st.in_commit and not applies
        heading = html.escape(title)
        if dim:
            heading += ' <span class="note">uncommitted only</span>'
        parts.append(f'<div class="group">{heading}</div>')
        # keys stay orange in a dimmed group; only the text dims
        row_class = "dim" if dim else "row"
        # a row per key with a padded key column; a single-action row becomes a link
        for key in group:
            pad = "&nbsp;" * (width - len(key.keys) + 2)
            row = f'<span class="k">{html.escape(key.keys)}</span>{pad}{html.escape(key.label)}'
            if len(key.actions) == 1 and not dim:
                row = f'<a href="{key.actions[0]}">{row}</a>'
            parts.append(f'<div class="{row_class}">{row}</div>')
    return f'<body id="vdiff-help"><style>{HELP_CSS}</style>{"".join(parts)}</body>'


def show_help(view: sublime.View, st: ViewState) -> None:
    """Toggle the key list popup at the caret"""
    if view.is_popup_visible():
        view.hide_popup()
        return

    def navigate(href: str) -> None:
        """Close the popup, then run the clicked row's action"""
        view.hide_popup()
        _run_action(view, href)

    # minihtml sizes follow the editor font, so fixed pixel bounds would clip at a large font size
    em = view.em_width() or 8
    view.show_popup(help_html(st), location=-1, max_width=int(em * 56), max_height=int(em * 44), on_navigate=navigate)
