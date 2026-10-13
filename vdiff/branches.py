"""The branches view: local branches and remotes in a left column, the selected branch's log on the right

Drawn into the same review view as the diffs, as plain text rows of two padded columns. The selected
branch is a tinted cell moved by J / K; the caret, and with it the tinted commit, is the commit cursor
moved by j / k
"""
import html
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, TypeVar

import sublime

from . import git, palette, render
from .git import Commit, FileChange, GitError, Ref
from .state import MODE_BRANCHES, BranchRow, ViewState
from .utils import DEBOUNCE_MS, commits_count, file_count, run_async, status

# an ACTIONS entry: act(view, window, state) on the review view it fired in
Action = Callable[[sublime.View, sublime.Window, ViewState], None]

# the left column: its padding after the longest cell, and the width it never exceeds
LEFT_PAD = 2
LEFT_MAX = 32
# the right column: "Author:      " and friends, and the bounds of its width when the viewport is odd
LABEL_WIDTH = 13
RIGHT_MIN = 60
RIGHT_MAX = 200
# a commit is its subject, author, date and stats rows, then a rule
ROWS_PER_COMMIT = 5
# box drawing: the column line, the rule under each item, and the column line's branch into a rule on the rule rows
COLUMN_GAP = "│ "
RULE = "─"
JUNCTION = "├─"
HEADING_BRANCHES = "BRANCHES"
HEADING_REMOTES = "REMOTES"

NOT_HERE = "works in the branches view, press b"

# the result of a load's work, handed to its adopt step
T = TypeVar("T")
# a coloured part of a cell: (start, end, "del" | "ins")
Span = Tuple[int, int, str]
# a right-column cell: its text, the index of its item (0 the working tree, i + 1 commit i, -1 none) and its spans
Cell = Tuple[str, int, List[Span]]
# the right column's first item: the working tree
WORKTREE = 0
NO_CHANGES = "No Changes"
# a left-column cell: its text, its branch (None for headings and remote names) and the branch's
# standing against its upstream ("↑1 ↓2", "gone" or ""), shown right-aligned in the cell
LeftCell = Tuple[str, Optional[Ref], str]


# --- loading ----------------------------------------------------------------


def _pick(refs: List[Ref], wanted: Optional[str]) -> Optional[Ref]:
    """The branch to select: `wanted` while it exists, else the checked-out one, else the first local, else the first"""
    for ref in refs:
        if ref.name == wanted:
            return ref
    return next((r for r in refs if r.current), next((r for r in refs if r.remote is None), refs[0] if refs else None))


def load(view: sublime.View, st: ViewState, message: Optional[str] = None, keep_caret: bool = False) -> None:
    """List the branches and log the selected one off the UI thread, then draw the view

    `message` goes to the status bar first; `keep_caret` keeps the commit cursor where it is. The diff
    state is vacated: what was shown before is not what persist records or the refresh checks any more
    """
    if message:
        status(message)
    st.mode = MODE_BRANCHES
    st.sha = st.commit = st.change = st.requested = None
    st.lines, st.blocks, st.binary = [], [], False
    repo, wanted, count = st.repo, st.branch_ref, commits_count()

    def work() -> Tuple[str, List[Ref], List[str], Optional[Ref], List[Commit], List[FileChange]]:
        """HEAD's hash, the branches, the remotes, the branch to select and its log, and the working tree's changes

        The calls that do not wait on the branch list run alongside it; only the log needs to know the branch
        """
        with ThreadPoolExecutor(max_workers=4) as pool:
            # git status first, the slowest; the caches are warmed too, so the fingerprint after the render costs
            # no git call on the UI thread (leaving the block waits for them)
            changes, sha, remotes = pool.submit(git.changed_files, repo), pool.submit(git.head_sha, repo), pool.submit(git.remotes, repo)
            pool.submit(git.ref_paths, repo)
            git.index_path(repo)
            refs = git.branches(repo)
            ref = _pick(refs, wanted)
            commits = git.ref_log(repo, ref.name, count) if ref else []
            return sha.result(), refs, remotes.result(), ref, commits, changes.result()

    def adopt(result: Tuple[str, List[Ref], List[str], Optional[Ref], List[Commit], List[FileChange]]) -> None:
        """Take the listing over"""
        st.branch_head_sha, st.branch_refs, st.branch_remotes, ref, st.branch_commits, st.branch_changes = result
        st.branch_ref = ref.name if ref else None

    _reload(view, st, work, adopt, keep_caret)


