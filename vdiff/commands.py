"""Commands: the two quick panels (changed files, recent commits) and the actions of a review view"""
import bisect
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import sublime
import sublime_plugin

from . import git, parse, render, state
from .git import Commit, FileChange
from .state import Kept, ViewState
from .utils import file_count, run_async, settings, status

# an ACTIONS entry: act(view, window, state) on the review view it fired in
Action = Callable[[sublime.View, sublime.Window, ViewState], None]

# quick-panel kind per status letter: colour + the letter; the description comes from git.STATUS_NAMES
STATUS_COLOURS: Dict[str, int] = {
    "M": sublime.KIND_ID_COLOR_ORANGISH,
    "A": sublime.KIND_ID_COLOR_GREENISH,
    "?": sublime.KIND_ID_COLOR_GREENISH,
    "D": sublime.KIND_ID_COLOR_REDISH,
    "R": sublime.KIND_ID_COLOR_BLUISH,
    "C": sublime.KIND_ID_COLOR_BLUISH,
    "T": sublime.KIND_ID_COLOR_PURPLISH,
    "U": sublime.KIND_ID_COLOR_REDISH,
}
COMMIT_KIND = (sublime.KIND_ID_COLOR_BLUISH, "c", "commit")

# row width shared by subject and annotation; the subject is clipped to fit, never below MIN_SUBJECT_WIDTH
COMMIT_ROW_WIDTH = 120
MIN_SUBJECT_WIDTH = 24

# highlight-to-preview delay: holding an arrow key must not spawn a git process per row
PREVIEW_DELAY_MS = 60

# cache directory -> repo root: resolve_repo runs git on the UI thread
_roots: Dict[str, str] = {}


def resolve_repo(window: sublime.Window) -> Optional[str]:
    """Repository of the review view or active file, else of the first project folder that is one"""
    view = window.active_view()
    candidates: List[str] = []
    # a review view already knows its repo; a file's directory is tried before the project folders
    if view is not None:
        if state.is_review_view(view):
            st = state.get(view) or restore_view(view)
            if st.repo:
                return st.repo
        else:
            name = view.file_name()
            if name:
                candidates.append(os.path.dirname(name))
    candidates.extend(window.folders())
    # first candidate in a repo wins; only hits are cached
    for directory in candidates:
        root = _roots.get(directory)
        if root is None:
            root = git.repo_root(directory)
            if root:
                _roots[directory] = root
        if root:
            return root
    return None


def find_review_view(window: sublime.Window) -> Optional[sublime.View]:
    """The window's first review view, if it has one"""
    for view in window.views():
        if state.is_review_view(view):
            return view
    return None


def open_review(window: sublime.Window) -> Optional[Tuple[sublime.View, ViewState]]:
    """Focus the window's review view (created if missing) on the resolved repo; (view, state), or None without a repo"""
    repo = resolve_repo(window)
    if not repo:
        status("no git repository for the active file or project folders")
        return None
    view = find_review_view(window)
    if view is None:
        view = window.new_file()
        render.init_view(view)
    st = state.get(view)
    if st is None or st.repo != repo:
        # a different repository: browsing state no longer applies
        st = state.put(view, state.ViewState(repo))
    window.focus_view(view)
    return view, st


def show_all(view: sublime.View, st: ViewState, focus: Optional[str] = None) -> None:
    """Load all file diffs off the UI thread and switch to the aggregate view, on `focus`'s heading if given"""
    st.all_mode = True
    # bump the token so earlier loads drop their results
    st.token += 1
    token, files = st.token, list(st.files)
    def work() -> Dict[str, Tuple[List[parse.Line], List[parse.Block], bool]]:
        """Parse every listed file's diff and its change blocks, keyed by path"""
        result = {}
        for change in files:
            diff = parse.parse_unified(git.full_diff(st.repo, change.path, sha=st.sha, untracked=(change.status == "?")))
            result[change.path] = (diff.lines, parse.change_blocks(diff.lines), diff.binary)
        return result
    def done(result: Dict[str, Tuple[List[parse.Line], List[parse.Block], bool]]) -> None:
        """Store the diffs and draw the aggregate view unless superseded or closed"""
        if token == st.token and view.is_valid():
            st.set_all_diffs(result)
            if st.change is None and files:
                st.change = files[0]
            render.render_all(view, st, focus=focus)
    run_async(work, done)


