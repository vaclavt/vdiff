import pytest

import sublime  # the stub, via conftest

from conftest import put_caret  # noqa: E402
from test_render import header, with_loaded_override  # noqa: E402
from vdiff import branches, commands, git, listeners, palette, render, state


def ref(name, remote=None, upstream=None, ahead=0, behind=0, gone=False, current=False, sha="abc1234", upstream_remote=""):
    """A Ref; the upstream's remote is read off its name unless given (None for a local upstream)"""
    branch = name.partition("/")[2] if remote else name
    if upstream_remote == "":
        upstream_remote = upstream.partition("/")[0] if upstream else None
    return git.Ref(name, branch, remote, sha, upstream, ahead, behind, gone, current, upstream_remote)


def commit(subject, tags=(), files=8, insertions=260, deletions=58, sha="1" * 40):
    return git.Commit(sha, sha[:7], "vtvrdik", "20.12.2025", subject, files, insertions, deletions, tuple(tags))


REFS = [
    ref("feature"),
    ref("main", upstream="origin/main", ahead=1, current=True),
    ref("github/main", remote="github"),
    ref("origin/main", remote="origin"),
]
COMMITS = [
    commit("default file view mode and some colors now configurable in settings", tags=("v1.1.0",), sha="a" * 40),
    commit("next commit message", files=None, insertions=None, deletions=None, sha="b" * 40),
]


def make_state(refs=REFS, commits=COMMITS, selected="main", remotes=("origin", "github")):
    st = state.ViewState("/repo/vdiff")
    st.mode = state.MODE_BRANCHES
    st.branch_refs = list(refs)
    st.branch_remotes = list(remotes)
    st.branch_commits = list(commits)
    st.branch_ref = selected
    st.branch_head_sha = "abc1234"
    return st


def rendered(st=None, live=False):
    """A drawn branches view; `live` models a scheme that has picked up the override, as after a restart"""
    view = with_loaded_override(sublime.View()) if live else sublime.View()
    render.init_view(view)
    st = st or make_state()
    branches.render_branches(view, st)
    return view, st


def rows(view):
    return view.text.split("\n")[1:]


class Calls:
    """Records the git helper calls an action makes; `answer` is what a worker returns"""

    def __init__(self, monkeypatch, answer=None):
        self.calls = []
        self.answer = answer
        for name in ("checkout", "checkout_detached", "pull", "fetch_into", "push", "fetch_all", "delete_branch"):
            monkeypatch.setattr(branches.git, name, self._record(name))
        self.loads = record_loads(monkeypatch)

    def _record(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args[1:], kwargs))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        return call


def record_loads(monkeypatch):
    """Replace branches.load with a recorder of (branch, mode, message, keep_caret) per call"""
    loads = []

    def fake_load(view, st, message=None, keep_caret=False):
        loads.append((st.branch_ref, st.mode, message, keep_caret))

    monkeypatch.setattr(branches, "load", fake_load)
    return loads


@pytest.fixture
def calls(monkeypatch):
    return Calls(monkeypatch)


@pytest.fixture
def loads(monkeypatch):
    return record_loads(monkeypatch)


@pytest.fixture
def dialogs(monkeypatch):
    monkeypatch.setattr(sublime, "dialogs", [])
    monkeypatch.setattr(sublime, "dialog_answer", True)
    return sublime.dialogs


# --- drawing ----------------------------------------------------------------


def test_two_columns_are_laid_out_as_in_the_mockup():
    view, st = rendered()
    lines = rows(view)
    # the left column is as wide as its longest cell plus the padding; the gap follows it
    width = len("  feature") + branches.LEFT_PAD
    assert all(line[width:width + 2] == ("├─" if line.endswith("─") else "│ ") for line in lines)
    right = 100 - width - 2
    # the working tree comes first, five rows like a commit; nothing changed here
    assert lines[0] == f"{'BRANCHES':<{width}}│ No Changes"
    assert lines[1] == f"{'  feature':<{width}}│ "
    # main is one commit ahead of its upstream: the count sits on the right edge of its cell
    assert lines[2] == f"{'* main':<{width - 3}}↑1 │ "
    assert lines[3] == f"{'REMOTES':<{width}}│ "
    assert lines[4] == f"{'origin':<{width}}├─" + "─" * right
    assert lines[5] == f"{'  main':<{width}}│ default file view mode and some colors now configurable in settings"
    assert lines[6].startswith(f"{'github':<{width}}│ Author:      vtvrdik")
    # the tag sits on the right edge of the author row
    assert lines[6].endswith("v1.1.0") and len(lines[6]) == width + 2 + right
    assert lines[7] == f"{'  main':<{width}}│ Date:        20.12.2025"
    # the left column ran out, so the cells are blank
    assert lines[8] == f"{'':<{width}}│ Stats:       8 files changed -58 +260"
    assert lines[9] == f"{'':<{width}}├─" + "─" * right
    assert lines[10] == f"{'':<{width}}│ next commit message"
    assert lines[11].rstrip() == f"{'':<{width}}│ Author:      vtvrdik"
    # no diffstat for a merge or an empty commit
    assert lines[13] == f"{'':<{width}}│ Stats:       (no diffstat)"
    assert lines[14] == f"{'':<{width}}├─" + "─" * right
    assert len(lines) == 15
    assert view.syntax == "scope:text.plain"
    assert view.name_ == "⟐ branches"


