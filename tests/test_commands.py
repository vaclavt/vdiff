import os

import pytest

import sublime  # the stub, via conftest

from vdiff import commands, git, state, utils


def change(path, status="M"):
    return git.FileChange(path, status, False, 1, 1, None)


def flagged(*flags):
    """f0.py, f1.py ... with the given staged flags"""
    return [git.FileChange(f"f{i}.py", "M", flag, 1, 1, None) for i, flag in enumerate(flags)]


def option(monkeypatch, name, value):
    """Set a plugin setting for one test; the stub hands out one Settings dict per file"""
    monkeypatch.setitem(sublime.load_settings(utils.SETTINGS_FILE), name, value)


def flush_previews():
    """The stub runs set_timeout synchronously, so previews are already shown"""


def at_change(blocks, row):
    """A rendered view with `blocks` as its changes and the caret on `row`"""
    view = sublime.View()
    st = state.ViewState("/repo")
    st.blocks = blocks
    view.text = "\n" + "\n".join(f"l{i}" for i in range(20))
    view.sel().add(sublime.Region(commands.render.index_point(view, row)))
    return view, st


def test_esc_restores_previous_file_and_mode(monkeypatch):
    shown = []
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: shown.append((st.sha, c.path)))
    view = sublime.View()
    st = state.ViewState("/repo")
    st.change = change("render.py")
    st.files = [st.change]

    # switch to a commit, as open_commit does; the panel gets the pre-switch snapshot
    kept = st.enter("abc", git.CommitInfo("abc", "abc1234", "A", "d", "s", ""))
    assert st.change is None and kept.change.path == "render.py"
    files = [change("notes.txt")]
    window = sublime.Window()
    commands.show_files_panel(window, view, st, files, kept, placeholder="x")
    items, on_select, selected, on_highlight, _ = window.panel
    assert [i.trigger for i in items] == ["notes.txt"] and selected == 0

    on_highlight(0)
    assert shown[-1] == ("abc", "notes.txt")

    on_select(-1)
    assert st.sha is None and st.commit is None and st.change.path == "render.py" and st.files == [st.change]
    assert shown[-1] == (None, "render.py")


def test_esc_with_nothing_kept_keeps_preview(monkeypatch):
    shown = []
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: shown.append(c.path))
    st = state.ViewState("/repo")
    window = sublime.Window()
    commands.show_files_panel(window, sublime.View(), st, [change("a.py"), change("b.py")], st.kept())
    _, on_select, _, on_highlight, _ = window.panel
    on_highlight(1)
    on_select(-1)
    assert shown == ["b.py"]


def test_enter_skips_refetch_when_preview_is_current(monkeypatch):
    shown = []

    def fake_show(view, st, c):
        st.token += 1
        st.change = c
        st.rendered_token = st.token
        shown.append(c.path)

    monkeypatch.setattr(commands, "show_file", fake_show)
    st = state.ViewState("/repo")
    window = sublime.Window()
    files = [change("a.py"), change("b.py")]
    commands.show_files_panel(window, sublime.View(), st, files, st.kept())
    _, on_select, _, on_highlight, _ = window.panel
    on_highlight(1)
    on_select(1)
    assert shown == ["b.py"] and st.files[1].path == "b.py"
    on_select(0)
    assert shown == ["b.py", "a.py"]


@pytest.mark.parametrize("mode, all_mode", [("file_by_file", False), ("all_in_one", True), (None, False)])
def test_a_new_review_view_starts_in_the_default_view_mode(monkeypatch, mode, all_mode):
    option(monkeypatch, "default_view_mode", mode)
    monkeypatch.setattr(commands, "resolve_repo", lambda window: "/repo")
    monkeypatch.setattr(commands, "find_review_view", lambda window: sublime.View())
    _, st = commands.open_review(sublime.Window())
    assert st.all_mode is all_mode