def show_file(view: sublime.View, st: ViewState, change: FileChange) -> None:
    """Fetch and parse the file's diff on the worker thread, then render it"""
    st.all_mode = False
    st.token += 1
    # set before the worker starts: n / p count from here while the diff is still loading
    st.requested = change
    token = st.token
    repo, sha = st.repo, st.sha

    def work() -> Optional[Tuple[parse.Diff, List[parse.Block]]]:
        """Run git diff and parse it, unless a newer request already superseded this one"""
        if token != st.token:
            return None  # superseded before it started: skip the subprocess
        text = git.full_diff(repo, change.path, sha=sha, untracked=(change.status == "?"))
        diff = parse.parse_unified(text)
        return diff, parse.change_blocks(diff.lines)

    def done(result: Optional[Tuple[parse.Diff, List[parse.Block]]]) -> None:
        """Make the file current and render it if this is still the latest request"""
        if result is None or token != st.token or not view.is_valid():
            return
        diff, blocks = result
        st.change = change
        st.lines, st.blocks, st.binary = diff.lines, blocks, diff.binary
        render.render(view, st)
        st.rendered_token = token

    run_async(work, done)


def file_items(files: Sequence[FileChange]) -> List[sublime.QuickPanelItem]:
    """Quick-panel rows: path; status, staged flag, rename source; line totals or binary"""
    items: List[sublime.QuickPanelItem] = []
    for change in files:
        description = git.STATUS_NAMES.get(change.status, "")
        colour = STATUS_COLOURS.get(change.status)
        kind = (colour, change.status, description) if colour else sublime.KIND_AMBIGUOUS
        details = ", ".join(filter(None, (description, "staged" if change.staged else "")))
        if change.old_path:
            details += "  ← " + change.old_path
        # no line counts means git saw a binary file
        if None in (change.insertions, change.deletions):
            annotation = "binary"
        else:
            annotation = f"-{change.deletions} +{change.insertions}"
        items.append(sublime.QuickPanelItem(change.path, details, annotation, kind))
    return items


def commit_items(commits: Sequence[Commit]) -> List[sublime.QuickPanelItem]:
    """Quick-panel rows: subject, author · date, and diffstat plus short hash right-aligned"""
    items: List[sublime.QuickPanelItem] = []
    for c in commits:
        # merges and empty commits have no diffstat, so they show the hash alone
        annotation = c.short
        if c.files is not None:
            annotation = f"{file_count(c.files)} changed  -{c.deletions} +{c.insertions}  {c.short}"
        # clip the subject so the annotation keeps its room
        subject = c.subject or "(no subject)"
        budget = max(COMMIT_ROW_WIDTH - len(annotation), MIN_SUBJECT_WIDTH)
        if len(subject) > budget:
            subject = subject[: budget - 2] + ".."
        items.append(sublime.QuickPanelItem(subject, f"{c.author} · {c.date}", annotation, COMMIT_KIND))
    return items