def _reload(view: sublime.View, st: ViewState, work: Callable[[], T], adopt: Callable[[T], None], keep_caret: bool) -> None:
    """Run `work` off the UI thread, then `adopt` its result and redraw, unless a later load superseded it or the view closed"""
    st.token += 1
    token = st.token

    def done(result: T) -> None:
        """Adopt and draw the result of the newest load only"""
        if token != st.token or not view.is_valid():
            return
        adopt(result)
        render_branches(view, st, keep_caret=keep_caret)
        st.rendered_token = token

    run_async(work, done)


def _load_log(view: sublime.View, st: ViewState) -> None:
    """Log the selected branch off the UI thread and redraw with the cursor back on the working-tree entry"""
    repo, name, count = st.repo, st.branch_ref, commits_count()

    def adopt(commits: List[Commit]) -> None:
        """Take the log over"""
        st.branch_commits = commits

    _reload(view, st, lambda: git.ref_log(repo, name, count) if name is not None else [], adopt, keep_caret=False)


def load_worktree(view: sublime.View, st: ViewState) -> None:
    """Relist the working tree's changes off the UI thread and redraw its entry; the branches and the log stand"""
    repo = st.repo

    def adopt(changes: List[FileChange]) -> None:
        """Take the working tree's changes over"""
        st.branch_changes = changes

    _reload(view, st, lambda: git.changed_files(repo), adopt, keep_caret=True)


def _step(view: sublime.View, position: int, count: int, delta: int) -> Optional[int]:
    """Where a step of `delta` from `position` among `count` items lands: off them (-1) the first for a step forward
    and the last for one back; None, after a flash, past either end"""
    if position < 0:
        return 0 if delta > 0 else count - 1
    index = position + delta
    if not 0 <= index < count:
        render.flash(view)
        return None
    return index


def select(view: sublime.View, st: ViewState, delta: int) -> None:
    """Move the branch selection `delta` branches on, ringing at the ends, and log the new one

    The cell and the header move at once; the log is fetched once the key rests, so holding J does
    not start one git process per branch passed
    """
    # git lists the local branches before the remote ones, the order J / K step in
    names = [r.name for r in st.branch_refs]
    if not names:
        status("no branches")
        return
    position = names.index(st.branch_ref) if st.branch_ref in names else -1
    index = _step(view, position, len(names), delta)
    if index is None or index == position:
        return
    st.branch_ref = names[index]
    _paint_selection(view, st)
    render._header(view, st, header_html(st))
    st.highlight += 1
    ticket = st.highlight

    def fetch() -> None:
        """Load the log unless another selection came since"""
        if ticket == st.highlight and view.is_valid():
            _load_log(view, st)

    sublime.set_timeout(fetch, DEBOUNCE_MS)


# --- drawing ----------------------------------------------------------------


def _standing_parts(ref: Ref) -> List[Tuple[str, str]]:
    """How a local branch stands against its upstream, as (text, "ins" | "del") parts: ahead green, behind or gone red;
    zero counts are left out, and a level or untracked branch has none"""
    if ref.gone:
        return [("gone", "del")]
    parts = [(f"\u2191{ref.ahead}", "ins")] if ref.ahead else []
    if ref.behind:
        parts.append((f"\u2193{ref.behind}", "del"))
    return parts