def test_panel_in_all_in_one_mode_scrolls_instead_of_loading_files(monkeypatch):
    loads, focused = [], []

    def fake_show_all(view, st, focus=None):
        # load at once: what show_all's done does once the diffs land
        st.token += 1
        st.rendered_token = st.token
        loads.append(st.change.path)

    monkeypatch.setattr(commands, "show_all", fake_show_all)
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: pytest.fail("loaded a single file"))
    monkeypatch.setattr(commands.render, "render_all", lambda view, st, focus=None: focused.append(focus))
    st = state.ViewState("/repo", all_mode=True)
    st.change = change("kept.py")
    st.files = [st.change]
    files = [change("a.py"), change("b.py")]
    st.collapsed = {"b.py"}
    window = sublime.Window()
    commands.show_files_panel(window, sublime.View(), st, files, st.kept())
    _, on_select, _, on_highlight, _ = window.panel
    assert loads == ["a.py"]

    # a highlight unfolds and scrolls to its file; Enter on it needs no redraw
    on_highlight(1)
    assert focused == ["b.py"] and st.change.path == "b.py" and "b.py" not in st.collapsed
    on_select(1)
    assert focused == ["b.py"] and loads == ["a.py"]

    # Esc goes back to the kept file, still in the all-in-one view
    on_select(-1)
    assert st.all_mode and st.change.path == "kept.py" and loads == ["a.py", "kept.py"]


def test_esc_goes_back_to_the_kept_view_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: calls.append(("file", c.path)))
    monkeypatch.setattr(commands, "show_all", lambda view, st, focus=None: calls.append(("all", st.change.path)))
    st = state.ViewState("/repo")
    st.change = change("kept.py")
    st.files = [st.change]
    kept = st.kept()
    # the view switched to all-in-one after the snapshot; Esc still restores file-by-file
    st.all_mode = True
    window = sublime.Window()
    commands.show_files_panel(window, sublime.View(), st, [change("a.py")], kept)
    window.panel[1](-1)
    assert calls[-1] == ("file", "kept.py") and not st.all_mode


def test_on_cancel_goes_back_to_commit_list(monkeypatch):
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: None)
    st = state.ViewState("/repo")
    kept = st.enter("abc", None)
    window = sublime.Window()
    back = []
    commands.show_files_panel(window, sublime.View(), st, [change("a.py")], kept, on_cancel=lambda: back.append(1))
    window.panel[1](-1)
    assert back == [1] and st.sha is None


def test_step_change_uses_blocks(rings):
    view, st = at_change([(2, 4), (7, 8)], 0)
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == 2
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == 7
    commands.ACTIONS["prev_change"](view, None, st)
    # stepping inside the list stays silent
    assert commands.render.caret_index(view) == 2 and rings == []


def test_file_items_annotations():
    items = commands.file_items([git.FileChange("x.py", "?", False, 3, 0, None), git.FileChange("i.png", "M", True, None, None, None)])
    assert items[0].annotation == "-0 +3" and items[0].details == "untracked" and items[0].kind[1] == "?"
    assert items[1].annotation == "binary" and items[1].details == "modified, staged"


def test_git_actions_refuse_commit_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "run_async", lambda work, done: calls.append(work))
    st = state.ViewState("/repo")
    st.enter("abc", None)
    st.change = change("a.py")
    commands.ACTIONS["stage"](sublime.View(), None, st)
    assert calls == []
    st.enter()
    st.change = change("a.py")
    commands.ACTIONS["stage"](sublime.View(), None, st)
    assert len(calls) == 1


def test_run_async_reports_git_error(monkeypatch):
    messages = []
    monkeypatch.setattr(commands.sublime, "status_message", lambda m: messages.append(m))

    def boom():
        raise git.GitError("fatal: bad revision 'nope'\nmore", 128)

    commands.run_async(boom, lambda r: messages.append("done"))
    assert messages == ["vDiff: fatal: bad revision 'nope'"]


def test_run_async_reports_generic_error(monkeypatch):
    messages = []
    monkeypatch.setattr(commands.sublime, "status_message", lambda m: messages.append(m))

    def boom():
        raise ValueError("boom")

    commands.run_async(boom, lambda r: messages.append("done"))
    assert messages == ["vDiff: boom"]


def test_j_and_k_move_the_caret_one_line():
    view = sublime.View()
    commands.ACTIONS["line_down"](view, None, None)
    commands.ACTIONS["line_up"](view, None, None)
    assert view.commands == [
        ("move", {"by": "lines", "forward": True}),
        ("move", {"by": "lines", "forward": False}),
    ]


def staged_state(*flags):
    st = state.ViewState("/repo")
    st.files = flagged(*flags)
    return st