CHANGES = [
    git.FileChange("a.py", "M", False, 1, 1, None, unstaged=True),
    git.FileChange("b.txt", "M", True, 1, 0, None),
    git.FileChange("c.txt", "M", True, 2, 0, None, unstaged=True),  # partially staged: counted on both sides
    git.FileChange("img.png", "M", True, None, None, None),  # binary: no counts
]


def test_the_working_tree_entry_counts_the_staged_and_unstaged_files_and_their_diffstat():
    st = make_state()
    st.branch_changes = list(CHANGES)
    view, st = rendered(st)
    lines = rows(view)
    width = len("  feature") + branches.LEFT_PAD
    assert lines[0] == f"{'BRANCHES':<{width}}│ 3 staged files, 2 unstaged files"
    assert lines[1] == f"{'  feature':<{width}}│ Stats:       4 files changed -1 +4"
    assert lines[2] == f"{'* main':<{width - 3}}↑1 │ " and lines[3] == f"{'REMOTES':<{width}}│ "
    assert lines[4].endswith("─" * 10) and lines[5].endswith("│ default file view mode and some colors now configurable in settings")
    # its counts are coloured like the commits', in row order
    assert [view.text[r.a:r.b] for r in view.regions[render.KEY_BRANCH_DEL][0]] == ["-1", "-58"]
    assert [view.text[r.a:r.b] for r in view.regions[render.KEY_BRANCH_INS][0]] == ["+4", "v1.1.0", "+260"]
    # the singular
    st.branch_changes = [CHANGES[0]]
    branches.render_branches(view, st)
    assert rows(view)[0] == f"{'BRANCHES':<{width}}│ 0 staged files, 1 unstaged file"
    st.branch_changes = [CHANGES[1]]
    branches.render_branches(view, st)
    assert rows(view)[0] == f"{'BRANCHES':<{width}}│ 1 staged file, 0 unstaged files"


def test_local_branches_show_their_standing_against_the_upstream():
    refs = [ref("level", upstream="origin/level"), ref("ahead", upstream="origin/ahead", ahead=3, current=True),
            ref("both", upstream="origin/both", ahead=12, behind=4), ref("lost", upstream="origin/lost", gone=True),
            ref("untracked"), ref("origin/level", remote="origin")]
    view, st = rendered(make_state(refs=refs, selected="ahead", remotes=("origin",)))
    width = len("  both ↑12 ↓4") + branches.LEFT_PAD
    cells = [line[:width] for line in rows(view)]
    # in the listing's order; level and untracked branches carry nothing, nor do remote ones
    assert cells[1:6] == [f"{'  level':<{width}}", f"{'* ahead':<{width - 3}}↑3 ", f"{'  both':<{width - 7}}↑12 ↓4 ",
                          f"{'  lost':<{width - 5}}gone ", f"{'  untracked':<{width}}"]
    assert cells[7] == f"{'origin':<{width}}" and cells[8] == f"{'  level':<{width}}"
    # ahead is green, behind and gone red; the selected branch's standing is painted apart, on its background
    def painted(key):
        """The coloured texts under a key"""
        return [view.text[r.a:r.b] for r in view.regions[key][0]]

    assert painted(render.KEY_BRANCH_STANDING_INS) == ["↑12"] and painted(render.KEY_BRANCH_STANDING_DEL) == ["↓4", "gone"]
    assert painted(render.KEY_BRANCH_SEL_INS) == ["↑3"] and painted(render.KEY_BRANCH_SEL_DEL) == []
    # moving the selection moves the standing between the keys
    branches.ACTIONS["next_branch"](view, None, st)
    assert st.branch_ref == "both"
    assert painted(render.KEY_BRANCH_STANDING_INS) == ["↑3"] and painted(render.KEY_BRANCH_STANDING_DEL) == ["gone"]
    assert painted(render.KEY_BRANCH_SEL_INS) == ["↑12"] and painted(render.KEY_BRANCH_SEL_DEL) == ["↓4"]
    # a long name gives way to its counts, never the other way round
    long = ref("feature/" + "x" * 60, upstream="origin/x", behind=2)
    view, st = rendered(make_state(refs=[ref("main", current=True), long], selected="main"))
    cell = rows(view)[2][:branches.LEFT_MAX]
    assert cell.endswith("↓2 ") and "…" in cell and len(cell) == branches.LEFT_MAX


def test_rows_know_their_branch_and_commit():
    view, st = rendered()
    # item 0 is the working tree, the commits follow from 1
    assert [(r.ref and r.ref.name, r.commit) for r in st.branch_rows] == [
        (None, 0), ("feature", 0), ("main", 0), (None, 0), (None, 0),
        ("origin/main", 1), (None, 1), ("github/main", 1), (None, 1), (None, 1),
        (None, 2), (None, 2), (None, 2), (None, 2), (None, 2),
    ]