def _standing(ref: Ref) -> str:
    """The standing as the left column shows it: "↑1 ↓2", "gone", or """""
    return " ".join(text for text, _ in _standing_parts(ref))


def _standing_html(ref: Ref) -> str:
    """The standing for the header, or "up to date" when there is nothing to say"""
    parts = _standing_parts(ref)
    if not parts:
        return '<span class="state">up to date</span>'
    return " ".join(f'<span class="{kind}">{text}</span>' for text, kind in parts)


def _left_cells(st: ViewState) -> List[LeftCell]:
    """(text, branch, standing) per row of the left column; headings and remote names carry no branch"""
    cells: List[LeftCell] = [(HEADING_BRANCHES, None, "")]
    for ref in st.branch_refs:
        if ref.remote is None:
            cells.append((("* " if ref.current else "  ") + ref.name, ref, _standing(ref)))
    cells.append((HEADING_REMOTES, None, ""))
    # a remote keeps its row even before anything was fetched from it
    remotes = list(dict.fromkeys(list(st.branch_remotes) + [r.remote for r in st.branch_refs if r.remote]))
    for remote in remotes:
        cells.append((remote, None, ""))
        cells.extend(("  " + ref.branch, ref, "") for ref in st.branch_refs if ref.remote == remote)
    return cells


def _left_width(cells: List[LeftCell]) -> int:
    """Width of the left column for its cells: the longest (with its standing and a space) plus padding, capped"""
    return min(LEFT_MAX, max(len(text) + (len(standing) + 1 if standing else 0) for text, _, standing in cells) + LEFT_PAD)


def _clip(text: str, width: int) -> str:
    """`text` cut to `width` characters with an ellipsis"""
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _standing_spans(ref: Ref, width: int) -> List[Span]:
    """The spans colouring a local branch's standing on the right edge of its cell `width` wide"""
    spans: List[Span] = []
    at = _standing_room(width, _standing(ref))
    for text, kind in _standing_parts(ref):
        spans.append((at, at + len(text), kind))
        at += len(text) + 1
    return spans


def _standing_room(width: int, standing: str) -> int:
    """Where a standing starts in a cell `width` wide: on the right edge, before the trailing space"""
    return width - 1 - len(standing)


def _left_text(cell: LeftCell, width: int) -> str:
    """The cell padded to `width` with its standing on the right edge"""
    text, _, standing = cell
    if not standing:
        return f"{_clip(text, width - 1):<{width}}"
    # the name gives way first: the counts are what the column is for
    room = _standing_room(width, standing)
    return f"{_clip(text, room - 1):<{room}}{standing} "


def _right_width(view: sublime.View, left_width: int) -> int:
    """Columns left for the right column after the left one and the gap, within sane bounds"""
    try:
        columns = int(view.viewport_extent()[0] / (view.em_width() or 8))
    except Exception:
        columns = 0
    return max(RIGHT_MIN, min(RIGHT_MAX, columns - left_width - len(COLUMN_GAP)))


def _stats_cell(files: Optional[int], insertions: Optional[int], deletions: Optional[int]) -> Tuple[str, List[Span]]:
    """A Stats row ("Stats:  3 files changed -5 +12") with the spans colouring its counts; no diffstat when `files` is None"""
    stats = f"{'Stats:':<{LABEL_WIDTH}}"
    spans: List[Span] = []
    if files is None:
        return stats + "(no diffstat)", spans
    stats += f"{file_count(files)} changed "
    dele, ins = f"-{deletions}", f"+{insertions}"
    spans.append((len(stats), len(stats) + len(dele), "del"))
    stats += dele + " "
    spans.append((len(stats), len(stats) + len(ins), "ins"))
    return stats + ins, spans