def test_commit_needs_a_staged_file(monkeypatch):
    messages = []
    monkeypatch.setattr(commands.sublime, "status_message", lambda m: messages.append(m))
    window = sublime.Window()

    commands.ACTIONS["commit"](sublime.View(), window, staged_state(False, False))
    assert messages == ["vDiff: no staged files"] and window.input is None

    # a commit under review has nothing to stage in the first place
    st = staged_state(True)
    st.enter("abc", None)
    commands.ACTIONS["commit"](sublime.View(), window, st)
    assert messages[-1] == "vDiff: commit works on uncommitted changes only" and window.input is None


def test_commit_prompts_then_commits(monkeypatch):
    committed = []
    refreshed = []
    monkeypatch.setattr(commands.git, "commit", lambda repo, message: committed.append((repo, message)))
    monkeypatch.setattr(commands, "refresh_listing", lambda view, st, message=None: refreshed.append(message))
    window = sublime.Window()

    commands.ACTIONS["commit"](sublime.View(), window, staged_state(True, False, True))
    caption, initial, on_done, _, _ = window.input
    assert caption == "Commit message" and initial == "" and committed == []

    on_done("  ")  # an empty message is not a commit
    assert committed == []

    on_done("real message")
    assert committed == [("/repo", "real message")]
    assert refreshed == ["committed 2 files"]


def prompt_amend(monkeypatch, st, head="last subject"):
    """Press m; git reports `head` as HEAD's message. Returns (window, amended, refreshed)"""
    amended, refreshed = [], []
    monkeypatch.setattr(commands.git, "head_message", lambda repo: head)
    monkeypatch.setattr(
        commands.git, "amend", lambda repo, message, allow_empty=False: amended.append((repo, message, allow_empty))
    )
    monkeypatch.setattr(commands, "refresh_listing", lambda view, st, message=None: refreshed.append(message))
    window = sublime.Window()
    commands.ACTIONS["amend"](sublime.View(), window, st)
    return window, amended, refreshed


def test_amend_refuses_commit_mode(monkeypatch):
    messages = []
    monkeypatch.setattr(commands.sublime, "status_message", lambda m: messages.append(m))
    st = staged_state(True)
    st.enter("abc", None)

    window, amended, _ = prompt_amend(monkeypatch, st)
    assert messages[-1] == "vDiff: amend works on uncommitted changes only"
    assert window.input is None and amended == []


def test_amend_prefills_the_subject_with_the_caret_at_its_end(monkeypatch):
    window, amended, _ = prompt_amend(monkeypatch, staged_state(True), head="fix the parser")

    caption, initial, _, _, on_cancel = window.input
    assert caption == "Amend message" and initial == "fix the parser" and amended == []
    # the panel selects its prefill; the caret must sit after it, ready to type
    assert list(window.input_view.sel()) == [sublime.Region(len("fix the parser"))]
    # Esc is a no-op: there is no cancel callback to run
    assert on_cancel is None


def test_amend_offers_the_subject_and_keeps_the_rest_verbatim(monkeypatch):
    head = "typo hree\n\nCloses #12.\n\n- a\n- b"
    window, amended, refreshed = prompt_amend(monkeypatch, staged_state(True, False, True), head=head)

    assert window.input[1] == "typo hree"  # only the first line is offered for editing
    window.input[2]("typo here")
    # everything below the first line comes back untouched, and staged files keep git's guard
    assert amended == [("/repo", "typo here" + head[len("typo hree") :], False)]
    assert refreshed == ["amended the last commit with 2 files"]


def test_amend_rewords_with_nothing_staged(monkeypatch):
    window, amended, refreshed = prompt_amend(monkeypatch, staged_state(False, False))

    window.input[2]("just a reword")
    # a reword alone opts out of git's refusal, so an empty commit can still be renamed
    assert amended == [("/repo", "just a reword", True)]
    assert refreshed == ["amended the last commit"]


def test_amend_with_an_empty_message_does_nothing(monkeypatch):
    messages = []
    monkeypatch.setattr(commands.sublime, "status_message", lambda m: messages.append(m))
    window, amended, refreshed = prompt_amend(monkeypatch, staged_state(True))

    window.input[2]("   ")
    assert amended == [] and refreshed == []
    assert messages[-1] == "vDiff: amend cancelled: empty message"


def test_commit_message_counts_one_file(monkeypatch):
    refreshed = []
    monkeypatch.setattr(commands.git, "commit", lambda repo, message: None)
    monkeypatch.setattr(commands, "refresh_listing", lambda view, st, message=None: refreshed.append(message))
    window = sublime.Window()

    commands.ACTIONS["commit"](sublime.View(), window, staged_state(True, False))
    window.input[2]("one")
    assert refreshed == ["committed 1 file"]


