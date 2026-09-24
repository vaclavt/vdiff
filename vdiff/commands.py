""" Commands: the two quick panels (changed files, recent commits) and the actions of a review view """
import bisect
import os

import sublime
import sublime_plugin

from . import git, parse, render, state
from .utils import run_async, settings, status

# quick-panel kind per status letter: colour + the letter; the description comes from git.STATUS_NAMES
STATUS_COLOURS = {
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
_roots = {}


def resolve_repo(window):
    """Repository of the review view or active file, else of the first project folder that is one"""
    view = window.active_view()
    candidates = []
    if view is not None:
        if state.is_review_view(view):
            st = state.get(view) or restore_view(view)
            if st.repo:
                return st.repo
        elif view.file_name():
            candidates.append(os.path.dirname(view.file_name()))
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


def find_review_view(window):
    for view in window.views():
        if state.is_review_view(view):
            return view
    return None


def open_review(window):
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


def show_file(view, st, change):
    """Fetch and parse the file's diff on the worker thread, then render it"""
    st.token += 1
    # set before the worker starts: n / p count from here while the diff is still loading
    st.requested = change
    token = st.token
    repo, sha = st.repo, st.sha

    def work():
        if token != st.token:
            return None  # superseded before it started: skip the subprocess
        text = git.full_diff(repo, change.path, sha=sha, untracked=(change.status == "?"))
        diff = parse.parse_unified(text)
        return diff, parse.change_blocks(diff.lines)

    def done(result):
        if result is None or token != st.token or not view.is_valid():
            return
        diff, blocks = result
        st.change = change
        st.lines, st.blocks, st.binary = diff.lines, blocks, diff.binary
        render.render(view, st)
        st.rendered_token = token

    run_async(work, done)


def file_items(files):
    items = []
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
            annotation = "-{} +{}".format(change.deletions, change.insertions)
        items.append(sublime.QuickPanelItem(change.path, details, annotation, kind))
    return items


def commit_items(commits):
    """Quick-panel rows: subject on the left, its totals and the hash right-aligned"""
    items = []
    for c in commits:
        annotation = c.short
        if c.files is not None:
            # merges and empty commits have no diffstat, so they show the hash alone
            annotation = "{} {} changed  -{} +{}  {}".format(
                c.files, "file" if c.files == 1 else "files", c.deletions, c.insertions, c.short
            )
        subject = c.subject or "(no subject)"
        budget = max(COMMIT_ROW_WIDTH - len(annotation), MIN_SUBJECT_WIDTH)
        if len(subject) > budget:
            subject = subject[: budget - 2] + ".."
        items.append(sublime.QuickPanelItem(subject, "{} · {}".format(c.author, c.date), annotation, COMMIT_KIND))
    return items


def show_files_panel(window, view, st, files, kept, on_cancel=None, placeholder="Changed files"):
    """Quick panel over `files`; highlighting previews, Enter keeps, Esc restores `kept` (or runs on_cancel)"""
    st.files = files
    selected = max(0, st.index())
    window.focus_view(view)

    def on_select(index):
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

    def on_highlight(index):
        if not 0 <= index < len(files):
            return
        st.highlight += 1
        ticket = st.highlight
        sublime.set_timeout(lambda: ticket == st.highlight and show_file(view, st, files[index]), PREVIEW_DELAY_MS)

    window.show_quick_panel(file_items(files), on_select, 0, selected, on_highlight, placeholder)


def refresh_listing(view, st, message=None):
    """Re-list the working tree changes and show the same file, or the next one when it is gone"""
    if message:
        status(message)
    previous_index = max(0, st.index())
    path = st.change.path if st.change else None

    def done(files):
        if not view.is_valid():
            return
        st.files = files
        index = state.find_change(files, path)
        if index >= 0:
            show_file(view, st, files[index])
        elif files:
            show_file(view, st, files[min(previous_index, len(files) - 1)])
        else:
            render.render_message(view, st, "No uncommitted changes in {}".format(st.repo))

    run_async(lambda: git.changed_files(st.repo), done)


def refresh_if_changed(view, st):
    """Re-render a working-tree view when the file or the index changed since it was drawn"""
    if st.in_commit or st.change is None:
        return
    if render.fingerprint(st) != st.fingerprint:
        refresh_listing(view, st)


class VdiffFilesCommand(sublime_plugin.WindowCommand):
    """List uncommitted changes (staged, unstaged, untracked) in a quick panel"""

    def run(self):
        window = self.window
        opened = open_review(window)
        if not opened:
            return
        view, st = opened

        def done(files):
            if not view.is_valid():
                return
            kept = st.enter()
            if not files:
                render.render_message(view, st, "No uncommitted changes in {}".format(st.repo))
                status("no uncommitted changes")
                return
            placeholder = "Changed files in {}".format(os.path.basename(st.repo))
            show_files_panel(window, view, st, files, kept, placeholder=placeholder)

        run_async(lambda: git.changed_files(st.repo), done)


class VdiffCommitsCommand(sublime_plugin.WindowCommand):
    """List recent commits, then the files of the chosen commit; `sha` skips straight to one commit"""

    def run(self, selected=0, sha=None):
        opened = open_review(self.window)
        if not opened:
            return
        view, st = opened
        if sha:
            self.open_commit(view, st, sha, None)
            return
        count = int(settings().get("commits_count", 30) or 30)

        def done(commits):
            if not view.is_valid():
                return
            if not commits:
                status("no commits yet")
                return
            self.show_commits(view, st, commits, selected)

        run_async(lambda: git.recent_commits(st.repo, count), done)

    def show_commits(self, view, st, commits, selected):
        items = commit_items(commits)
        for i, c in enumerate(commits):
            if c.sha == st.sha:
                selected = i

        def on_select(index):
            if index != -1:
                self.open_commit(view, st, commits[index].sha, index)

        self.window.show_quick_panel(items, on_select, 0, selected, None, "Recent commits")

    def open_commit(self, view, st, sha, index):
        """Files panel of one commit; Esc goes back to the commit list when it came from there (index given)"""
        window = self.window

        def done(result):
            if not view.is_valid():
                return
            info, files = result
            kept = st.enter(info.sha, info)
            if not files:
                render.render_message(view, st, "Commit {} touches no files".format(info.short))
                return

            def back():
                window.run_command("vdiff_commits", {"selected": index})

            placeholder = "{} · {}".format(info.short, info.subject)
            on_cancel = back if index is not None else None
            show_files_panel(window, view, st, files, kept, on_cancel=on_cancel, placeholder=placeholder)

        run_async(lambda: git.commit_with_files(st.repo, sha), done)


# --- review view actions ----------------------------------------------------


def _act_files(view, window, st):
    window.run_command("vdiff_files")


def _act_commits(view, window, st):
    window.run_command("vdiff_commits")


def _act_close(view, window, st):
    view.close()


def _act_help(view, window, st):
    render.show_help(view, st)


def _git_action(verb, done_verb, operation, confirm=None):
    """Action running `operation(repo, change)` on the shown working-tree file, then re-listing"""

    def act(view, window, st):
        if st.in_commit or st.change is None:
            status("{} works on uncommitted changes only".format(verb))
            return
        change = st.change
        if confirm is not None and not confirm(change):
            return
        run_async(lambda: operation(st.repo, change), lambda _: refresh_listing(view, st, "{} {}".format(done_verb, change.path)))

    return act


def _confirm_discard(change):
    if not settings().get("confirm_discard", True):
        return True
    if change.status == "?":
        message = "Delete the untracked file {}?\n\nThis cannot be undone.".format(change.path)
    else:
        message = "Discard all changes to {}?\n\nThe file goes back to its last committed version. This cannot be undone.".format(
            change.path
        )
    return sublime.ok_cancel_dialog(message, "Discard")


def _act_refresh(view, window, st):
    if st.change is None:
        return
    if st.in_commit:
        show_file(view, st, st.change)
    else:
        refresh_listing(view, st)


def _act_open(view, window, st):
    if st.change is None:
        return
    path = os.path.join(st.repo, st.change.path)
    if not os.path.exists(path):
        status("{} does not exist in the working tree".format(st.change.path))
        return
    line = parse.line_for_open(st.lines, render.caret_index(view))
    window.open_file("{}:{}".format(path, line), sublime.ENCODED_POSITION)


def _step_file(delta):
    def act(view, window, st):
        if not st.files:
            status("no file list, press f or c")
            return
        index = st.index() + delta
        if not 0 <= index < len(st.files):
            status("no more files")
            return
        show_file(view, st, st.files[index])

    return act


def _step_change(delta):
    def act(view, window, st):
        starts = [start for start, _ in st.blocks]
        row = render.caret_index(view)
        position = bisect.bisect_right(starts, row) if delta > 0 else bisect.bisect_left(starts, row) - 1
        if not 0 <= position < len(starts):
            status("no more changes")
            return
        point = render.index_point(view, starts[position])
        view.sel().clear()
        view.sel().add(sublime.Region(point))
        view.show_at_center(point)

    return act


def _act_commit(view, window, st):
    """Commit the staged files with a message typed into an input panel; Esc there cancels"""
    if st.in_commit:
        status("commit works on uncommitted changes only")
        return
    staged = [c for c in st.files if c.staged]
    if not staged:
        status("no staged files")
        return

    def on_done(message):
        if not message.strip():
            status("commit cancelled: empty message")
            return
        noun = "file" if len(staged) == 1 else "files"
        run_async(
            lambda: git.commit(st.repo, message),
            lambda _: refresh_listing(view, st, "committed {} {}".format(len(staged), noun)),
        )

    window.show_input_panel("Commit message", "", on_done, None, None)


def _move_line(forward):
    def act(view, window, st):
        view.run_command("move", {"by": "lines", "forward": forward})

    return act


ACTIONS = {
    "files": _act_files,
    "commits": _act_commits,
    "close": _act_close,
    "help": _act_help,
    "stage": _git_action("stage", "staged", git.stage),
    "unstage": _git_action("unstage", "unstaged", git.unstage),
    "discard": _git_action("discard", "discarded", git.discard, confirm=_confirm_discard),
    "commit": _act_commit,
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

    def is_enabled(self, action=None):
        return state.is_review_view(self.view)

    def run(self, edit, action):
        act = ACTIONS.get(action)
        if act is None:
            status("unknown action {}".format(action))
            return
        st = state.get(self.view) or restore_view(self.view)
        act(self.view, self.view.window(), st)


def restore_view(view):
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

    if st.in_commit:

        def done(result):
            st.commit, st.files = result
            show_file(view, st, change)

        run_async(lambda: git.commit_with_files(repo, st.sha), done)
    else:

        def done(files):
            st.files = files
            index = state.find_change(files, path)
            if index >= 0:
                show_file(view, st, files[index])
            else:
                render.render_message(view, st, "{} has no uncommitted changes any more".format(path))

        run_async(lambda: git.changed_files(repo), done)
    return st