def _worktree_cells(changes: List[FileChange], width: int) -> List[Cell]:
    """The working-tree entry: the staged / unstaged file counts and their diffstat, or "No Changes", padded to five rows"""
    if not changes:
        head, stats, spans = NO_CHANGES, "", []
    else:
        stat = git.worktree_stats(changes)
        head = f"{file_count(stat.staged, 'staged')}, {file_count(stat.unstaged, 'unstaged')}"
        stats, spans = _stats_cell(stat.files, stat.insertions, stat.deletions)
    return [(head, WORKTREE, []), (stats, WORKTREE, spans), ("", WORKTREE, []), ("", WORKTREE, []), (RULE * width, WORKTREE, [])]


def _right_cells(changes: List[FileChange], commits: List[Commit], width: int) -> List[Cell]:
    """The right column: the working-tree entry, then five rows per commit, with the spans of the tags and counts to colour"""
    cells = _worktree_cells(changes, width)
    for i, c in enumerate(commits, 1):
        author = f"{'Author:':<{LABEL_WIDTH}}{c.author}"
        author_spans: List[Span] = []
        if c.tags:
            # the tags on the right edge, one space at least from the author
            tags = " ".join(c.tags)
            author = f"{author:<{max(len(author) + 1, width - len(tags))}}{tags}"
            author_spans.append((len(author) - len(tags), len(author), "ins"))
        stats, stats_spans = _stats_cell(c.files, c.insertions, c.deletions)
        cells.append((c.subject or "(no subject)", i, []))
        cells.append((author, i, author_spans))
        cells.append((f"{'Date:':<{LABEL_WIDTH}}{c.date}", i, []))
        cells.append((stats, i, stats_spans))
        cells.append((RULE * width, i, []))
    if not commits:
        cells.append(("(no commits)", -1, []))
    return cells


def _item_count(st: ViewState) -> int:
    """How many items the right column holds: the working tree and the commits"""
    return len(st.branch_commits) + 1


def _caret_commit(view: sublime.View, st: ViewState) -> int:
    """Index of the item whose rows hold the caret (WORKTREE or a commit's), -1 off them"""
    row = render.caret_index(view)
    if 0 <= row < len(st.branch_rows):
        return st.branch_rows[row].commit
    return -1


class Paint(NamedTuple):
    """What the painters need from the last render, worked out once so a j / k or J / K repaint asks Sublime nothing

    `starts` / `ends` are each row's text points, `spans` the right column's coloured (start, end, kind) points
    per row, `standings` the left column's, and `scopes` the scope names resolved for this render
    """

    starts: List[int]
    ends: List[int]
    spans: List[List[Span]]
    standings: List[List[Span]]
    scopes: Dict[str, Optional[str]]


def _paint(view: sublime.View, st: ViewState, rows: List[str]) -> Paint:
    """The geometry of `rows` as drawn, and the scopes the scheme answers for now"""
    starts = render._row_starts_for_text(rows)
    spans = [[(start + a, start + b, kind) for a, b, kind in row.spans] for start, row in zip(starts, st.branch_rows)]
    standings = [[(start + a, start + b, kind) for a, b, kind in _standing_spans(row.ref, st.branch_width)]
                 if row.ref is not None and row.ref.remote is None else []
                 for start, row in zip(starts, st.branch_rows)]
    scopes: Dict[str, Optional[str]] = {
        "sel": render._file_header_scope(view), "cursor": palette.cursor_scope(view),
        "del": palette.text_scope(view, "del"), "ins": palette.text_scope(view, "ins"),
        "sel.del": palette.count_scope(view, "del"), "sel.ins": palette.count_scope(view, "ins"),
        "cursor.del": palette.cursor_text_scope(view, "del"), "cursor.ins": palette.cursor_text_scope(view, "ins"),
    }
    return Paint(starts, [start + len(row) for start, row in zip(starts, rows)], spans, standings, scopes)