def commit(subject, files=1, insertions=1, deletions=0, short="abc1234"):
    return git.Commit("a" * 40, short, "Ann", "2 days ago", subject, files, insertions, deletions)


def test_commit_items_show_totals_before_the_hash():
    plural, single, merge = commands.commit_items(
        [
            commit("fix the parser", 23, 210, 0, "fda1368"),
            commit("tweak", 1, 1, 2, "d45a250"),
            commit("merge branch 'side'", None, None, None, "beef123"),
        ]
    )
    assert plural.annotation == "23 files changed  -0 +210  fda1368"
    assert single.annotation == "1 file changed  -2 +1  d45a250"
    # no diffstat for a merge, so the row falls back to the hash alone
    assert merge.annotation == "beef123"
    assert plural.trigger == "fix the parser" and plural.details == "Ann · 2 days ago"


def test_long_subject_is_clipped_to_clear_the_annotation():
    long_subject = "a subject that runs on and on " * 6
    item = commands.commit_items([commit(long_subject, 3, 9, 4)])[0]

    assert item.trigger.endswith("..") and item.trigger[:-2] == long_subject[: len(item.trigger) - 2]
    # the clipped subject plus the annotation fit the row, leaving the totals and hash readable
    assert len(item.trigger) + len(item.annotation) <= commands.COMMIT_ROW_WIDTH
    # a subject that already fits is left alone
    assert commands.commit_items([commit("short one")])[0].trigger == "short one"


def test_commit_items_keep_a_readable_subject_when_the_annotation_is_huge():
    # an annotation long enough to eat the whole row: the subject still keeps its floor
    item = commands.commit_items([commit("x" * 200, 999999, 999999, 999999, "a" * 90)])[0]
    assert len(item.trigger) == commands.MIN_SUBJECT_WIDTH and item.trigger.endswith("..")


def test_commit_items_fall_back_for_an_empty_subject():
    assert commands.commit_items([commit("")])[0].trigger == "(no subject)"


# --- navigating while a diff is still loading -------------------------------


@pytest.fixture
def loading(monkeypatch):
    """Queue run_async instead of running it inline, so a test can act mid-load like a user can

    The stub runs set_timeout synchronously, so the real async window — the one n / p read stale
    state through — is invisible to every other test.
    """
    queue = []
    monkeypatch.setattr(commands, "run_async", lambda work, done: queue.append((work, done)))
    monkeypatch.setattr(commands.render, "render", lambda view, st: None)
    monkeypatch.setattr(commands.git, "full_diff", lambda *a, **k: "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")

    def flush():
        while queue:
            work, done = queue.pop(0)
            done(work())

    return queue, flush


def commit_panel(files):
    """A review view showing a commit, its file panel open, nothing loaded yet"""
    view, window = sublime.View(), sublime.Window()
    st = state.ViewState("/repo")
    st.enter("abc", git.CommitInfo("abc", "abc1234", "A", "d", "s", ""))
    commands.show_files_panel(window, view, st, files, st.kept(), placeholder="c")
    return view, window, st


def numbered(count=5):
    return flagged(*(False,) * count)


def requested_path(queue):
    """Path of the file the newest queued load will fetch"""
    return queue[-1][0].__closure__[0].cell_contents.path


def test_next_file_steps_from_the_file_being_loaded(loading):
    queue, _ = loading
    files = numbered()
    view, window, st = commit_panel(files)

    window.panel[1](2)  # Enter on the third file; its diff has not arrived yet
    assert st.change is None
    commands.ACTIONS["next_file"](view, window, st)
    assert requested_path(queue) == "f3.py"


def test_prev_file_works_before_the_diff_arrives(loading, monkeypatch):
    queue, _ = loading
    messages = []
    monkeypatch.setattr(commands.sublime, "status_message", lambda m: messages.append(m))
    files = numbered()
    view, window, st = commit_panel(files)

    window.panel[1](2)
    commands.ACTIONS["prev_file"](view, window, st)
    assert requested_path(queue) == "f1.py" and messages == []


def test_rapid_next_file_presses_each_advance(loading):
    queue, flush = loading
    files = numbered()
    view, window, st = commit_panel(files)

    window.panel[1](1)
    for _ in range(3):
        commands.ACTIONS["next_file"](view, window, st)
    assert requested_path(queue) == "f4.py"
    flush()
    assert st.change.path == "f4.py"