def test_a_remote_without_fetched_branches_keeps_its_row():
    view, st = rendered(make_state(refs=[ref("main", current=True)], remotes=("origin", "gitea")))
    names = [line[:st.branch_width].rstrip() for line in rows(view)]
    assert names[:5] == ["BRANCHES", "* main", "REMOTES", "origin", "gitea"]


def test_long_branch_names_are_clipped_at_the_cap():
    long = "feature/" + "x" * 60
    view, st = rendered(make_state(refs=[ref("main", current=True), ref(long)], selected="main"))
    width = branches.LEFT_MAX
    assert rows(view)[2].startswith(f"{'  ' + long[:width - 4]}…" + " │ ") or rows(view)[2][width:width + 2] == "│ "
    assert rows(view)[2][:width].rstrip().endswith("…")


def test_selection_headings_tags_and_counts_are_painted():
    view, st = rendered()
    width = len("  feature") + branches.LEFT_PAD
    starts = render._row_starts_for_text(rows(view))
    selected, scope, _, _ = view.regions[render.KEY_BRANCH_SEL]
    assert selected == [sublime.Region(starts[2], starts[2] + width)]
    assert scope == render._file_header_scope(view)
    assert [view.text[r.a:r.b] for r in view.regions[render.KEY_BRANCH_DEL][0]] == ["-58"]
    # the tags are green like the insertions, in row order; main's ↑1 sits on the selected row
    assert [view.text[r.a:r.b] for r in view.regions[render.KEY_BRANCH_INS][0]] == ["v1.1.0", "+260"]
    assert [view.text[r.a:r.b] for r in view.regions[render.KEY_BRANCH_SEL_INS][0]] == ["↑1"]
    assert view.regions[render.KEY_BRANCH_STANDING_INS][0] == []
    # filled regions, since only those take the scope's text colour; the scheme's tint stands in until the
    # override's red/green-on-background rules are live, after which the text is red / green on the editor
    # background, or on the heading background along the selected row so its tint runs through the standing
    assert not view.regions[render.KEY_BRANCH_DEL][3] & sublime.DRAW_NO_FILL
    assert view.regions[render.KEY_BRANCH_DEL][1] == palette.SCOPE_DEL
    assert view.regions[render.KEY_BRANCH_INS][1] == palette.SCOPE_INS
    assert view.regions[render.KEY_BRANCH_SEL_INS][1] == palette.SCOPE_INS
    live, _ = rendered(live=True)
    assert live.regions[render.KEY_BRANCH_DEL][1] == palette.SCOPE_TEXT_DEL
    assert live.regions[render.KEY_BRANCH_INS][1] == palette.SCOPE_TEXT_INS
    assert live.regions[render.KEY_BRANCH_STANDING_DEL][1] == palette.SCOPE_TEXT_DEL
    assert live.regions[render.KEY_BRANCH_SEL_DEL][1] == palette.SCOPE_FILE_DEL_COUNT
    assert live.regions[render.KEY_BRANCH_SEL_INS][1] == palette.SCOPE_FILE_INS_COUNT


def test_caret_starts_on_the_working_tree_entry_and_stays_on_its_item_across_a_refresh():
    view, st = rendered()
    assert view.sel()[0].a == render.index_point(view, 0) and st.branch_rows[0].commit == branches.WORKTREE
    branches.ACTIONS["next_commit"](view, None, st)
    assert render.caret_index(view) == branches.ROWS_PER_COMMIT
    branches.render_branches(view, st, keep_caret=True)
    assert render.caret_index(view) == branches.ROWS_PER_COMMIT
    # a plain redraw goes back to the top
    branches.render_branches(view, st)
    assert render.caret_index(view) == 0


def test_no_commits_and_no_branches_still_draw():
    view, st = rendered(make_state(refs=[], commits=[], selected=None, remotes=()))
    rule = "─" * (100 - len("BRANCHES  │ "))
    assert rows(view) == ["BRANCHES  │ No Changes", "REMOTES   │ ", "          │ ", "          │ ", "          ├─" + rule,
                          "          │ (no commits)"]
    # the one item to step through is the working tree
    assert view.sel()[0].a == render.index_point(view, 0)
    branches.ACTIONS["next_commit"](view, None, st)
    assert render.caret_index(view) == 0
    assert view.regions[render.KEY_BRANCH_SEL][0] == []
    assert "no branch selected" in header(view)


def test_render_sets_the_mode_and_persists_the_branch():
    view, st = rendered()
    assert view.settings().get(state.MODE_SETTING) == "branches"
    assert view.settings().get(state.VIEW_DATA) == {"repo": "/repo/vdiff", "sha": None, "mode": "branches", "ref": "main"}
    assert view.status[render.STATUS_KEY] == render.key_hints("branches")
    assert st.fingerprint == ((), (None,))  # no ref paths and no index outside a repository
    # a diff render switches back
    st.change = git.FileChange("x.py", "M", False, 1, 1, None)
    render.render_message(view, st, "nothing")
    assert view.settings().get(state.MODE_SETTING) == "diff"
    assert "mode" not in view.settings().get(state.VIEW_DATA)
    assert view.status[render.STATUS_KEY] == render.key_hints()
    assert render.KEY_BRANCH_SEL not in view.regions