def _add(view: sublime.View, key: str, regions: List[sublime.Region], scope: Optional[str]) -> None:
    """Fill `regions` under `key` with `scope`'s colours"""
    view.add_regions(key, regions, scope or "", flags=sublime.DRAW_NO_OUTLINE)


def _paint_cursor(view: sublime.View, st: ViewState, index: int) -> None:
    """Tint the right-column cells of item `index` (subject to stats) as the commit cursor, once the scope is live,
    and colour the counts and tags of every row: on the cursor's rows on its background, elsewhere on the editor's

    No two regions overlap, as a region painted later repaints the text under it in its own colour. Until the
    cursor scope is live Sublime's own line highlight marks the caret row across both columns, so it stays on
    """
    paint = st.branch_paint
    assert paint is not None
    scope = paint.scopes["cursor"]
    view.settings().set("highlight_line", scope is None)
    first, last = index * ROWS_PER_COMMIT, index * ROWS_PER_COMMIT + ROWS_PER_COMMIT - 1  # the rule row stays untinted
    cursor = scope is not None and 0 <= index < _item_count(st)
    gap = st.branch_width + len(COLUMN_GAP)
    tint: List[sublime.Region] = []
    coloured: Dict[str, List[sublime.Region]] = {"del": [], "ins": [], "cursor.del": [], "cursor.ins": []}
    for i, spans in enumerate(paint.spans):
        on_cursor = cursor and first <= i < last
        at = paint.starts[i] + gap
        for a, b, kind in spans:
            if on_cursor:
                tint.append(sublime.Region(at, a))
            coloured["cursor." + kind if on_cursor else kind].append(sublime.Region(a, b))
            at = b
        if on_cursor:
            tint.append(sublime.Region(at, paint.ends[i]))
    _add(view, render.KEY_BRANCH_DEL, coloured["del"], paint.scopes["del"])
    _add(view, render.KEY_BRANCH_INS, coloured["ins"], paint.scopes["ins"])
    if not cursor:
        for key in (render.KEY_BRANCH_CURSOR, render.KEY_BRANCH_CURSOR_DEL, render.KEY_BRANCH_CURSOR_INS):
            view.erase_regions(key)
        return
    _add(view, render.KEY_BRANCH_CURSOR, tint, scope)
    _add(view, render.KEY_BRANCH_CURSOR_DEL, coloured["cursor.del"], paint.scopes["cursor.del"])
    _add(view, render.KEY_BRANCH_CURSOR_INS, coloured["cursor.ins"], paint.scopes["cursor.ins"])


def _paint_selection(view: sublime.View, st: ViewState) -> None:
    """Tint the selected branch's cell, or nothing when none is listed, and colour the branches' standings

    The standings are filled regions, as only those take a scope's text colour, so the selected row's
    carry its background rather than the editor's; a change of selection repaints them all
    """
    paint = st.branch_paint
    assert paint is not None
    selected: List[sublime.Region] = []
    standings: Dict[str, List[sublime.Region]] = {"del": [], "ins": [], "sel.del": [], "sel.ins": []}
    for i, row in enumerate(st.branch_rows):
        chosen = row.ref is not None and row.ref.name == st.branch_ref
        if chosen:
            selected.append(sublime.Region(paint.starts[i], paint.starts[i] + st.branch_width))
        for a, b, kind in paint.standings[i]:
            standings["sel." + kind if chosen else kind].append(sublime.Region(a, b))
    _add(view, render.KEY_BRANCH_SEL, selected, paint.scopes["sel"])
    _add(view, render.KEY_BRANCH_STANDING_DEL, standings["del"], paint.scopes["del"])
    _add(view, render.KEY_BRANCH_STANDING_INS, standings["ins"], paint.scopes["ins"])
    _add(view, render.KEY_BRANCH_SEL_DEL, standings["sel.del"], paint.scopes["sel.del"])
    _add(view, render.KEY_BRANCH_SEL_INS, standings["sel.ins"], paint.scopes["sel.ins"])


