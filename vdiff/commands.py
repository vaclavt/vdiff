""" Commands: the two quick panels (changed files, recent commits) and the actions of a review view """
import bisect
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import sublime
import sublime_plugin

from . import git, parse, render, state
from .git import Commit, FileChange
from .state import Kept, ViewState
from .utils import file_count, run_async, settings, status

# what an ACTIONS entry does: act(view, window, state) for the review view it was fired in
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

# a quick-panel row draws the annotation hard right on the subject's line; past this budget the
# subject would run into it, so it is clipped. The floor keeps a readable stub on a narrow panel.
COMMIT_ROW_WIDTH = 120
MIN_SUBJECT_WIDTH = 24

# highlight-to-preview delay: holding an arrow key must not spawn a git process per row
PREVIEW_DELAY_MS = 60

# directory -> repository root; roots do not move, and resolving runs on the UI thread
_roots: Dict[str, str] = {}


def resolve_repo(window: sublime.Window) -> Optional[str]:
    """Repository of the review view or active file, else of the first project folder that is one"""
    view = window.active_view()
    candidates: List[str] = []
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
    for view in window.views():
        if state.is_review_view(view):
            return view
    return None


def open_review(window: sublime.Window) -> Optional[Tuple[sublime.View, ViewState]]:
    """(view, state) of the window's single review view for the resolved repo, focused; None without a repo"""
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


def show_file(view: sublime.View, st: ViewState, change: FileChange) -> None:
    """Fetch and parse the file's diff on the worker thread, then render it"""
    st.token += 1
    # set before the worker starts: n / p count from here while the diff is still loading
    st.requested = change
    token = st.token
    repo, sha = st.repo, st.sha

    def work() -> Optional[Tuple[parse.Diff, List[parse.Block]]]:
        if token != st.token:
            return None  # superseded before it started: skip the subprocess
        text = git.full_diff(repo, change.path, sha=sha, untracked=(change.status == "?"))
        diff = parse.parse_unified(text)
        return diff, parse.change_blocks(diff.lines)

    def done(result: Optional[Tuple[parse.Diff, List[parse.Block]]]) -> None:
        if result is None or token != st.token or not view.is_valid():
            return
        diff, blocks = result
        st.change = change
        st.lines, st.blocks, st.binary = diff.lines, blocks, diff.binary
        render.render(view, st)
        st.rendered_token = token

    run_async(work, done)


def file_items(files: Sequence[FileChange]) -> List[sublime.QuickPanelItem]:
    items: List[sublime.QuickPanelItem] = []
    for change in files:
        description = git.STATUS_NAMES.get(change.status, "")
        colour = STATUS_COLOURS.get(change.status)
        kind = (colour, change.status, description) if colour else sublime.KIND_AMBIGUOUS
        details = ", ".join(filter(None, (description, "staged" if change.staged else "")))
        if change.old_path:
            details += "  ← " + change.old_path
        if None in (change.insertions, change.deletions):
            annotation = "binary"
        else:
            annotation = f"-{change.deletions} +{change.insertions}"
        items.append(sublime.QuickPanelItem(change.path, details, annotation, kind))
    return items


def commit_items(commits: Sequence[Commit]) -> List[sublime.QuickPanelItem]:
    """Quick-panel rows: subject on the left, its totals and the hash right-aligned"""
    items: List[sublime.QuickPanelItem] = []
    for c in commits:
        annotation = c.short
        if c.files is not None:
            # merges and empty commits have no diffstat, so they show the hash alone
            annotation = f"{file_count(c.files)} changed  -{c.deletions} +{c.insertions}  {c.short}"
        subject = c.subject or "(no subject)"
        budget = max(COMMIT_ROW_WIDTH - len(annotation), MIN_SUBJECT_WIDTH)
        if len(subject) > budget:
            subject = subject[: budget - 2] + ".."
        items.append(sublime.QuickPanelItem(subject, f"{c.author} · {c.date}", annotation, COMMIT_KIND))
    return items