def test_an_unchanged_header_is_not_redrawn():
    view, st = rendered()
    first = view.phantoms[render.PHANTOM_KEY][0]
    # the same header over the same text keeps the phantom in place
    render._header(view, st, st.header[0])
    assert view.phantoms[render.PHANTOM_KEY] == [first]
    # new text may have dropped the phantom, so it is drawn again even for the same header
    branches.render_branches(view, st, keep_caret=True)
    assert view.phantoms[render.PHANTOM_KEY][0] is not first and len(view.phantoms[render.PHANTOM_KEY]) == 1


def test_header_shows_head_upstream_and_the_links_of_the_selected_kind():
    view, st = rendered()
    text = header(view)
    assert "Branches · vdiff" in text and ">main<" in text and "on <span" in text
    # the standing as the cell shows it: a zero count is left out
    assert "→ origin/main" in text and '<span class="ins">↑1</span>' in text and "↓0" not in text
    st.branch_refs[1] = ref("main", upstream="origin/main", behind=2, current=True)
    branches.render_branches(view, st)
    assert '<span class="del">↓2</span>' in header(view) and "↑0" not in header(view)
    st.branch_refs[1] = ref("main", upstream="origin/main", current=True)
    branches.render_branches(view, st)
    assert "up to date" in header(view)
    st.branch_refs[1] = REFS[1]
    branches.render_branches(view, st)
    for link in ("checkout", "pull", "push", "fetch", "delete_branch", "files", "commits", "branches", "help"):
        assert f'href="{link}"' in text
    assert 'href="checkout_detached"' not in text

    st.branch_ref = "origin/main"
    branches.render_branches(view, st)
    text = header(view)
    assert "remote branch of origin" in text and 'href="checkout_detached"' in text
    assert 'href="delete_branch"' not in text and 'href="pull"' not in text

    st.branch_ref = "feature"
    st.branch_refs[1] = ref("main", upstream="origin/main", ahead=1)  # nothing checked out: detached
    branches.render_branches(view, st)
    text = header(view)
    assert "detached at" in text and "abc1234" in text and "→" not in text
    st.branch_head_sha = ""
    branches.render_branches(view, st)
    assert "no commits" in header(view)

    st.branch_refs[0] = ref("feature", upstream="origin/feature", gone=True)
    branches.render_branches(view, st)
    assert ">gone<" in header(view)


# --- navigation -------------------------------------------------------------


def test_j_and_k_step_commits_and_ring_at_the_ends(rings):
    view, st = rendered()
    # the working tree, then the two commits
    branches.ACTIONS["prev_commit"](view, None, st)
    assert len(rings) == 1 and render.caret_index(view) == 0
    branches.ACTIONS["next_commit"](view, None, st)
    assert render.caret_index(view) == 5
    branches.ACTIONS["next_commit"](view, None, st)
    assert render.caret_index(view) == 10
    branches.ACTIONS["next_commit"](view, None, st)
    assert len(rings) == 2 and render.caret_index(view) == 10
    branches.ACTIONS["prev_commit"](view, None, st)
    assert render.caret_index(view) == 5
    # off the items (the caret moved by the arrows onto a left-column-only row): k goes to the last, j to the first
    view, st = rendered(make_state(refs=REFS + [ref("x"), ref("y"), ref("z")], commits=COMMITS[:1]))
    assert st.branch_rows[10].commit == -1
    put_caret(view, 10)
    branches.ACTIONS["prev_commit"](view, None, st)
    assert render.caret_index(view) == 5
    put_caret(view, 10)
    branches.ACTIONS["next_commit"](view, None, st)
    assert render.caret_index(view) == 0


def test_J_and_K_select_branches_skipping_headings_and_reload_the_log(monkeypatch, rings):
    logged = []
    monkeypatch.setattr(branches.git, "ref_log", lambda repo, name, count: logged.append(name) or [COMMITS[1]])
    view, st = rendered()
    assert st.branch_ref == "main"
    branches.ACTIONS["prev_branch"](view, None, st)
    assert st.branch_ref == "feature" and logged == ["feature"]
    branches.ACTIONS["prev_branch"](view, None, st)
    assert st.branch_ref == "feature" and len(rings) == 1
    for expected in ("main", "github/main", "origin/main"):
        branches.ACTIONS["next_branch"](view, None, st)
        assert st.branch_ref == expected
    branches.ACTIONS["next_branch"](view, None, st)
    assert st.branch_ref == "origin/main" and len(rings) == 2
    # the stub runs the worker at once, so the log was redrawn with the new selection
    assert rows(view)[5].endswith("│ next commit message")
    selected = view.regions[render.KEY_BRANCH_SEL][0]
    assert view.text[selected[0].a:selected[0].b].strip() == "main"
    # the caret went back to the working-tree entry
    assert render.caret_index(view) == 0 and st.branch_rows[0].commit == branches.WORKTREE


