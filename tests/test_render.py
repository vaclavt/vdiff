import sublime  # the stub, via conftest

from vdiff import commands, git, listeners, parse, render, state  # noqa: F401

DIFF = """--- a/x.py
+++ b/x.py
@@ -1,6 +1,7 @@
 def f():
-    return 1
-
+    return 10
+    # added
+    # more
 
 def g():
     pass
"""


def make_state(diff_text=DIFF):
    st = state.ViewState("/repo")
    st.change = git.FileChange("x.py", "M", False, 3, 2, None)
    diff = parse.parse_unified(diff_text)
    st.lines, st.binary = diff.lines, diff.binary
    st.blocks = parse.change_blocks(diff.lines)
    return st


def header(view):
    return view.phantoms[render.PHANTOM_KEY][0].content


def test_render_text_regions_and_header():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    render.render(view, st)

    assert view.text.split("\n") == ["", "def f():", "    return 1", "", "    return 10", "    # added", "    # more", "", "def g():", "    pass"]
    assert view.is_read_only()
    assert view.syntax.endswith("Python.sublime-syntax")

    deleted, scope, _, flags = view.regions[render.KEY_DEL]
    assert scope == "diff.deleted" and flags == sublime.DRAW_NO_OUTLINE
    # rows 2-3 merged into one region, newline included; offsets match the view's own row arithmetic
    assert deleted == [sublime.Region(view.text_point(2, 0), view.text_point(4, 0))]
    inserted = view.regions[render.KEY_INS][0]
    assert inserted == [sublime.Region(view.text_point(4, 0), view.text_point(7, 0))]

    marks = view.regions[render.KEY_INS_GUTTER]
    assert [r.a for r in marks[0]] == [view.text_point(4, 0), view.text_point(5, 0), view.text_point(6, 0)]
    assert marks[2] == "dot"

    # 2 deleted vs 3 inserted lines: not a paired block, no char marks
    assert view.regions[render.KEY_INS_CHAR][0] == []

    html = header(view)
    assert "x.py" in html and "-2" in html and "+3" in html and 'href="stage"' in html and "modified" in html
    assert len(view.phantoms[render.PHANTOM_KEY]) == 1
    assert view.name() == "⟐ x.py"
    assert view.settings().get(state.VIEW_DATA) == {"repo": "/repo", "sha": None, "path": "x.py", "status": "M", "staged": False}
    assert view.sel()[0].a == view.text_point(1, 0)


def test_last_line_region_stops_at_buffer_end():
    view = sublime.View()
    render.init_view(view)
    st = make_state("--- a/y\n+++ b/y\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n")
    render.render(view, st)
    assert view.text == "\na\nb\nc"
    assert view.regions[render.KEY_INS][0] == [sublime.Region(view.text_point(3, 0), len(view.text))]
    render.render(view, st)  # re-render replaces, never duplicates, the header
    assert len(view.phantoms[render.PHANTOM_KEY]) == 1


def test_char_marks_for_paired_lines():
    view = sublime.View()
    render.init_view(view)
    st = make_state("--- a/y\n+++ b/y\n@@ -1 +1 @@\n-foo = bar\n+foo = baz\n")
    render.render(view, st)
    assert view.regions[render.KEY_DEL_CHAR][0] == [sublime.Region(view.text_point(1, 8), view.text_point(1, 9))]
    assert view.regions[render.KEY_INS_CHAR][0] == [sublime.Region(view.text_point(2, 8), view.text_point(2, 9))]


def test_commit_header_and_message():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    st.enter("abc", git.CommitInfo("abc", "abc1234", "Ann <x>", "2026-09-23 10:00", "Fix things", "details\nmore"))
    st.change = git.FileChange("x.py", "M", False, 3, 2, None)
    render.render(view, st)
    html = header(view)
    assert "abc1234" in html and "Ann &lt;x&gt;" in html and "Fix things" in html and "details" in html
    assert 'href="stage"' not in html and 'href="open"' in html

    render.render_message(view, st, "nothing")
    assert view.text == "\nnothing" and st.change is None and view.name() == "vDiff"


def test_binary_and_caret_index():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    st.binary, st.lines, st.blocks = True, [], []
    render.render(view, st)
    assert view.text == "\n(binary file)" and render.KEY_DEL not in view.regions
    view.sel().clear()
    view.sel().add(sublime.Region(render.index_point(view, 0)))
    assert render.caret_index(view) == 0
    view.sel().clear()
    view.sel().add(sublime.Region(0))
    assert render.caret_index(view) == -1
