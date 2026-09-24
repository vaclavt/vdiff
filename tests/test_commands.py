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