def show_files_panel(window: sublime.Window, view: sublime.View, st: ViewState, files: List[FileChange],
                     kept: Kept, on_cancel: Optional[Callable[[], None]] = None,
                     placeholder: str = "Changed files") -> None:
    """Quick panel over `files`: highlight previews, Enter shows, Esc restores `kept` (then runs on_cancel)"""
    # the panel opens on the shown file, the view focused behind it
    st.files = files
    selected = max(0, st.index())
    window.focus_view(view)

    def on_select(index: int) -> None:
        """Enter shows the chosen file; Esc restores `kept`, then runs on_cancel if given"""
        st.highlight += 1  # cancel a pending preview
        if index == -1:
            if on_cancel is not None:
                st.restore(kept)
                on_cancel()
            elif kept.change is not None:
                # reshow the kept file
                st.restore(kept)
                show_file(view, st, kept.change)
            # nothing was kept before: the last previewed file stays
            return
        change = files[index]
        if st.change is change and st.rendered_token == st.token:
            return  # the preview already shows it
        show_file(view, st, change)

    def on_highlight(index: int) -> None:
        """Schedule a debounced preview of the highlighted file"""
        if not 0 <= index < len(files):
            return
        st.highlight += 1
        ticket = st.highlight

        def preview() -> None:
            """Show the file unless another highlight came since"""
            if ticket == st.highlight:
                show_file(view, st, files[index])

        sublime.set_timeout(preview, PREVIEW_DELAY_MS)

    window.show_quick_panel(file_items(files), on_select, 0, selected, on_highlight, placeholder)


def refresh_listing(view: sublime.View, st: ViewState, message: Optional[str] = None,
                    pick: Optional[Callable[[List[FileChange], int], Optional[FileChange]]] = None) -> None:
    """Re-list the working tree changes and show the same file, or the next one when it is gone

    `pick(files, index)` may choose another file; `index` is the shown file's new slot, or its old one if it left the list
    """
    if message:
        status(message)
    # remember the shown file and its slot before the list changes
    previous_index = max(0, st.index())
    path = st.change.path if st.change else None

    def done(files: List[FileChange]) -> None:
        """Show the same file again, its slot's successor, or what `pick` chooses"""
        if not view.is_valid():
            return
        st.files = files
        if not files:
            render.render_message(view, st, f"No uncommitted changes in {st.repo}")
            return
        index = state.find_change(files, path)
        if index < 0:
            # the file left the list: whatever moved up into its slot takes over
            index = min(previous_index, len(files) - 1)
        chosen = pick(files, index) if pick else None
        change = chosen if chosen is not None else files[index]
        if st.all_mode:
            # stay in the aggregate view, re-diffed over the new list, on the same file
            st.change = change
            show_all(view, st, focus=change.path)
        else:
            show_file(view, st, change)

    run_async(lambda: git.changed_files(st.repo), done)


def refresh_if_changed(view: sublime.View, st: ViewState) -> None:
    """Re-render a working-tree view when the file or the index changed since it was drawn"""
    if st.in_commit or st.change is None:
        return
    if render.fingerprint(st) != st.fingerprint:
        refresh_listing(view, st)


class VdiffFilesCommand(sublime_plugin.WindowCommand):
    """List uncommitted changes (staged, unstaged, untracked) in a quick panel"""

    def run(self) -> None:
        """Open the review view, then list the changed files off the UI thread"""
        window = self.window
        opened = open_review(window)
        if not opened:
            return
        view, st = opened

        def done(files: List[FileChange]) -> None:
            """Show the files panel, or a notice when there is nothing uncommitted"""
            if not view.is_valid():
                return
            kept = st.enter()
            if not files:
                render.render_message(view, st, f"No uncommitted changes in {st.repo}")
                status("no uncommitted changes")
                return
            placeholder = f"Changed files in {os.path.basename(st.repo)}"
            show_files_panel(window, view, st, files, kept, placeholder=placeholder)

        run_async(lambda: git.changed_files(st.repo), done)