def test_a_stale_log_is_dropped(monkeypatch):
    pending = []
    monkeypatch.setattr(branches, "run_async", lambda work, done: pending.append((work, done)))
    monkeypatch.setattr(branches.git, "ref_log", lambda repo, name, count: [commit(name)])
    view, st = rendered()
    branches.ACTIONS["prev_branch"](view, None, st)  # feature
    branches.ACTIONS["next_branch"](view, None, st)  # main
    assert [name for name in (st.branch_ref,)] == ["main"] and len(pending) == 2
    # the older answer lands first, but the token moved on
    work, done = pending[0]
    done(work())
    assert "│ feature" not in view.text
    work, done = pending[1]
    done(work())
    assert rows(view)[5].endswith("│ main")


def test_load_lists_everything_and_picks_the_branch(monkeypatch):
    monkeypatch.setattr(branches.git, "head_sha", lambda repo: "abc1234")
    monkeypatch.setattr(branches.git, "ref_paths", lambda repo: ())
    monkeypatch.setattr(branches.git, "branches", lambda repo: list(REFS))
    monkeypatch.setattr(branches.git, "remotes", lambda repo: ["origin", "github"])
    monkeypatch.setattr(branches.git, "ref_log", lambda repo, name, count: [commit(f"log of {name}")])
    monkeypatch.setattr(branches.git, "index_path", lambda repo: None)
    monkeypatch.setattr(branches.git, "changed_files", lambda repo: [CHANGES[2]])
    view = sublime.View()
    render.init_view(view)
    st = state.ViewState("/repo/vdiff")
    branches.load(view, st)
    # nothing wanted: the checked-out branch; the working tree above its log
    assert st.branch_ref == "main" and rows(view)[5].endswith("│ log of main")
    assert rows(view)[0].endswith("│ 1 staged file, 1 unstaged file") and rows(view)[1].endswith("│ Stats:       1 file changed -0 +2")
    assert st.branch_changes == [CHANGES[2]]
    assert st.branch_remotes == ["origin", "github"] and st.branch_head_sha == "abc1234"
    assert st.token == st.rendered_token
    # a wanted branch that vanished falls back the same way
    st.branch_ref = "deleted"
    branches.load(view, st)
    assert st.branch_ref == "main"
    st.branch_ref = "origin/main"
    branches.load(view, st)
    assert st.branch_ref == "origin/main" and rows(view)[5].endswith("│ log of origin/main")


def test_pick_prefers_the_wanted_then_the_current_then_the_first_local_then_the_first():
    assert branches._pick(REFS, "origin/main").name == "origin/main"
    assert branches._pick(REFS, "gone").name == "main"
    detached = [ref("feature"), ref("zeta"), ref("origin/x", remote="origin")]
    assert branches._pick(detached, None).name == "feature"
    assert branches._pick([ref("origin/x", remote="origin")], None).name == "origin/x"
    assert branches._pick([], None) is None


# --- actions ----------------------------------------------------------------


def test_checkout_routes_local_current_and_remote_branches(calls):
    view, st = rendered()
    branches.ACTIONS["checkout"](view, None, st)
    assert calls.calls == [] and calls.loads == []  # already on main

    st.branch_ref = "feature"
    branches.ACTIONS["checkout"](view, None, st)
    assert calls.calls[-1] == ("checkout", (REFS[0], ["feature", "main"]), {})
    assert calls.loads[-1] == ("feature", "branches", "checked out feature", False)

    st.branch_ref = "github/main"
    branches.ACTIONS["checkout"](view, None, st)
    assert calls.calls[-1][1][0].name == "github/main"
    assert st.branch_ref == "main"  # the local branch of that name is what got checked out

    st.branch_ref = "origin/main"
    branches.ACTIONS["checkout_detached"](view, None, st)
    assert calls.calls[-1] == ("checkout_detached", ("origin/main",), {})
    assert calls.loads[-1][2] == "checked out origin/main detached"
    st.branch_ref = "feature"
    branches.ACTIONS["checkout_detached"](view, None, st)
    assert calls.calls[-1][0] == "checkout_detached" and len(calls.calls) == 3  # refused for a local branch


def test_pull_pulls_the_current_branch_and_fast_forwards_another(calls):
    view, st = rendered(make_state(refs=REFS + [ref("tracked", upstream="origin/tracked", behind=2)]))
    branches.ACTIONS["pull"](view, None, st)
    assert calls.calls[-1] == ("pull", (), {}) and calls.loads[-1][2] == "pulled main"
    st.branch_ref = "tracked"
    branches.ACTIONS["pull"](view, None, st)
    assert calls.calls[-1] == ("fetch_into", ("origin", "tracked", "tracked"), {})
    assert calls.loads[-1][2] == "fast-forwarded tracked to origin/tracked"
    # the upstream's own name on its remote, not the local one
    st.branch_refs.append(ref("foo", upstream="origin/bar", behind=1))
    st.branch_ref = "foo"
    branches.ACTIONS["pull"](view, None, st)
    assert calls.calls[-1] == ("fetch_into", ("origin", "bar", "foo"), {})
    st.branch_refs.append(ref("local", upstream="main", upstream_remote=None, behind=1))
    for name in ("feature", "origin/main", "local"):
        st.branch_ref = name
        branches.ACTIONS["pull"](view, None, st)
    assert len(calls.calls) == 3  # no upstream, a remote row and a local upstream: all refused