def test_esc_rewinds_the_file_navigation_counts_from(loading):
    queue, flush = loading
    files = numbered()
    view, window, st = commit_panel(files)
    window.panel[1](3)
    flush()

    kept = st.kept()
    window.panel[1](0)  # preview another file, then back out
    st.restore(kept)
    commands.ACTIONS["next_file"](view, window, st)
    assert requested_path(queue) == "f4.py"


# --- cycling past the ends --------------------------------------------------


@pytest.fixture
def rings(monkeypatch):
    """Record visual bells instead of drawing them"""
    bells = []
    monkeypatch.setattr(commands.render, "flash", lambda view: bells.append(view))
    return bells


def at_file(files, path):
    """A loaded working-tree view sitting on `path`"""
    view, window = sublime.View(), sublime.Window()
    st = state.ViewState("/repo")
    st.files = files
    st.change = st.requested = next(c for c in files if c.path == path)
    return view, window, st


def test_next_file_wraps_past_the_last(loading, rings):
    queue, _ = loading
    files = numbered()
    view, window, st = at_file(files, "f4.py")

    commands.ACTIONS["next_file"](view, window, st)
    assert requested_path(queue) == "f0.py" and len(rings) == 1


def test_prev_file_wraps_before_the_first(loading, rings):
    queue, _ = loading
    files = numbered()
    view, window, st = at_file(files, "f0.py")

    commands.ACTIONS["prev_file"](view, window, st)
    assert requested_path(queue) == "f4.py" and len(rings) == 1


def test_stepping_inside_the_list_does_not_ring(loading, rings):
    queue, _ = loading
    files = numbered()
    view, window, st = at_file(files, "f2.py")

    commands.ACTIONS["next_file"](view, window, st)
    commands.ACTIONS["prev_file"](view, window, st)
    assert requested_path(queue) == "f2.py" and rings == []


def test_single_file_list_rings_without_refetching(loading, rings):
    queue, _ = loading
    files = numbered(1)
    view, window, st = at_file(files, "f0.py")

    commands.ACTIONS["next_file"](view, window, st)
    commands.ACTIONS["prev_file"](view, window, st)
    # it rings both times, but never re-runs git for the file already on screen
    assert len(rings) == 2 and queue == []


def test_first_step_without_a_shown_file_picks_an_end(loading, rings):
    queue, _ = loading
    files = numbered()
    view, window, st = commit_panel(files)  # opening a commit leaves nothing shown
    assert st.index() == -1

    commands.ACTIONS["next_file"](view, window, st)
    assert requested_path(queue) == "f0.py"
    st.requested = None
    commands.ACTIONS["prev_file"](view, window, st)
    assert requested_path(queue) == "f4.py"
    # neither is a wrap: there was no position to run past
    assert rings == []


def test_next_change_wraps_past_the_last(rings):
    view, st = at_change([(2, 4), (7, 8)], 7)

    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == 2 and len(rings) == 1


def test_prev_change_wraps_before_the_first(rings):
    view, st = at_change([(2, 4), (7, 8)], 2)

    commands.ACTIONS["prev_change"](view, None, st)
    assert commands.render.caret_index(view) == 7 and len(rings) == 1


def test_single_change_rings_and_stays_put(rings):
    view, st = at_change([(2, 4)], 2)

    commands.ACTIONS["next_change"](view, None, st)
    commands.ACTIONS["prev_change"](view, None, st)
    assert commands.render.caret_index(view) == 2 and len(rings) == 2


def test_a_file_without_changes_neither_moves_nor_rings(rings):
    view, st = at_change([], 3)

    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == 3 and rings == []


# --- staging moves on to the next unstaged file -----------------------------


def press(monkeypatch, action, before, path, after):
    """Run `action` on `path` of `before`; git then reports `after`. Returns the paths shown

    ACTIONS captured git.stage and friends at build time, so the stub goes one level down at git.run,
    keeping the action's real wiring under test.
    """
    shown = []
    monkeypatch.setattr(commands.git, "run", lambda repo, args, ok_codes=(0,): "")
    monkeypatch.setattr(commands.git, "changed_files", lambda repo: after)
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: shown.append(c.path))
    view, window, st = at_file(before, path)
    commands.ACTIONS[action](view, window, st)
    return shown