def _put_caret(view: sublime.View, st: ViewState, index: int, show: bool) -> None:
    """Put the caret on item `index`'s first row and tint the item; `show` scrolls it into view"""
    point = render.index_point(view, index * ROWS_PER_COMMIT)
    view.sel().clear()
    view.sel().add(sublime.Region(point))
    _paint_cursor(view, st, index)
    if show:
        view.show(point)


def render_branches(view: sublime.View, st: ViewState, keep_caret: bool = False) -> None:
    """Draw the two columns, tint the selection, and put the caret on an item's first row

    `keep_caret` leaves the cursor on the item it was on and keeps the scroll; otherwise the
    working-tree entry is the cursor and the view scrolls to the top
    """
    commit = _caret_commit(view, st) if keep_caret else -1
    viewport = view.viewport_position() if keep_caret else (0, 0)
    # the cells of both columns, then rows of the longer column's length
    left = _left_cells(st)
    width = st.branch_width = _left_width(left)
    right_width = _right_width(view, width)
    right = _right_cells(st.branch_changes, st.branch_commits, right_width)
    rows: List[str] = []
    metadata: List[BranchRow] = []
    gap = width + len(COLUMN_GAP)
    for i in range(max(len(left), len(right))):
        left_text = _left_text(left[i], width) if i < len(left) else " " * width
        cell, index, spans = right[i] if i < len(right) else ("", -1, [])
        rows.append(left_text + (JUNCTION if cell == RULE * right_width else COLUMN_GAP) + cell)
        # the spans relative to the row's start, for the cursor painter to colour
        metadata.append(BranchRow(left[i][1] if i < len(left) else None, index, tuple((gap + a, gap + b, kind) for a, b, kind in spans)))
    st.branch_rows = metadata
    render._replace(view, "\n".join(rows))
    view.assign_syntax("scope:text.plain")
    palette.ensure(view)
    if palette.claim_restart_notice(view, palette.BRANCH_SCOPES, tokens=False):
        status(render.NEEDS_RESTART_BRANCHES)
    st.branch_paint = _paint(view, st, rows)
    # the selection and, with it, the standings; the tags and counts are painted with the cursor, by _put_caret
    _paint_selection(view, st)
    render._finish(view, st, MODE_BRANCHES, "⟐ branches", header_html(st), fingerprint)
    # the caret: on the kept item's first row, else on the working-tree entry
    if not 0 <= commit < _item_count(st):
        commit = WORKTREE
    _put_caret(view, st, commit, show=False)
    view.set_viewport_position(viewport, False)
    if keep_caret:
        view.show(render.index_point(view, commit * ROWS_PER_COMMIT))


def fingerprint(st: ViewState) -> Tuple[Any, ...]:
    """Cheap "did anything change" key: the refs' mtimes (what a checkout, commit, fetch, push or branch change
    touches), then the working-tree entry's, so a refresh can tell which half moved"""
    refs = tuple(git.mtime(path) for path in git.ref_paths(st.repo))
    return refs, render.worktree_fingerprint(st.repo, st.branch_changes)


def refresh_if_changed(view: sublime.View, st: ViewState) -> None:
    """Reload when the refs changed since the view was drawn, or relist only the working tree when only it did"""
    # not while a load is in flight: its result would only be thrown away
    if st.token != st.rendered_token:
        return
    now = fingerprint(st)
    if now == st.fingerprint:
        return
    if st.fingerprint and now[0] == st.fingerprint[0]:
        # a save, something staged: the branches and the log stand
        load_worktree(view, st)
    else:
        load(view, st, keep_caret=True)