class VdiffCommitsCommand(sublime_plugin.WindowCommand):
    """List recent commits, then the files of the chosen commit; `sha` skips straight to one commit"""

    def run(self, selected: int = 0, sha: Optional[str] = None) -> None:
        """Open the review view, then the given commit or the recent-commits panel"""
        opened = open_review(self.window)
        if not opened:
            return
        view, st = opened
        if sha:
            self.open_commit(view, st, sha, None)
            return
        count = int(settings().get("commits_count", 30) or 30)

        def done(commits: List[Commit]) -> None:
            """Show the commits panel unless the repository has none"""
            if not view.is_valid():
                return
            if not commits:
                status("no commits yet")
                return
            self.show_commits(view, st, commits, selected)

        run_async(lambda: git.recent_commits(st.repo, count), done)

    def show_commits(self, view: sublime.View, st: ViewState, commits: Sequence[Commit], selected: int) -> None:
        """Recent-commits panel, preselecting the commit already shown"""
        items = commit_items(commits)
        for i, c in enumerate(commits):
            if c.sha == st.sha:
                selected = i

        def on_select(index: int) -> None:
            """Open the chosen commit's files, remembering its row for Esc"""
            if index != -1:
                self.open_commit(view, st, commits[index].sha, index)

        self.window.show_quick_panel(items, on_select, 0, selected, None, "Recent commits")

    def open_commit(self, view: sublime.View, st: ViewState, sha: str, index: Optional[int]) -> None:
        """Files panel of one commit; with `index`, Esc reopens the commit list on that row"""
        window = self.window

        def done(result: Tuple[git.CommitInfo, List[FileChange]]) -> None:
            """Enter the commit and show its files panel, or a notice when it touches none"""
            if not view.is_valid():
                return
            info, files = result
            kept = st.enter(info.sha, info)
            if not files:
                render.render_message(view, st, f"Commit {info.short} touches no files")
                return

            def back() -> None:
                """Reopen the commit list on the row this commit came from"""
                window.run_command("vdiff_commits", {"selected": index})

            placeholder = f"{info.short} · {info.subject}"
            on_cancel = back if index is not None else None
            show_files_panel(window, view, st, files, kept, on_cancel=on_cancel, placeholder=placeholder)

        run_async(lambda: git.commit_with_files(st.repo, sha), done)


# --- review view actions ----------------------------------------------------