def show_files_panel(window: sublime.Window, view: sublime.View, st: ViewState, files: List[FileChange],
                     kept: Kept, on_cancel: Optional[Callable[[], None]] = None,
                     placeholder: str = "Changed files") -> None:
    """Quick panel over `files`; highlighting previews, Enter keeps, Esc restores `kept` (or runs on_cancel)"""
    st.files = files
    selected = max(0, st.index())
    window.focus_view(view)

    def on_select(index: int) -> None:
        st.highlight += 1  # cancel a pending preview
        if index == -1:
            if on_cancel is not None:
                st.restore(kept)
                on_cancel()
            elif kept.change is not None:
                # go back to the file that was kept before the panel opened
                st.restore(kept)
                show_file(view, st, kept.change)
            # nothing was kept before: the last previewed file stays
            return
        change = files[index]
        if st.change is change and st.rendered_token == st.token:
            return  # the preview already shows it
        show_file(view, st, change)

    def on_highlight(index: int) -> None:
        if not 0 <= index < len(files):
            return
        st.highlight += 1
        ticket = st.highlight

        def preview() -> None:
            if ticket == st.highlight:
                show_file(view, st, files[index])

        sublime.set_timeout(preview, PREVIEW_DELAY_MS)

    window.show_quick_panel(file_items(files), on_select, 0, selected, on_highlight, placeholder)


def refresh_listing(view: sublime.View, st: ViewState, message: Optional[str] = None,
                    pick: Optional[Callable[[List[FileChange], int], Optional[FileChange]]] = None) -> None:
    """Re-list the working tree changes and show the same file, or the next one when it is gone

    `pick(files, index)` may name another file to show instead; `index` is where the shown file
    sits in the new list, or the slot it left behind when it is gone.
    """
    if message:
        status(message)
    previous_index = max(0, st.index())
    path = st.change.path if st.change else None

    def done(files: List[FileChange]) -> None:
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
        show_file(view, st, chosen if chosen is not None else files[index])

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
        window = self.window
        opened = open_review(window)
        if not opened:
            return
        view, st = opened

        def done(files: List[FileChange]) -> None:
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
        opened = open_review(self.window)
        if not opened:
            return
        view, st = opened
        if sha:
            self.open_commit(view, st, sha, None)
            return
        count = int(settings().get("commits_count", 30) or 30)

        def done(commits: List[Commit]) -> None:
            if not view.is_valid():
                return
            if not commits:
                status("no commits yet")
                return
            self.show_commits(view, st, commits, selected)

        run_async(lambda: git.recent_commits(st.repo, count), done)

    def show_commits(self, view: sublime.View, st: ViewState, commits: Sequence[Commit], selected: int) -> None:
        items = commit_items(commits)
        for i, c in enumerate(commits):
            if c.sha == st.sha:
                selected = i

        def on_select(index: int) -> None:
            if index != -1:
                self.open_commit(view, st, commits[index].sha, index)

        self.window.show_quick_panel(items, on_select, 0, selected, None, "Recent commits")

    def open_commit(self, view: sublime.View, st: ViewState, sha: str, index: Optional[int]) -> None:
        """Files panel of one commit; Esc goes back to the commit list when it came from there (index given)"""
        window = self.window

        def done(result: Tuple[git.CommitInfo, List[FileChange]]) -> None:
            if not view.is_valid():
                return
            info, files = result
            kept = st.enter(info.sha, info)
            if not files:
                render.render_message(view, st, f"Commit {info.short} touches no files")
                return

            def back() -> None:
                window.run_command("vdiff_commits", {"selected": index})

            placeholder = f"{info.short} · {info.subject}"
            on_cancel = back if index is not None else None
            show_files_panel(window, view, st, files, kept, on_cancel=on_cancel, placeholder=placeholder)

        run_async(lambda: git.commit_with_files(st.repo, sha), done)


# --- review view actions ----------------------------------------------------