def header_html(st: ViewState) -> str:
    """The minihtml header: the repository and HEAD, then the selected branch, its upstream and the action links"""
    current = st.current_branch()
    if current is not None:
        where = f'on <span class="hash">{html.escape(current.name)}</span>'
    elif st.branch_head_sha:
        where = f'detached at <span class="hash">{html.escape(st.branch_head_sha)}</span>'
    else:
        where = "no commits"
    summary = f'<div class="summary">Branches · {html.escape(os.path.basename(st.repo))} · {where}</div>'
    ref = st.selected_ref()
    if ref is None:
        title = '<span class="path">no branch selected</span>'
    elif ref.remote is not None:
        title = f'<span class="path">{html.escape(ref.name)}</span>&nbsp;&nbsp;<span class="when">remote branch of {html.escape(ref.remote)}</span>'
    else:
        title = f'<span class="path">{html.escape(ref.name)}</span>'
        if ref.upstream:
            title += f'&nbsp;&nbsp;<span class="when">→ {html.escape(ref.upstream)}</span>&nbsp;&nbsp;{_standing_html(ref)}'
    actions: List[str] = []
    if ref is not None:
        actions.append('<a href="checkout">Checkout</a>')
        if ref.remote is not None:
            actions.append('<a href="checkout_detached">Detached</a>')
        else:
            actions.append('<a href="pull">Pull</a>')
            actions.append('<a href="push">Push</a>')
    actions.append('<a href="fetch">Fetch</a>')
    if ref is not None and ref.remote is None:
        actions.append('<a href="delete_branch" class="danger">Delete</a>')
    return render.header_frame(summary, title, actions)


# --- actions ----------------------------------------------------------------


def _selected(st: ViewState, local_verb: Optional[str] = None) -> Optional[Ref]:
    """The selected branch, with a status message when there is none; with `local_verb`, a remote one does not count either"""
    ref = st.selected_ref()
    if ref is None:
        status("no branch selected")
    elif local_verb and ref.remote is not None:
        status(f"{local_verb} works on a local branch, select one")
        return None
    return ref