def test_push_uses_the_upstream_or_sets_one_on_origin(calls):
    view, st = rendered()
    branches.ACTIONS["push"](view, None, st)
    assert calls.calls[-1] == ("push", ("origin", "main"), {"remote_branch": "main"}) and calls.loads[-1][2] == "pushed main to origin"
    # to the upstream itself when it is called differently there
    st.branch_refs.append(ref("foo", upstream="gitea/bar", ahead=2))
    st.branch_ref = "foo"
    branches.ACTIONS["push"](view, None, st)
    assert calls.calls[-1] == ("push", ("gitea", "foo"), {"remote_branch": "bar"})
    st.branch_ref = "feature"
    branches.ACTIONS["push"](view, None, st)
    assert calls.calls[-1] == ("push", ("origin", "feature"), {"set_upstream": True})
    # without origin the first remote takes it; without any, nothing happens; nor for a local upstream
    st.branch_remotes = ["gitea"]
    branches.ACTIONS["push"](view, None, st)
    assert calls.calls[-1] == ("push", ("gitea", "feature"), {"set_upstream": True})
    st.branch_remotes = []
    branches.ACTIONS["push"](view, None, st)
    st.branch_ref = "origin/main"
    st.branch_remotes = ["origin"]
    branches.ACTIONS["push"](view, None, st)
    st.branch_refs.append(ref("local", upstream="main", upstream_remote=None, ahead=1))
    st.branch_ref = "local"
    branches.ACTIONS["push"](view, None, st)
    assert len(calls.calls) == 4


def test_fetch_fetches_everything_and_keeps_the_cursor(calls):
    view, st = rendered()
    branches.ACTIONS["fetch"](view, None, st)
    assert calls.calls == [("fetch_all", (), {})] and calls.loads == [("main", "branches", "fetched all remotes", True)]


def test_delete_asks_then_deletes_and_asks_again_for_an_unmerged_branch(calls, dialogs, monkeypatch):
    view, st = rendered()
    st.branch_ref = "feature"
    branches.ACTIONS["delete_branch"](view, None, st)
    assert dialogs == [("Delete the branch feature?", "Delete")]
    assert calls.calls == [("delete_branch", ("feature",), {})] and calls.loads == [("feature", "branches", "deleted feature", False)]

    # not fully merged: a second question, then the forced deletion
    calls.answer = git.GitError("error: The branch 'feature' is not fully merged.\n", 1)
    del dialogs[:]
    branches.ACTIONS["delete_branch"](view, None, st)
    assert [d[1] for d in dialogs] == ["Delete", "Delete anyway"]
    assert calls.calls[-1] == ("delete_branch", ("feature",), {"force": True})

    # Cancel on the first question does nothing; so does a remote or the checked-out branch
    monkeypatch.setattr(sublime, "dialog_answer", False)
    count = len(calls.calls)
    branches.ACTIONS["delete_branch"](view, None, st)
    st.branch_ref = "main"
    branches.ACTIONS["delete_branch"](view, None, st)
    st.branch_ref = "origin/main"
    branches.ACTIONS["delete_branch"](view, None, st)
    assert len(calls.calls) == count


def test_other_git_errors_from_delete_still_reach_the_status_bar(calls, dialogs, monkeypatch):
    messages = []
    monkeypatch.setattr(branches, "status", lambda m: messages.append(m))
    monkeypatch.setattr("vdiff.utils.status", lambda m: messages.append(m))
    calls.answer = git.GitError("fatal: something else\n", 1)
    view, st = rendered()
    st.branch_ref = "feature"
    branches.ACTIONS["delete_branch"](view, None, st)
    assert messages[-1] == "fatal: something else" and calls.loads == []


def test_o_reviews_the_commit_under_the_caret():
    view, st = rendered()
    window = sublime.Window()
    # the working-tree entry opens the files view, which itself stays put when there is nothing to list
    branches.ACTIONS["open_commit"](view, window, st)
    assert window.commands == [("vdiff_files", None)]
    branches.ACTIONS["next_commit"](view, window, st)
    branches.ACTIONS["open_commit"](view, window, st)
    branches.ACTIONS["next_commit"](view, window, st)
    branches.ACTIONS["open_commit"](view, window, st)
    assert window.commands[1:] == [("vdiff_commits", {"sha": "a" * 40}), ("vdiff_commits", {"sha": "b" * 40})]
    # a row without an item (the left column outran the log) opens nothing
    view, st = rendered(make_state(refs=REFS + [ref("x"), ref("y"), ref("z")], commits=COMMITS[:1]))
    put_caret(view, 10)
    branches.ACTIONS["open_commit"](view, window, st)
    assert len(window.commands) == 3


def test_branch_actions_refuse_a_diff_view(calls):
    view, st = rendered()
    st.mode = state.MODE_DIFF
    for name in branches.ACTIONS:
        branches.ACTIONS[name](view, sublime.Window(), st)
    assert calls.calls == [] and calls.loads == []


# --- wiring -----------------------------------------------------------------


def test_b_opens_the_branches_command_and_refresh_reloads_in_place(loads):
    view, st = rendered()
    window = sublime.Window()
    commands.ACTIONS["branches"](view, window, st)
    assert window.commands == [("vdiff_branches", None)]
    commands.ACTIONS["refresh"](view, window, st)
    assert loads == [("main", "branches", None, True)]