def test_next_unstaged_scans_forward_and_wraps():
    files = flagged(False, True, False, True)
    assert commands._next_unstaged(files, 1) == 2
    assert commands._next_unstaged(files, 2) == 2  # the slot we count from counts itself
    assert commands._next_unstaged(files, 3) == 0  # wraps past the end
    assert commands._next_unstaged(flagged(True, True), 0) == -1


def test_staging_moves_to_the_next_unstaged_file(monkeypatch):
    shown = press(monkeypatch, "stage", flagged(True, False, True, False), "f1.py", flagged(True, True, True, False))
    assert shown == ["f3.py"]


def test_staging_wraps_to_an_unstaged_file_above(monkeypatch):
    shown = press(monkeypatch, "stage", flagged(False, True, False), "f2.py", flagged(False, True, True))
    assert shown == ["f0.py"]


def test_staging_the_last_unstaged_file_stays_put(monkeypatch):
    shown = press(monkeypatch, "stage", flagged(True, False), "f1.py", flagged(True, True))
    assert shown == ["f1.py"]


def test_staging_a_file_that_leaves_the_list_counts_from_its_slot(monkeypatch):
    # staging can make the entry vanish (the change was identical to HEAD); f2.py takes the slot
    before = flagged(True, False, False)
    after = [before[0], before[2]]
    shown = press(monkeypatch, "stage", before, "f1.py", after)
    assert shown == ["f2.py"]


def test_staging_does_not_move_when_the_option_is_off(monkeypatch):
    option(monkeypatch, "stage_and_move_next_file", False)
    shown = press(monkeypatch, "stage", flagged(True, False, False), "f1.py", flagged(True, True, False))
    assert shown == ["f1.py"]


def test_unstage_and_discard_do_not_move(monkeypatch):
    option(monkeypatch, "confirm_discard", False)
    files = flagged(True, True, False)
    assert press(monkeypatch, "unstage", files, "f1.py", flagged(True, False, False)) == ["f1.py"]
    assert press(monkeypatch, "discard", files, "f1.py", flagged(True, False, False)) == ["f1.py"]


def test_stage_all_and_unstage_all_stay_on_the_file(monkeypatch):
    messages = []
    monkeypatch.setattr(commands, "status", messages.append)
    assert press(monkeypatch, "stage_all", flagged(True, False, False), "f1.py", flagged(True, True, True)) == ["f1.py"]
    assert messages[0] == "staged 2 files"
    assert press(monkeypatch, "unstage_all", flagged(True, False, True), "f1.py", flagged(False, False, False)) == ["f1.py"]
    assert messages[-1] == "unstaged 2 files"


def test_stage_all_and_unstage_all_need_something_to_do(monkeypatch):
    messages = []
    monkeypatch.setattr(commands, "status", messages.append)
    assert press(monkeypatch, "stage_all", flagged(True, True), "f0.py", []) == []
    assert press(monkeypatch, "unstage_all", flagged(False, False), "f0.py", []) == []
    assert messages == ["nothing to stage", "nothing to unstage"]
    view, window, st = at_file(flagged(False), "f0.py")
    st.enter("abc", git.CommitInfo("abc", "abc1234", "Ann", "2026-09-23 10:00", "Fix things", ""))
    commands.ACTIONS["stage_all"](view, window, st)
    assert messages[-1] == "stage all works on uncommitted changes only"


def watched(monkeypatch, tmp_path, all_mode):
    """A working-tree view of a.py and b.py on disk with its fingerprint taken; returns (st, refreshes)"""
    for name in ("a.py", "b.py", "index"):
        (tmp_path / name).write_text(name)
    monkeypatch.setattr(commands.git, "index_path", lambda repo: str(tmp_path / "index"))
    refreshes = []
    monkeypatch.setattr(commands, "refresh_listing", lambda view, st: refreshes.append(st))
    st = state.ViewState(str(tmp_path))
    st.files = [change("a.py"), change("b.py")]
    st.change, st.all_mode = st.files[0], all_mode
    st.fingerprint = commands.render.fingerprint(st)
    return st, refreshes


def touch(path):
    """Push the file's mtime forward, as an edit would"""
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))


def test_refresh_if_changed_watches_every_file_of_the_all_files_view(monkeypatch, tmp_path):
    st, refreshes = watched(monkeypatch, tmp_path, all_mode=True)
    commands.refresh_if_changed(sublime.View(), st)
    assert refreshes == []
    touch(tmp_path / "b.py")  # not the file under the caret
    commands.refresh_if_changed(sublime.View(), st)
    assert refreshes == [st]