def _act_files(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    window.run_command("vdiff_files")


def _act_commits(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    window.run_command("vdiff_commits")


def _act_close(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    view.close()


def _act_help(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    render.show_help(view, st)


def _git_action(verb: str, done_verb: str, operation: Callable[[str, FileChange], None],
                confirm: Optional[Callable[[FileChange], bool]] = None,
                pick: Optional[Callable[[List[FileChange], int], Optional[FileChange]]] = None) -> Action:
    """Action running `operation(repo, change)` on the shown working-tree file, then re-listing

    `pick` is handed to refresh_listing to choose which file to show afterwards.
    """

    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        if st.in_commit or st.change is None:
            status(f"{verb} works on uncommitted changes only")
            return
        change = st.change
        if confirm is not None and not confirm(change):
            return
        run_async(
            lambda: operation(st.repo, change),
            lambda _: refresh_listing(view, st, f"{done_verb} {change.path}", pick=pick),
        )

    return act


def _confirm_discard(change: FileChange) -> bool:
    if not settings().get("confirm_discard", True):
        return True
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
    # the file just staged is staged now, so a scan starting on it moves off by itself
    following = _next_unstaged(files, index)
    return files[following] if following >= 0 else None


def _act_refresh(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    if st.change is None:
        return
    if st.in_commit:
        show_file(view, st, st.change)
    else:
        refresh_listing(view, st)


def _act_open(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
    if st.change is None:
        return
    path = os.path.join(st.repo, st.change.path)
    if not os.path.exists(path):
        status(f"{st.change.path} does not exist in the working tree")
        return
    line = parse.line_for_open(st.lines, render.caret_index(view))
    window.open_file(f"{path}:{line}", sublime.ENCODED_POSITION)


def _step_file(delta: int) -> Action:
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        if not st.files:
            status("no file list, press f or c")
            return
        count = len(st.files)
        position = st.index()
        if position < 0:
            # nothing shown yet: n starts at the first file, p at the last. Not a wrap.
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
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        starts = [start for start, _ in st.blocks]
        if not starts:
            status("no changes to step through")
            return
        row = render.caret_index(view)
        position = bisect.bisect_right(starts, row) if delta > 0 else bisect.bisect_left(starts, row) - 1
        if not 0 <= position < len(starts):
            render.flash(view)
            position %= len(starts)  # -1 is the last change, len(starts) the first
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

    The panel is one line, so only the subject is offered; the rest of the message rides along
    verbatim. Nothing staged is fine - then the amend is a pure reword.
    """
    if st.in_commit:
        status("amend works on uncommitted changes only")
        return
    staged = [c for c in st.files if c.staged]

    def prompt(message: str) -> None:
        subject, newline, body = message.partition("\n")

        def on_done(text: str) -> None:
            if not text.strip():
                status("amend cancelled: empty message")
                return
            done = "amended the last commit"
            if staged:
                done += f" with {file_count(len(staged))}"
            run_async(
                # nothing staged makes this a pure reword, which git refuses on an empty commit
                lambda: git.amend(st.repo, text + newline + body, allow_empty=not staged),
                lambda _: refresh_listing(view, st, done),
            )

        panel = window.show_input_panel("Amend message", subject, on_done, None, None)
        # the panel selects the whole prefill; the caret belongs after it, ready to type
        panel.sel().clear()
        panel.sel().add(sublime.Region(panel.size()))

    run_async(lambda: git.head_message(st.repo), prompt)


def _move_line(forward: bool) -> Action:
    def act(view: sublime.View, window: sublime.Window, st: ViewState) -> None:
        view.run_command("move", {"by": "lines", "forward": forward})

    return act


ACTIONS: Dict[str, Action] = {
    "files": _act_files,
    "commits": _act_commits,
    "close": _act_close,
    "help": _act_help,
    "stage": _git_action("stage", "staged", git.stage, pick=_stage_next),
    "unstage": _git_action("unstage", "unstaged", git.unstage),
    "discard": _git_action("discard", "discarded", git.discard, confirm=_confirm_discard),
    "commit": _act_commit,
    "amend": _act_amend,
    "refresh": _act_refresh,
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
        return state.is_review_view(self.view)

    def run(self, edit: sublime.Edit, action: str) -> None:
        act = ACTIONS.get(action)
        if act is None:
            status(f"unknown action {action}")
            return
        st = state.get(self.view) or restore_view(self.view)
        act(self.view, self.view.window(), st)


def restore_view(view: sublime.View) -> ViewState:
    """Rebuild the state of a review view restored from a previous session; re-renders asynchronously"""
    data = view.settings().get(state.VIEW_DATA) or {}
    repo = data.get("repo")
    render.init_view(view)
    if not repo or not os.path.isdir(repo):
        st = state.put(view, state.ViewState(""))
        render.render_message(view, st, "Repository not found. Press f for changed files, c for commits.")
        return st
    st = state.put(view, state.ViewState(repo))
    st.sha = data.get("sha")
    path = data.get("path")
    if not path:
        render.render_message(view, st, "Press f for changed files, c for commits.")
        return st
    change = git.FileChange(path, data.get("status", "M"), bool(data.get("staged")), None, None, None)

    sha = st.sha
    if sha is not None:

        def show_commit(result: Tuple[git.CommitInfo, List[FileChange]]) -> None:
            st.commit, st.files = result
            show_file(view, st, change)

        run_async(lambda: git.commit_with_files(repo, sha), show_commit)
    else:

        def show_working_tree(files: List[FileChange]) -> None:
            st.files = files
            index = state.find_change(files, path)
            if index >= 0:
                show_file(view, st, files[index])
            else:
                render.render_message(view, st, f"{path} has no uncommitted changes any more")

        run_async(lambda: git.changed_files(repo), show_working_tree)
    return st