def _act_checkout(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Check out the selected branch; a remote one as the same-named local branch, created tracking it"""
    ref = _selected(st)
    if ref is None:
        return
    if ref.current:
        status(f"already on {ref.name}")
        return
    locals_ = [r.branch for r in st.branch_refs if r.remote is None]

    def done(_: None) -> None:
        """Select the branch now checked out and relist"""
        st.branch_ref = ref.branch
        load(view, st, f"checked out {ref.branch}")

    run_async(lambda: git.checkout(st.repo, ref, locals_), done)


def _act_checkout_detached(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Check out the selected remote branch with HEAD detached"""
    ref = _selected(st)
    if ref is None:
        return
    if ref.remote is None:
        status("detached checkout is for remote branches, press Enter for a local one")
        return
    run_async(lambda: git.checkout_detached(st.repo, ref.name), lambda _: load(view, st, f"checked out {ref.name} detached"))


def _act_pull(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Pull the checked-out branch, or fast-forward another local branch from its upstream"""
    ref = _selected(st, "pull")
    if ref is None:
        return
    if ref.current:
        status(f"pulling {ref.name}…")
        run_async(lambda: git.pull(st.repo), lambda _: load(view, st, f"pulled {ref.name}"))
        return
    if not ref.upstream or ref.gone:
        status(f"{ref.name} has no upstream to pull from")
        return
    remote, remote_branch = ref.upstream_remote, ref.upstream_branch
    if remote is None or remote_branch is None:
        status(f"{ref.name} tracks the local branch {ref.upstream}, pull works with a remote upstream")
        return
    status(f"fast-forwarding {ref.name} from {ref.upstream}…")
    run_async(lambda: git.fetch_into(st.repo, remote, remote_branch, ref.name),
              lambda _: load(view, st, f"fast-forwarded {ref.name} to {ref.upstream}"))


def _act_push(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Push the selected local branch to its upstream, or to its remote (origin by default), recording that as the upstream"""
    ref = _selected(st, "push")
    if ref is None:
        return
    if ref.upstream and not ref.gone:
        # to the upstream itself, whatever it is called there
        remote, remote_branch = ref.upstream_remote, ref.upstream_branch
        if remote is None:
            status(f"{ref.name} tracks the local branch {ref.upstream}, there is no remote to push to")
            return
        work = lambda: git.push(st.repo, remote, ref.name, remote_branch=remote_branch)  # noqa: E731
    else:
        remotes = st.branch_remotes
        if not remotes:
            status("no remote to push to")
            return
        remote = "origin" if "origin" in remotes else remotes[0]
        work = lambda: git.push(st.repo, remote, ref.name, set_upstream=True)  # noqa: E731
    status(f"pushing {ref.name} to {remote}…")
    run_async(work, lambda _: load(view, st, f"pushed {ref.name} to {remote}"))


def _act_fetch(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Fetch every remote, pruning, and relist"""
    status("fetching all remotes…")
    run_async(lambda: git.fetch_all(st.repo), lambda _: load(view, st, "fetched all remotes", keep_caret=True))


def _act_delete(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Delete the selected local branch after a confirmation; a second one forces an unmerged branch"""
    ref = _selected(st, "delete")
    if ref is None:
        return
    if ref.current:
        status(f"{ref.name} is checked out, switch away before deleting it")
        return
    if not sublime.ok_cancel_dialog(f"Delete the branch {ref.name}?", "Delete"):
        return

    def work() -> Optional[GitError]:
        """Delete, handing back the refusal of an unmerged branch for the second question"""
        try:
            git.delete_branch(st.repo, ref.name)
        except GitError as error:
            if "not fully merged" not in error.stderr:
                raise
            return error
        return None

    def done(refusal: Optional[GitError]) -> None:
        """Relist, or offer to force the deletion"""
        if refusal is None:
            load(view, st, f"deleted {ref.name}")
            return
        if sublime.ok_cancel_dialog(f"{ref.name} is not fully merged.\n\nDelete it anyway? This cannot be undone.", "Delete anyway"):
            run_async(lambda: git.delete_branch(st.repo, ref.name, force=True), lambda _: load(view, st, f"deleted {ref.name}"))

    run_async(work, done)


def _step_branch(delta: int) -> Action:
    """Action moving the branch selection `delta` branches on"""
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Select the neighbouring branch"""
        select(view, st, delta)

    return act


def _step_commit(delta: int) -> Action:
    """Action moving the caret `delta` items on, ringing at the ends"""
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Put the caret on the neighbouring item's first row"""
        # off the items (a left-column or blank row) j goes to the first, k to the last
        index = _step(view, _caret_commit(view, st), _item_count(st), delta)
        if index is not None:
            _put_caret(view, st, index, show=True)

    return act


def _act_open_commit(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Review the commit under the caret in the commit view, or the changed files from the working-tree entry"""
    index = _caret_commit(view, st)
    if not 0 <= index < _item_count(st):
        status("no commit under the caret")
        return
    if index == WORKTREE:
        window.run_command("vdiff_files")
        return
    window.run_command("vdiff_commits", {"sha": st.branch_commits[index - 1].sha})


def _in_branches(act: Action) -> Action:
    """`act` guarded to the branches view: header links and the popup can fire it over a diff too"""
    def guarded(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Run the action, or say where it works"""
        if st.in_branches:
            act(view, window, st)
        else:
            status(NOT_HERE)

    return guarded


ACTIONS: Dict[str, Action] = {
    name: _in_branches(act)
    for name, act in {
        "checkout": _act_checkout,
        "checkout_detached": _act_checkout_detached,
        "pull": _act_pull,
        "push": _act_push,
        "fetch": _act_fetch,
        "delete_branch": _act_delete,
        "next_branch": _step_branch(1),
        "prev_branch": _step_branch(-1),
        "next_commit": _step_commit(1),
        "prev_commit": _step_commit(-1),
        "open_commit": _act_open_commit,
    }.items()
}
