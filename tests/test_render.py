import json
import os
import re

import sublime  # the stub, via conftest

from vdiff import commands, git, listeners, palette, parse, render, state  # noqa: F401

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


def with_loaded_override(view):
    """Model a view whose scheme has picked up the override palette.py writes

    The stub no more simulates that merge than sublime does before a restart, so a test says so itself.
    """
    view.override_loaded = True
    return view


def test_render_text_regions_and_header():
    view = with_loaded_override(sublime.View())
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


# --- key help popup ---------------------------------------------------------


def help_popup(st):
    """Open the popup through the action registry and return (view, html)"""
    view = sublime.View()
    render.init_view(view)
    commands.ACTIONS["help"](view, None, st)
    return view, view.popup


def test_help_popup_lists_every_key():
    st = make_state()
    view, html = help_popup(st)

    assert view.is_popup_visible()
    for group in ("Navigate", "Panels", "Actions", "View"):
        assert f">{group}<" in html
    for keys in ("] [", "n p", "o ⏎", "j k", "f", "c", "s", "u", "d", "C", "m", "r", "?", "esc"):
        assert f">{keys}<" in html
    assert "next / previous change" in html and "open the real file at this line" in html
    # working-tree mode: nothing dimmed, every single-action row is a link
    assert 'class="dim"' not in html and "uncommitted only" not in html
    assert 'href="stage"' in html and 'href="files"' in html and 'href="close"' in html
    # rows covering two actions stay plain text
    assert 'href="next_change"' not in html
    # the status bar advertises it too
    assert "? keys" in view.status[render.STATUS_KEY]


def test_help_popup_dims_file_actions_in_commit_mode():
    st = make_state()
    st.enter("abc", git.CommitInfo("abc", "abc1234", "Ann", "2026-09-23 10:00", "Fix things", ""))
    _, html = help_popup(st)

    assert "uncommitted only" in html
    assert html.count('class="dim"') == 5
    assert 'href="stage"' not in html and 'href="unstage"' not in html and 'href="discard"' not in html
    assert 'href="commit"' not in html and 'href="amend"' not in html
    # everything that still applies stays clickable
    assert 'href="files"' in html and 'href="open"' in html and 'href="refresh"' in html


def test_help_popup_toggles_and_navigates():
    st = make_state()
    view, _ = help_popup(st)

    commands.ACTIONS["help"](view, None, st)
    assert not view.is_popup_visible()

    commands.ACTIONS["help"](view, None, st)
    assert view.is_popup_visible()
    view.popup_navigate("files")
    assert not view.is_popup_visible()
    assert view.commands[-1] == ("vdiff_action", {"action": "files"})


# keymap spellings that the popup shows as a symbol
DISPLAY_KEYS = {"enter": "⏎", "escape": "esc"}


def keymap_bindings():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Default.sublime-keymap")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_keymap_matches_help_table():
    """The keymap, ACTIONS and the popup agree on both the actions and the keys that reach them"""
    bindings = keymap_bindings()

    assert all(b["command"] == "vdiff_action" for b in bindings)
    bound = {b["args"]["action"] for b in bindings}
    listed = {action for k in render.KEYS for action in k.actions}
    assert bound == set(commands.ACTIONS) == listed

    # rebinding a key without updating the table would leave the popup and status bar lying
    bound_keys = {DISPLAY_KEYS.get(key, key) for b in bindings for key in b["keys"]}
    assert bound_keys == {token for k in render.KEYS for token in k.keys.split()}


def test_no_binding_yields_to_vintage_command_mode():
    """The review view is read-only, so its letters are vdiff commands in either vim mode

    init_view opens in command mode; a binding still carrying the old `command_mode: false` context
    would go dead there, which is what broke n / p.
    """
    guard = {"key": "setting.command_mode", "operand": False}
    for binding in keymap_bindings():
        assert guard not in binding["context"], binding["keys"]
    view = sublime.View()
    render.init_view(view)
    assert view.settings().get("command_mode") is True