def _act_files(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Open the changed-files panel"""
    window.run_command("vdiff_files")


def _act_commits(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Open the recent-commits panel"""
    window.run_command("vdiff_commits")


def _act_close(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Close the review view"""
    view.close()


def _act_help(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Show the help popup"""
    render.show_help(view, st)


def _git_action(verb: str, done_verb: str, operation: Callable[[str, FileChange], None],
                confirm: Optional[Callable[[FileChange], bool]] = None,
                pick: Optional[Callable[[List[FileChange], int], Optional[FileChange]]] = None) -> Action:
    """Action running `operation(repo, change)` on the current working-tree file, then re-listing; `pick` is passed to refresh_listing"""

    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Confirm if asked, run the operation off the UI thread, then refresh the listing"""
        change = _current_change(view, st)
        if st.in_commit or change is None:
            status(f"{verb} works on uncommitted changes only")
            return
        if confirm is not None and not confirm(change):
            return
        run_async(
            lambda: operation(st.repo, change),
            lambda _: refresh_listing(view, st, f"{done_verb} {change.path}", pick=pick),
        )

    return act


def _bulk_action(verb: str, done_verb: str, operation: Callable[[str], None],
                 wanted: Callable[[FileChange], bool]) -> Action:
    """Action running `operation(repo)` ("<verb> all") when some listed file is `wanted`, then re-listing on the same file"""

    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Run the operation off the UI thread, then refresh the listing"""
        if st.in_commit:
            status(f"{verb} all works on uncommitted changes only")
            return
        count = sum(1 for c in st.files if wanted(c))
        if not count:
            status(f"nothing to {verb}")
            return
        run_async(
            lambda: operation(st.repo),
            lambda _: refresh_listing(view, st, f"{done_verb} {file_count(count)}"),
        )

    return act


def _confirm_discard(change: FileChange) -> bool:
    """Ask before discarding or deleting, unless the confirm_discard setting is off"""
    if not settings().get("confirm_discard", True):
        return True
    # untracked files are deleted outright, tracked ones reset
    if change.status == "?":
        message = f"Delete the untracked file {change.path}?\n\nThis cannot be undone."
    else:
        message = (
            f"Discard all changes to {change.path}?\n\n"
            "The file goes back to its last committed version. This cannot be undone."
        )
    return sublime.ok_cancel_dialog(message, "Discard")


def _next_unstaged(files: Sequence[FileChange], start: int) -> int:
    """Index of the first unstaged file from `start` on, wrapping round; -1 when every file is staged"""
    for offset in range(len(files)):
        index = (start + offset) % len(files)
        if not files[index].staged:
            return index
    return -1


def _stage_next(files: Sequence[FileChange], index: int) -> Optional[FileChange]:
    """After staging, the next unstaged file to show; None keeps the file the refresh would show"""
    if not settings().get("stage_and_move_next_file", True):
        return None
    # scanning from the staged file's slot is safe: it is skipped as staged
    following = _next_unstaged(files, index)
    return files[following] if following >= 0 else None


def _act_refresh(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Reload what is shown: all diffs, the commit's file, or the working-tree listing"""
    if st.all_mode:
        show_all(view, st)
        return
    if st.change is None:
        return
    if st.in_commit:
        show_file(view, st, st.change)
    else:
        refresh_listing(view, st)


def _current_change(view: sublime.View, st: ViewState) -> Optional[FileChange]:
    """File under the caret in the aggregate view (made current), else the shown file"""
    if st.all_mode:
        change = render.all_change_at(view, st)
        if change is not None:
            st.change = change
    return st.change


def _act_open(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Open the working-tree file at the line under the caret"""
    change = _current_change(view, st)
    if change is None:
        return
    path = os.path.join(st.repo, change.path)
    if not os.path.exists(path):
        status(f"{change.path} does not exist in the working tree")
        return
    # map the caret row to a line number of the new side
    if st.all_mode:
        row = render.caret_index(view)
        displayed = st.all_rows[row][1] if 0 <= row < len(st.all_rows) else None
        line = displayed.new_no if displayed and displayed.new_no is not None else 1
    else:
        line = parse.line_for_open(st.lines, render.caret_index(view))
    window.open_file(f"{path}:{line}", sublime.ENCODED_POSITION)


def _act_view(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Switch between the single-file and the aggregate view"""
    if st.all_mode:
        st.all_mode = False
        if st.change is not None:
            show_file(view, st, st.change)
    else:
        show_all(view, st)


def _toggle_file(delta: int) -> Action:
    """Action expanding (delta > 0) or collapsing the caret's file in the aggregate view"""
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Expand or collapse the file under the caret and redraw focused on it"""
        if not st.all_mode:
            return
        change = _current_change(view, st)
        if change is not None:
            (st.collapsed.discard if delta > 0 else st.collapsed.add)(change.path)
            render.render_all(view, st, focus=change.path)
    return act


def _act_fold_all(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Collapse every file while any is expanded, otherwise expand them all"""
    if not st.all_mode:
        return
    change = _current_change(view, st)
    paths = {c.path for c in st.files}
    st.collapsed = set() if st.collapsed >= paths else paths
    render.render_all(view, st, focus=change.path if change else None)


def _step_file(delta: int) -> Action:
    """Action moving `delta` files through the list, wrapping round"""
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Move to the neighbouring file: scroll to it in the aggregate view, else show its diff"""
        # aggregate view: unfold the neighbouring file and scroll to it
        if st.all_mode:
            if not st.files:
                status("no files to step through")
                return
            change = _current_change(view, st)
            index = state.find_change(st.files, change.path if change else None)
            # nothing selected yet: n starts at the first file, p at the last
            index = (index + delta) % len(st.files) if index >= 0 else (0 if delta > 0 else len(st.files) - 1)
            st.change = st.files[index]
            st.collapsed.discard(st.change.path)
            render.render_all(view, st, focus=st.change.path, center=True)
            return
        # single-file view: show the neighbouring file, ringing the bell on a wrap
        if not st.files:
            status("no file list, press f or c")
            return
        count = len(st.files)
        position = st.index()
        if position < 0:
            # nothing shown yet: n starts at the first file, p at the last, no wrap
            index, wrapped = (0 if delta > 0 else count - 1), False
        else:
            index = position + delta
            wrapped = not 0 <= index < count
            index %= count  # -1 is the last file, count is the first
        if wrapped:
            render.flash(view)
        if index != position:
            # a one-file list wraps onto itself: ring, but do not re-run git for the same file
            show_file(view, st, st.files[index])

    return act


def _step_change(delta: int) -> Action:
    """Action moving the caret `delta` changes on, wrapping round"""
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Jump to the next or previous change, across files in the aggregate view"""
        if st.all_mode:
            # aggregate view: the hunks of all files are the targets
            current_change = _current_change(view, st)
            rows = st.all_change_rows
            targets = st.all_change_targets
            if not targets:
                return
            current = render.caret_index(view)
            current_path = current_change.path if current_change else None
            # anchor: the caret file's last shown hunk at or above the caret
            current_target = None
            for row, target in zip(rows, st.all_change_row_targets):
                if target[0] == current_path and row <= current:
                    current_target = target
            if current_target in targets:
                # step from the anchor, wrapping round
                index = targets.index(current_target) + delta
            else:
                # above the file's first hunk (heading, folded) or outside any file: its first hunk
                # is next and the one before it previous; outside a file, the list's first or last
                first = next((i for i, t in enumerate(targets) if t[0] == current_path), None)
                if first is None:
                    index = 0 if delta > 0 else -1
                else:
                    index = first if delta > 0 else first - 1
            target = targets[index % len(targets)]
            # make the target's file current, unfold it and centre on the hunk
            target_change = next(c for c in st.files if c.path == target[0])
            st.change = target_change
            st.collapsed.discard(target_change.path)
            render.render_all(view, st, focus=target, center=True)
            return
        # single-file view: step between change starts from the caret row
        starts = [start for start, _ in st.blocks]
        if not starts:
            status("no changes to step through")
            return
        row = render.caret_index(view)
        position = bisect.bisect_right(starts, row) if delta > 0 else bisect.bisect_left(starts, row) - 1
        if not 0 <= position < len(starts):
            render.flash(view)
            position %= len(starts)  # -1 is the last change, len(starts) the first
        # move the caret onto the change and centre it
        point = render.index_point(view, starts[position])
        view.sel().clear()
        view.sel().add(sublime.Region(point))
        view.show_at_center(point)

    return act


def _act_commit(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Commit the staged files with a message typed into an input panel; Esc there cancels"""
    if st.in_commit:
        status("commit works on uncommitted changes only")
        return
    staged = [c for c in st.files if c.staged]
    if not staged:
        status("no staged files")
        return

    def on_done(message: str) -> None:
        """Commit with the typed message off the UI thread, or cancel on an empty one"""
        if not message.strip():
            status("commit cancelled: empty message")
            return
        run_async(
            lambda: git.commit(st.repo, message),
            lambda _: refresh_listing(view, st, f"committed {file_count(len(staged))}"),
        )

    window.show_input_panel("Commit message", "", on_done, None, None)


def _act_amend(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    """Rewrite HEAD's message in an input panel, folding in whatever is staged; Esc there cancels

    The one-line panel offers only the subject; the body rides along verbatim, and nothing staged is a reword
    """
    if st.in_commit:
        status("amend works on uncommitted changes only")
        return
    staged = [c for c in st.files if c.staged]

    def prompt(message: str) -> None:
        """Show the input panel prefilled with HEAD's subject"""
        subject, newline, body = message.partition("\n")

        def on_done(text: str) -> None:
            """Amend with the typed subject and the original body, or cancel on an empty one"""
            if not text.strip():
                status("amend cancelled: empty message")
                return
            done = "amended the last commit"
            if staged:
                done += f" with {file_count(len(staged))}"
            run_async(
                # a reword needs --allow-empty when HEAD itself is an empty commit
                lambda: git.amend(st.repo, text + newline + body, allow_empty=not staged),
                lambda _: refresh_listing(view, st, done),
            )

        panel = window.show_input_panel("Amend message", subject, on_done, None, None)
        # the panel selects the whole prefill; the caret belongs after it, ready to type
        panel.sel().clear()
        panel.sel().add(sublime.Region(panel.size()))

    run_async(lambda: git.head_message(st.repo), prompt)


def _move_line(forward: bool) -> Action:
    """Action moving the caret one line down (forward) or up"""
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        """Move the caret one line"""
        view.run_command("move", {"by": "lines", "forward": forward})

    return act


ACTIONS: Dict[str, Action] = {
    "files": _act_files,
    "commits": _act_commits,
    "close": _act_close,
    "help": _act_help,
    "stage": _git_action("stage", "staged", git.stage, pick=_stage_next),
    "unstage": _git_action("unstage", "unstaged", git.unstage),
    "stage_all": _bulk_action("stage", "staged", git.stage_all, lambda c: not c.staged),
    "unstage_all": _bulk_action("unstage", "unstaged", git.unstage_all, lambda c: c.staged),
    "discard": _git_action("discard", "discarded", git.discard, confirm=_confirm_discard),
    "commit": _act_commit,
    "amend": _act_amend,
    "refresh": _act_refresh,
    "view": _act_view,
    "expand": _toggle_file(1),
    "collapse": _toggle_file(-1),
    "fold_all": _act_fold_all,
    "open": _act_open,
    "next_file": _step_file(1),
    "prev_file": _step_file(-1),
    "next_change": _step_change(1),
    "prev_change": _step_change(-1),
    "line_down": _move_line(True),
    "line_up": _move_line(False),
}


class VdiffActionCommand(sublime_plugin.TextCommand):
    """Every key/link action of a review view, dispatched by name through ACTIONS"""

    def is_enabled(self, action: Optional[str] = None) -> bool:
        """Only in review views"""
        return state.is_review_view(self.view)

    def run(self, edit: sublime.Edit, action: str) -> None:
        """Run the named action with the view's state, rebuilt if lost (e.g. after a restart)"""
        act = ACTIONS.get(action)
        if act is None:
            status(f"unknown action {action}")
            return
        st = state.get(self.view) or restore_view(self.view)
        act(self.view, self.view.window(), st)


def restore_view(view: sublime.View) -> ViewState:
    """Rebuild the state of a review view restored from a previous session; re-renders asynchronously"""
    # the repo, commit and file saved with the view
    data = view.settings().get(state.VIEW_DATA) or {}
    repo = data.get("repo")
    render.init_view(view)
    # repository gone: show a notice
    if not repo or not os.path.isdir(repo):
        st = state.put(view, state.ViewState(""))
        render.render_message(view, st, "Repository not found. Press f for changed files, c for commits.")
        return st
    st = state.put(view, state.ViewState(repo))
    st.sha = data.get("sha")
    st.all_mode = bool(data.get("all_mode", False))
    path = data.get("path")
    if not path:
        render.render_message(view, st, "Press f for changed files, c for commits.")
        return st
    # stand-in FileChange: no line counts or rename source
    change = git.FileChange(path, data.get("status", "M"), bool(data.get("staged")), None, None, None)

    # a commit view re-lists the commit's files, a working-tree view the uncommitted changes
    sha = st.sha
    if sha is not None:

        def show_commit(result: Tuple[git.CommitInfo, List[FileChange]]) -> None:
            """Adopt the commit's files and redraw the saved file or the aggregate view"""
            st.commit, st.files = result
            if st.all_mode:
                st.change = change
                show_all(view, st)
            else:
                show_file(view, st, change)

        run_async(lambda: git.commit_with_files(repo, sha), show_commit)
    else:

        def show_working_tree(files: List[FileChange]) -> None:
            """Redraw the saved file if it still has changes, else say it has none"""
            st.files = files
            index = state.find_change(files, path)
            if index >= 0:
                if st.all_mode:
                    st.change = files[index]
                    show_all(view, st)
                else:
                    show_file(view, st, files[index])
            else:
                render.render_message(view, st, f"{path} has no uncommitted changes any more")

        run_async(lambda: git.changed_files(repo), show_working_tree)
    return st