def test_refresh_if_changed_watches_the_shown_file_and_the_index(monkeypatch, tmp_path):
    st, refreshes = watched(monkeypatch, tmp_path, all_mode=False)
    touch(tmp_path / "b.py")  # another file: the single-file view does not show it
    commands.refresh_if_changed(sublime.View(), st)
    assert refreshes == []
    touch(tmp_path / "index")
    commands.refresh_if_changed(sublime.View(), st)
    assert refreshes == [st]


def test_staging_the_only_file_leaves_an_empty_listing(monkeypatch):
    messages = []
    monkeypatch.setattr(commands.render, "render_message", lambda view, st, text: messages.append(text))
    assert press(monkeypatch, "stage", flagged(False), "f0.py", []) == []
    assert messages == ["No uncommitted changes in /repo"]


def all_files_view():
    """An all-files view of a.py (two hunks) and b.py (one), rendered with the caret at the top"""
    from vdiff import parse
    lines_a = [parse.Line(" ", i, i, f"a{i}") for i in range(1, 30)]
    lines_a[2] = parse.Line("+", None, 3, "new a3")
    lines_a[25] = parse.Line("+", None, 26, "new a26")
    lines_b = [parse.Line(" ", 1, 1, "b1"), parse.Line("-", 2, None, "old b2")]
    view = sublime.View()
    st = state.ViewState("/repo")
    st.all_mode = True
    st.files = [change("a.py"), change("b.py")]
    st.all_diffs = {path: (lines, parse.change_blocks(lines), False)
                    for path, lines in (("a.py", lines_a), ("b.py", lines_b))}
    commands.render.render_all(view, st)
    return view, st


def heading_row(st, path):
    return next(i for i, (c, _, section) in enumerate(st.all_rows) if section and c.path == path)


def put_caret(view, row):
    view.sel().clear()
    view.sel().add(sublime.Region(commands.render.index_point(view, row)))


def test_all_files_step_change_from_a_heading_lands_on_its_first_hunk():
    view, st = all_files_view()
    rows = dict(zip(st.all_change_row_targets, st.all_change_rows))

    # from the top, outside any file: the very first hunk, not the second
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == rows[("a.py", 0)]

    # from a.py's heading: its first hunk
    put_caret(view, heading_row(st, "a.py"))
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == rows[("a.py", 0)]

    # from b.py's heading back: the hunk before b.py's first, a.py's last
    put_caret(view, heading_row(st, "b.py"))
    commands.ACTIONS["prev_change"](view, None, st)
    assert commands.render.caret_index(view) == rows[("a.py", 1)]

    # from a hunk: the next one, across files
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == rows[("b.py", 0)]


def test_arrows_do_nothing_in_the_single_file_view():
    view, st = at_change([(2, 4)], 2)
    st.change = change("a.py")
    commands.ACTIONS["collapse"](view, None, st)
    assert st.collapsed == set() and not st.all_mode and st.all_rows == []


def test_git_action_refresh_stays_in_the_all_files_view(monkeypatch):
    view, st = all_files_view()
    st.change = st.files[1]
    monkeypatch.setattr(commands, "run_async", lambda work, done: done(work()))
    monkeypatch.setattr(commands.git, "changed_files", lambda repo: [change("a.py"), change("b.py")])
    shown = []
    monkeypatch.setattr(commands, "show_all", lambda view, st, focus=None: shown.append(focus))
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: shown.append(("file", c.path)))
    commands.refresh_listing(view, st, pick=None)
    assert shown == ["b.py"] and st.change.path == "b.py"


def test_all_files_step_file_from_nothing_and_with_no_files():
    view, st = all_files_view()
    commands.ACTIONS["next_file"](view, None, st)
    assert st.change.path == "a.py"
    st.change = None
    put_caret(view, 0)
    commands.ACTIONS["prev_file"](view, None, st)
    assert st.change.path == "b.py"
    st.files = []
    commands.ACTIONS["next_file"](view, None, st)  # no ZeroDivisionError


def test_all_files_context_zero_shows_only_changed_lines(monkeypatch):
    option(monkeypatch, "all_files_context", 0)
    view, st = all_files_view()
    shown = [line for _, line, _ in st.all_rows if line is not None]
    assert shown and all(line.kind != " " for line in shown)