def test_minihtml_stylesheets_reject_unsupported_selectors():
    """minihtml drops the rest of a stylesheet at an unparsable selector; these forms broke HELP_CSS"""
    # descendant/child/sibling combinators, pseudo-classes, attribute selectors, compound classes
    unsupported = re.compile(r"[\s:>~+\[]|[.#][^.#]*[.#]")
    sheets = [(name, css) for name, css in vars(render).items() if name.endswith("_CSS") and isinstance(css, str)]
    assert len(sheets) >= 2, sheets
    for name, css in sheets:
        for rule in css.split("}"):
            head = rule.split("{")[0].strip()
            for selector in filter(None, (s.strip() for s in head.split(","))):
                assert not unsupported.search(selector), f"{name}: {selector}"


# --- header summary line ----------------------------------------------------


def test_summary_counts_staged_and_unstaged_files():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    st.files = [
        git.FileChange("a.py", "M", True, 1, 1, None),
        git.FileChange("b.py", "M", False, 1, 1, None),
        git.FileChange("c.py", "?", False, 1, 0, None),
    ]
    render.render(view, st)
    summary = header(view).split('<div class="summary">')[1].split("</div>")[0]

    # the untracked file's whole content counts as insertions, the way git.changed_files records it
    assert summary.startswith("1 staged, 2 unstaged · ")
    assert '<span class="del">-2</span>' in summary and '<span class="ins">+3</span>' in summary


def test_summary_totals_skip_binary_files_but_still_count_them():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    st.files = [
        git.FileChange("a.py", "M", False, 4, 1, None),
        git.FileChange("logo.png", "M", False, None, None, None),  # binary: no counts, still a file
    ]
    render.render(view, st)
    summary = header(view).split('<div class="summary">')[1].split("</div>")[0]

    assert summary.startswith("0 staged, 2 unstaged · ")
    assert '<span class="del">-1</span>' in summary and '<span class="ins">+4</span>' in summary


def test_summary_totals_a_commit_and_owns_the_date():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    st.enter("abc", git.CommitInfo("abc", "abc1234", "Ann", "2026-09-23 10:00", "Fix things", ""))
    st.change = git.FileChange("x.py", "M", False, 3, 2, None)
    st.files = [
        git.FileChange("x.py", "M", False, 3, 2, None),
        git.FileChange("y.py", "M", False, 7, 0, None),
        git.FileChange("logo.png", "M", False, None, None, None),  # binary: no counts, still a file
    ]
    render.render(view, st)
    html = header(view)

    # hash, author, date, totals and message all live on the one summary line
    summary = html.split('<div class="summary">')[1].split("</div>")[0]
    for part in ("abc1234", "Ann", "2026-09-23 10:00", "3 files changed", "-2", "+10", "Fix things"):
        assert part in summary, part
    # and nowhere else: there is no second commit line any more
    assert html.count("abc1234") == 1 and html.count("2026-09-23 10:00") == 1
    assert 'class="commit"' not in html


def test_summary_is_omitted_without_a_file_list():
    view = sublime.View()
    render.init_view(view)
    st = make_state()
    render.render(view, st)
    assert 'class="summary"' not in header(view)


def test_flash_tints_the_buffer_then_clears(monkeypatch):
    view = sublime.View("some text")
    timers = []
    monkeypatch.setattr(render.sublime, "set_timeout", lambda fn, ms=0: timers.append((fn, ms)))

    render.flash(view)
    regions, scope, _, flags = view.regions[render.BELL_KEY]
    assert regions == [sublime.Region(0, len(view.text))] and flags == sublime.DRAW_NO_OUTLINE
    assert scope and timers[0][1] == render.BELL_MS

    timers[0][0]()
    assert render.BELL_KEY not in view.regions


def test_flash_survives_a_re_render(monkeypatch):
    """The wrap that rings also re-renders; _replace must not wipe the bell before it is seen"""
    assert render.BELL_KEY not in render.REGION_KEYS
    monkeypatch.setattr(render.sublime, "set_timeout", lambda fn, ms=0: None)  # hold the bell up
    view = sublime.View()
    render.init_view(view)
    render.flash(view)
    render.render(view, make_state())
    assert render.BELL_KEY in view.regions


def test_bell_scope_falls_back_when_the_scheme_lacks_it():
    view = with_loaded_override(sublime.View())
    assert render._bell_scope(view) == render.SCOPE_BELL
    del view.styles["region.yellowish"]
    # an invisible bell is a silent failure, so fall back to a scope that is resolving
    assert render._bell_scope(view) == palette.SCOPE_INS
    # ... and when the plugin's own scopes are not resolving either, to the scheme's
    view.override_loaded = False
    assert render._bell_scope(view) == "diff.inserted"