def test_restore_view_comes_back_in_branches_mode(loads, tmp_path):
    view = sublime.View()
    view.settings().set(state.VIEW_DATA, {"repo": str(tmp_path), "sha": None, "mode": "branches", "ref": "feature"})
    st = commands.restore_view(view)
    assert loads == [("feature", "branches", None, False)] and view.settings().get(state.MODE_SETTING) == "branches"
    # while the load (stubbed here) is in flight an activation does not start a second one
    st.token += 1
    commands.refresh_if_changed(view, st)
    assert len(loads) == 1


def test_esc_from_the_files_panel_goes_back_to_the_branches(monkeypatch, loads):
    monkeypatch.setattr(commands, "show_file", lambda view, st, c: None)
    view, st = rendered()
    kept = st.enter()
    assert kept.mode == "branches"
    window = sublime.Window()
    files = [git.FileChange("a.py", "M", False, 1, 1, None)]
    commands.show_files_panel(window, view, st, files, kept, placeholder="x")
    _, on_select, _, on_highlight, _ = window.panel
    on_highlight(0)
    st.mode = state.MODE_DIFF  # the preview drew a diff
    on_select(-1)
    assert [m for _, m, _, _ in loads] == ["branches"] and st.in_branches


def test_keymap_context_answers_the_mode():
    listener = listeners.VdiffEventListener()
    view, st = rendered()
    assert view.settings().get(state.MODE_SETTING) == "branches"
    assert listener.on_query_context(view, "vdiff_mode", sublime.OP_EQUAL, "branches", False) is True
    assert listener.on_query_context(view, "vdiff_mode", sublime.OP_EQUAL, "diff", False) is False
    assert listener.on_query_context(view, "vdiff_mode", sublime.OP_NOT_EQUAL, "diff", False) is True
    assert listener.on_query_context(view, "vdiff_mode", sublime.OP_NOT_EQUAL, "branches", False) is False
    # other keys and other views are someone else's business
    assert listener.on_query_context(view, "setting.vdiff_mode", sublime.OP_EQUAL, "branches", False) is None
    assert listener.on_query_context(sublime.View(), "vdiff_mode", sublime.OP_EQUAL, "diff", False) is None
    # a review view without the setting (an older session) is a diff view
    plain = sublime.View()
    plain.settings().set(state.VIEW_FLAG, True)
    assert listener.on_query_context(plain, "vdiff_mode", sublime.OP_EQUAL, "diff", False) is True


def test_branches_view_reloads_when_the_refs_changed(monkeypatch):
    mtimes = {"/repo/.git/HEAD": 1.0, "/repo/.git/refs/heads": 2.0, "/repo/vdiff/a.py": 5.0}
    monkeypatch.setattr(branches.git, "ref_paths", lambda repo: ("/repo/.git/HEAD", "/repo/.git/refs/heads"))
    monkeypatch.setattr(branches.git, "index_path", lambda repo: "/repo/.git/index")
    monkeypatch.setattr(git, "mtime", lambda path: mtimes.get(path))
    loads = record_loads(monkeypatch)
    worktree_loads = []
    monkeypatch.setattr(branches, "load_worktree", lambda view, st: worktree_loads.append(st.branch_ref))
    st = make_state()
    st.branch_changes = [CHANGES[0]]
    view, st = rendered(st)
    st.rendered_token = st.token
    # the refs, then the changed files of the working-tree entry and the index they are staged in
    assert st.fingerprint == ((1.0, 2.0), (5.0, None))
    commands.refresh_if_changed(view, st)
    assert loads == [] and worktree_loads == []
    mtimes["/repo/.git/HEAD"] = 3.0  # a checkout elsewhere: everything is relisted
    commands.refresh_if_changed(view, st)
    assert loads == [("main", "branches", None, True)]
    st.rendered_token = st.token
    st.fingerprint = branches.fingerprint(st)
    # the changed file saved, something staged: only the working tree is relisted
    mtimes["/repo/vdiff/a.py"] = 6.0
    commands.refresh_if_changed(view, st)
    assert len(loads) == 1 and worktree_loads == ["main"]
    st.fingerprint = branches.fingerprint(st)
    mtimes["/repo/.git/index"] = 7.0
    commands.refresh_if_changed(view, st)
    assert len(loads) == 1 and worktree_loads == ["main", "main"]
    # not while a load is in flight
    st.token += 1
    mtimes["/repo/.git/HEAD"] = 4.0
    commands.refresh_if_changed(view, st)
    assert len(loads) == 1 and len(worktree_loads) == 2


