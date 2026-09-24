import pytest

import sublime  # the stub, via conftest

from vdiff import commands, git, state


def change(path, status="M"):
    return git.FileChange(path, status, False, 1, 1, None)


def flush_previews():
    """The stub runs set_timeout synchronously, so previews are already shown"""


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


def test_on_cancel_goes_back_to_commit_list(monkeypatch):
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: None)
    st = state.ViewState("/repo")
    kept = st.enter("abc", None)
    window = sublime.Window()
    back = []
    commands.show_files_panel(window, sublime.View(), st, [change("a.py")], kept, on_cancel=lambda: back.append(1))
    window.panel[1](-1)
    assert back == [1] and st.sha is None


def test_step_change_uses_blocks():
    view = sublime.View()
    st = state.ViewState("/repo")
    st.blocks = [(2, 4), (7, 8)]
    view.text = "\n" + "\n".join("l{}".format(i) for i in range(10))
    view.sel().add(sublime.Region(commands.render.index_point(view, 0)))
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == 2
    commands.ACTIONS["next_change"](view, None, st)
    assert commands.render.caret_index(view) == 7
    commands.ACTIONS["prev_change"](view, None, st)
    assert commands.render.caret_index(view) == 2


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
    st.files = [git.FileChange("f{}.py".format(i), "M", flag, 1, 1, None) for i, flag in enumerate(flags)]
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

    The stub runs set_timeout synchronously, which is why the real plugin's async window - the one
    n / p used to read stale state through - is invisible to every other test in the suite.
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
    return [git.FileChange("f{}.py".format(i), "M", False, 1, 1, None) for i in range(count)]


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