def token_regions(view, kind, colour):
    """The regions painted for one (background kind, colour) pair of the token layer"""
    scope = palette.scope_for(kind, colour)
    entry = view.regions.get(scope and scope.replace("vdiff.", "vdiff.tok.", 1))
    return entry[0] if entry else []


def test_changed_lines_are_repainted_in_the_files_own_colours():
    view = with_loaded_override(sublime.View())
    render.init_view(view)
    render.render(view, make_state())

    keyword = view.styles["keyword"]["foreground"]
    punctuation = view.styles["punctuation.definition"]["foreground"]
    # "    return 10" is inserted: its words carry the keyword colour, its spaces and digits their
    # own runs. Without this layer all of them would be the tint's flat foreground.
    inserted = token_regions(view, "ins", keyword)
    assert sublime.Region(view.text_point(4, 4), view.text_point(4, 10)) in inserted
    # and a deleted line is painted from the deleted palette, not the inserted one
    assert sublime.Region(view.text_point(2, 4), view.text_point(2, 10)) in token_regions(view, "del", keyword)
    assert token_regions(view, "del", punctuation)

    # the plain tint still covers the whole line underneath, so gaps and line ends stay coloured
    assert view.regions[render.KEY_INS][1] == palette.SCOPE_INS


def test_token_runs_merge_but_stop_at_a_word_diff_range():
    view = with_loaded_override(sublime.View())
    render.init_view(view)
    render.render(view, make_state("--- a/y\n+++ b/y\n@@ -1 +1 @@\n-foo = bar\n+foo = baz\n"))

    keyword = view.styles["keyword"]["foreground"]
    # only the changed character sits in the stronger tint; the rest of the word keeps the line tint
    assert token_regions(view, "ins.char", keyword) == [sublime.Region(view.text_point(2, 8), view.text_point(2, 9))]
    plain = token_regions(view, "ins", keyword)
    assert sublime.Region(view.text_point(2, 6), view.text_point(2, 8)) in plain
    assert sublime.Region(view.text_point(2, 0), view.text_point(2, 3)) in plain


def test_token_layer_waits_for_the_scopes_to_resolve():
    view = sublime.View()
    render.init_view(view)
    render.render(view, make_state())

    # the override is not loaded yet, so the lines keep the flat tint and no token key is painted:
    # an unknown scope would draw its text in sublime's marker green
    assert view.regions[render.KEY_DEL][1] == "diff.deleted"
    assert view.regions[render.KEY_INS][1] == "diff.inserted"
    assert not [key for key in view.regions if key.startswith("vdiff.tok.")]


def test_token_layer_is_skipped_for_a_diff_too_big_to_read(monkeypatch):
    monkeypatch.setattr(render, "TOKEN_COLOUR_MAX_CHARS", 4)
    view = with_loaded_override(sublime.View())
    render.init_view(view)
    render.render(view, make_state())

    assert not [key for key in view.regions if key.startswith("vdiff.tok.")]


def test_token_regions_are_dropped_when_a_re_render_no_longer_needs_them():
    view = with_loaded_override(sublime.View())
    render.init_view(view)
    render.render(view, make_state())
    assert [key for key in view.regions if key.startswith("vdiff.tok.")]

    render.render(view, make_state("--- a/y\n+++ b/y\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"))
    keyword = view.styles["keyword"]["foreground"]
    # the new diff paints far fewer runs; the keys the old one used must not linger
    assert token_regions(view, "ins", keyword) == [sublime.Region(view.text_point(3, 0), view.text_point(3, 1))]


def test_restart_notice_when_the_override_cannot_take_effect_yet(monkeypatch):
    said = []
    monkeypatch.setattr(sublime, "status_message", said.append)
    view = sublime.View()
    render.init_view(view)
    render.render(view, make_state())

    assert said == ["vDiff: " + render.NEEDS_RESTART]
    # said once per scheme, not once per render
    render.render(view, make_state())
    assert said == ["vDiff: " + render.NEEDS_RESTART]