def test_load_worktree_relists_only_the_changes_and_keeps_the_caret(monkeypatch):
    listed = []
    monkeypatch.setattr(branches.git, "changed_files", lambda repo: listed.append(repo) or [CHANGES[1]])
    monkeypatch.setattr(branches.git, "index_path", lambda repo: None)
    view, st = rendered()
    branches.ACTIONS["next_commit"](view, None, st)
    branches.load_worktree(view, st)
    assert listed == ["/repo/vdiff"] and st.branch_changes == [CHANGES[1]]
    assert rows(view)[0].endswith("│ 1 staged file, 0 unstaged files") and rows(view)[5].endswith("│ default file view mode and some colors now configurable in settings")
    assert render.caret_index(view) == 5 and st.token == st.rendered_token
    # a later load wins over a slower relisting
    pending = []
    monkeypatch.setattr(branches, "run_async", lambda work, done: pending.append((work, done)))
    branches.load_worktree(view, st)
    st.token += 1
    work, done = pending[0]
    done([])
    assert st.branch_changes == [CHANGES[1]]


def test_the_commit_cursor_tints_only_the_right_column_once_its_rule_is_live():
    # before the restart: Sublime's line highlight, no cursor regions
    view, st = rendered()
    assert view.settings().get("highlight_line") is True
    assert render.KEY_BRANCH_CURSOR not in view.regions
    # after it: the four cells of the caret's commit, from the gap to the end of the row
    view, st = rendered(live=True)
    assert view.settings().get("highlight_line") is False
    regions, scope, _, _ = view.regions[render.KEY_BRANCH_CURSOR]
    assert scope == palette.SCOPE_CURSOR
    width = len("  feature") + branches.LEFT_PAD + 2
    lines = rows(view)
    assert [view.text[r.a:r.b] for r in regions] == [line[width:] for line in lines[:4]]
    # the commits' counts and tags keep the plain red / green
    assert [view.text[r.a:r.b] for r in view.regions[render.KEY_BRANCH_INS][0]] == ["v1.1.0", "+260"]
    assert view.regions[render.KEY_BRANCH_INS][1] == palette.SCOPE_TEXT_INS
    assert view.regions[render.KEY_BRANCH_CURSOR_INS][0] == [] and view.regions[render.KEY_BRANCH_CURSOR_DEL][0] == []
    branches.ACTIONS["next_commit"](view, None, st)
    regions = view.regions[render.KEY_BRANCH_CURSOR][0]
    assert regions[0].a == render.index_point(view, 5) + width
    # the tint leaves out the counts and tags, which get the cursor's background with their own colour instead:
    # overlapping regions would let the later one recolour the text
    text = lambda key: [view.text[r.a:r.b] for r in view.regions[key][0]]  # noqa: E731
    assert text(render.KEY_BRANCH_CURSOR_INS) == ["v1.1.0", "+260"] and text(render.KEY_BRANCH_CURSOR_DEL) == ["-58"]
    assert view.regions[render.KEY_BRANCH_CURSOR_INS][1] == palette.SCOPE_CURSOR_INS
    assert text(render.KEY_BRANCH_INS) == [] and text(render.KEY_BRANCH_DEL) == []
    pieces = sorted(sum((list(view.regions[key][0]) for key in (render.KEY_BRANCH_CURSOR, render.KEY_BRANCH_CURSOR_INS,
                                                                 render.KEY_BRANCH_CURSOR_DEL)), []), key=lambda r: r.a)
    assert all(a.b <= b.a for a, b in zip(pieces, pieces[1:]))
    by_row = {}
    for r in pieces:
        by_row.setdefault(view.rowcol(r.a)[0] - 1, []).append(view.text[r.a:r.b])
    assert {row: "".join(parts) for row, parts in by_row.items()} == {i: lines[i][width:] for i in range(5, 9)}
    # back in a diff the line highlight returns
    st.change = git.FileChange("x.py", "M", False, 1, 1, None)
    render.render_message(view, st, "nothing")
    assert view.settings().get("highlight_line") is True


def test_holding_J_fetches_the_log_once_the_key_rests(monkeypatch):
    timers = []
    monkeypatch.setattr(sublime, "set_timeout", lambda fn, delay=0: timers.append((fn, delay)))
    logged = []
    monkeypatch.setattr(branches.git, "ref_log", lambda repo, name, count: logged.append(name) or [])
    view, st = rendered()
    branches.ACTIONS["prev_branch"](view, None, st)
    branches.ACTIONS["next_branch"](view, None, st)
    branches.ACTIONS["next_branch"](view, None, st)
    assert st.branch_ref == "github/main" and logged == []
    assert [delay for _, delay in timers] == [branches.DEBOUNCE_MS] * 3
    for fn, _ in timers:
        fn()
    # only the last selection's fetch ran; the cell moved at once each time
    assert logged == ["github/main"]
    selected = view.regions[render.KEY_BRANCH_SEL][0]
    assert view.text[selected[0].a:selected[0].b].strip() == "main"


def test_entering_the_branches_view_vacates_the_diff_state(monkeypatch):
    monkeypatch.setattr(branches, "run_async", lambda work, done: None)
    view = sublime.View()
    render.init_view(view)
    st = state.ViewState("/repo/vdiff")
    st.sha, st.change = "abc", git.FileChange("x.py", "M", False, 1, 1, None)
    st.files = [st.change]
    branches.load(view, st)
    assert st.in_branches and st.sha is None and st.change is None and st.requested is None and st.lines == []
    assert st.files == [st.change] or st.files == [git.FileChange("x.py", "M", False, 1, 1, None)]
