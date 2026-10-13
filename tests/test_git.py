import os
import re
import subprocess

import pytest

from vdiff import git, parse


def sh(repo, *args):
    subprocess.run(["git"] + list(args), cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(repo, name, text):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


@pytest.fixture
def repo(tmp_path):
    repo = str(tmp_path)
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.email", "t@example.com")
    sh(repo, "config", "user.name", "Test")
    write(repo, "a.py", "def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    write(repo, "b.txt", "keep\n")
    write(repo, "gone.py", "bye\n")
    sh(repo, "add", ".")
    sh(repo, "commit", "-q", "-m", "initial")
    return repo


def test_repo_root(repo):
    assert git.repo_root(os.path.join(repo, "a.py")) == os.path.realpath(repo)
    assert git.repo_root(repo) == os.path.realpath(repo)
    assert git.repo_root("/") is None


def test_changed_files_all_kinds(repo):
    write(repo, "a.py", "def f():\n    return 10\n\n\ndef g():\n    return 2\n")  # unstaged M
    write(repo, "b.txt", "keep\nmore\n")
    sh(repo, "add", "b.txt")  # staged M
    os.remove(os.path.join(repo, "gone.py"))  # unstaged D
    write(repo, "sub/new.py", "x = 1\ny = 2")  # untracked, no trailing newline
    write(repo, "c.txt", "c\n")
    sh(repo, "add", "c.txt")
    write(repo, "c.txt", "c\nd\n")  # partially staged: staged A, unstaged M
    changes = {c.path: c for c in git.changed_files(repo)}
    assert set(changes) == {"a.py", "b.txt", "c.txt", "gone.py", "sub/new.py"}
    assert changes["a.py"] == git.FileChange("a.py", "M", False, 1, 1, None, unstaged=True)
    assert changes["b.txt"] == git.FileChange("b.txt", "M", True, 1, 0, None)
    assert changes["c.txt"] == git.FileChange("c.txt", "M", True, 2, 0, None, unstaged=True)
    assert changes["gone.py"].status == "D" and changes["gone.py"].deletions == 1
    assert changes["sub/new.py"] == git.FileChange("sub/new.py", "?", False, 2, 0, None, unstaged=True)


def test_worktree_stats_counts_a_partially_staged_file_in_both_and_a_binary_in_neither_total():
    changes = [git.FileChange("a.py", "M", False, 1, 1, None, unstaged=True),
               git.FileChange("b.txt", "M", True, 1, 0, None),
               git.FileChange("c.txt", "M", True, 2, 0, None, unstaged=True),
               git.FileChange("img.png", "M", True, None, None, None)]
    assert git.worktree_stats(changes) == git.WorkTree(staged=3, unstaged=2, files=4, insertions=4, deletions=1)
    assert git.worktree_stats([]) == git.WorkTree(0, 0, 0, 0, 0)


def test_full_diff_working_tree(repo):
    write(repo, "a.py", "def f():\n    return 10\n\n\ndef g():\n    return 2\n")
    diff = parse.parse_unified(git.full_diff(repo, "a.py"))
    assert [l.kind for l in diff.lines] == [" ", "-", "+", " ", " ", " ", " "]
    assert diff.lines[2].text == "    return 10"
    write(repo, "n.py", "one\ntwo\n")
    new = parse.parse_unified(git.full_diff(repo, "n.py", untracked=True))
    assert [l.text for l in new.lines] == ["one", "two"] and all(l.kind == "+" for l in new.lines)


def test_stage_unstage_discard(repo):
    write(repo, "a.py", "changed\n")
    write(repo, "n.py", "new\n")
    changes = {c.path: c for c in git.changed_files(repo)}
    git.stage(repo, changes["a.py"])
    changes = {c.path: c for c in git.changed_files(repo)}
    assert changes["a.py"].staged
    git.unstage(repo, changes["a.py"])
    changes = {c.path: c for c in git.changed_files(repo)}
    assert not changes["a.py"].staged
    git.discard(repo, changes["a.py"])
    git.discard(repo, git.FileChange("n.py", "?", False, 1, 0, None))
    assert git.changed_files(repo) == []
    assert not os.path.exists(os.path.join(repo, "n.py"))
    # staged new file: discard removes it from index and disk
    write(repo, "s.py", "s\n")
    git.stage(repo, git.FileChange("s.py", "?", False, 1, 0, None))
    change = git.changed_files(repo)[0]
    assert change.status == "A"
    git.discard(repo, change)
    assert git.changed_files(repo) == []


def test_stage_all_unstage_all(repo):
    write(repo, "a.py", "changed\n")
    os.remove(os.path.join(repo, "gone.py"))
    write(repo, "sub/n.py", "new\n")
    git.stage_all(repo)
    changes = {c.path: c for c in git.changed_files(repo)}
    assert set(changes) == {"a.py", "gone.py", "sub/n.py"} and all(c.staged for c in changes.values())
    git.unstage_all(repo)
    changes = {c.path: c for c in git.changed_files(repo)}
    assert set(changes) == {"a.py", "gone.py", "sub/n.py"} and not any(c.staged for c in changes.values())
    assert changes["sub/n.py"].status == "?"


def test_index_path_finds_a_worktrees_index(repo, tmp_path_factory):
    assert git.index_path(repo) == os.path.join(repo, ".git", "index")
    # a worktree's .git is a file pointing into the main repository
    tree = str(tmp_path_factory.mktemp("tree") / "wt")
    sh(repo, "worktree", "add", "-q", tree)
    index = git.index_path(tree)
    assert os.path.isfile(os.path.join(tree, ".git")) and os.path.isfile(index)
    assert os.path.realpath(index).startswith(os.path.realpath(os.path.join(repo, ".git", "worktrees")))


def test_commits(repo):
    write(repo, "a.py", "def f():\n    return 10\n\n\ndef g():\n    return 2\n")
    write(repo, "c.py", "c\n")
    os.remove(os.path.join(repo, "gone.py"))
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "second\n\nbody text")
    commits = git.recent_commits(repo, 10)
    assert [c.subject for c in commits] == ["second", "initial"]
    info, files = git.commit_with_files(repo, commits[0].sha)
    assert info.subject == "second" and info.body.strip() == "body text" and info.author == "Test"
    assert info.sha == commits[0].sha and len(info.date) == 16
    files = {c.path: c for c in files}
    assert files["a.py"] == git.FileChange("a.py", "M", False, 1, 1, None)
    assert files["c.py"].status == "A" and files["c.py"].insertions == 1
    assert files["gone.py"].status == "D" and files["gone.py"].deletions == 1
    diff = parse.parse_unified(git.full_diff(repo, "a.py", sha=commits[0].sha))
    assert [l.kind for l in diff.lines] == [" ", "-", "+", " ", " ", " ", " "]
    # root commit diffs against the empty tree
    _, root_files = git.commit_with_files(repo, commits[1].sha)
    assert {c.path: c.status for c in root_files} == {"a.py": "A", "b.txt": "A", "gone.py": "A"}
    # the short form resolves too (used by "review HEAD")
    info, _ = git.commit_with_files(repo, "HEAD")
    assert info.sha == commits[0].sha


def test_recent_commits_carry_shortstat_totals(repo):
    write(repo, "new.py", "one\ntwo\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "added a file")
    write(repo, "a.py", "x\n")  # 6 lines become 1
    write(repo, "b.txt", "y\n")  # 1 line becomes 1
    sh(repo, "commit", "-q", "-am", "changed two files")

    newest, added, initial = git.recent_commits(repo, 3)
    assert (newest.files, newest.insertions, newest.deletions) == (2, 2, 7)
    # a pure addition: git omits the deletions clause entirely, which reads as zero, not unknown
    assert (added.files, added.insertions, added.deletions) == (1, 2, 0)
    assert initial.subject == "initial" and initial.files == 3


def test_commits_without_a_diffstat_report_no_totals(repo):
    sh(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    sh(repo, "checkout", "-q", "-b", "side")
    write(repo, "s.py", "s\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "on the side")
    sh(repo, "checkout", "-q", "main")
    sh(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")

    totals = {c.subject: (c.files, c.insertions, c.deletions) for c in git.recent_commits(repo, 5)}
    # git log prints no diffstat for either, so the totals stay unknown rather than a false zero
    assert totals["merge side"] == (None, None, None)
    assert totals["empty"] == (None, None, None)
    assert totals["on the side"] == (1, 1, 0)


def test_no_head_repo(tmp_path):
    repo = str(tmp_path)
    sh(repo, "init", "-q")
    write(repo, "x.py", "x\n")
    assert git.recent_commits(repo, 5) == []
    changes = git.changed_files(repo)
    assert changes[0].status == "?"
    git.stage(repo, changes[0])
    changes = git.changed_files(repo)
    assert changes[0].status == "A" and changes[0].staged
    diff = parse.parse_unified(git.full_diff(repo, "x.py"))
    assert [l.kind for l in diff.lines] == ["+"]
    git.unstage(repo, changes[0])
    assert git.changed_files(repo)[0].status == "?"
    git.stage_all(repo)
    assert git.changed_files(repo)[0].staged
    git.unstage_all(repo)
    assert git.changed_files(repo)[0].status == "?"


def test_rename_in_commit(repo):
    sh(repo, "mv", "b.txt", "moved.txt")
    sh(repo, "commit", "-q", "-m", "rename")
    _, files = git.commit_with_files(repo, "HEAD")
    assert files == [git.FileChange("moved.txt", "R", False, 0, 0, "b.txt")]


def test_git_error_message(repo):
    with pytest.raises(git.GitError) as info:
        git.run(repo, ["rev-parse", "--verify", "nope"])
    assert "\n" not in str(info.value) and info.value.stderr


def test_count_lines(tmp_path):
    p = str(tmp_path / "f")
    open(p, "wb").write(b"a\n" * 3000000)
    assert git._count_lines(p) == 3000000
    open(p, "wb").write(b"x\0y")
    assert git._count_lines(p) is None
    open(p, "wb").write(b"")
    assert git._count_lines(p) == 0


def test_commit_only_records_staged_files(repo):
    write(repo, "a.py", "staged\n")
    write(repo, "b.txt", "left alone\n")
    changes = {c.path: c for c in git.changed_files(repo)}
    git.stage(repo, changes["a.py"])

    git.commit(repo, "a message with 'quotes' and \"doubles\"")

    left = {c.path: c for c in git.changed_files(repo)}
    assert "a.py" not in left and not left["b.txt"].staged
    info, files = git.commit_with_files(repo, "HEAD")
    # the message reaches git verbatim: it is one argv element, never a shell string
    assert info.subject == "a message with 'quotes' and \"doubles\""
    assert [f.path for f in files] == ["a.py"]


def test_commit_without_staged_files_raises(repo):
    write(repo, "a.py", "unstaged only\n")
    with pytest.raises(git.GitError) as info:
        git.commit(repo, "nothing to record")
    assert info.value.code != 0


def message(repo):
    """HEAD's raw message, as git stores it"""
    return git.run(repo, ["log", "-1", "--format=%B"]).strip("\n")


def test_head_message_returns_the_whole_message(repo):
    assert git.head_message(repo) == "initial"

    sh(repo, "commit", "-q", "--allow-empty", "-m", "second", "-m", "body text\nsecond line")
    assert git.head_message(repo) == "second\n\nbody text\nsecond line"


def test_amend_keeps_everything_below_the_first_line_byte_for_byte(repo):
    body = "Closes #12.\n\n- a\n- b"
    sh(repo, "commit", "-q", "--allow-empty", "-m", "typo hree", "-m", body)
    before = git.run(repo, ["rev-list", "--count", "HEAD"])
    _, newline, rest = git.head_message(repo).partition("\n")

    git.amend(repo, "typo here" + newline + rest, allow_empty=True)

    # the paragraph the user never touched comes back exactly as git stored it
    assert message(repo) == "typo here\n\n" + body
    # an amend rewrites HEAD, it does not add a commit
    assert git.run(repo, ["rev-list", "--count", "HEAD"]) == before


def test_amend_without_a_body_leaves_a_one_line_message(repo):
    git.amend(repo, "reworded")
    assert message(repo) == "reworded"


def test_amend_rewords_an_empty_commit(repo):
    # git refuses an amend that leaves nothing behind unless --allow-empty says otherwise
    sh(repo, "commit", "-q", "--allow-empty", "-m", "marker")
    git.amend(repo, "marker, reworded", allow_empty=True)
    assert message(repo) == "marker, reworded"


def test_amend_that_would_empty_the_commit_is_refused(repo):
    # folding staged files in keeps git's guard: only a reword opts out of it
    write(repo, "new.py", "temporary\n")
    sh(repo, "add", "new.py")
    sh(repo, "commit", "-q", "-m", "add new.py")
    sh(repo, "rm", "-q", "new.py")

    with pytest.raises(git.GitError):
        git.amend(repo, "add new.py")


def test_amend_folds_staged_files_into_head(repo):
    write(repo, "a.py", "amended\n")
    changes = {c.path: c for c in git.changed_files(repo)}
    git.stage(repo, changes["a.py"])
    before = git.run(repo, ["rev-list", "--count", "HEAD"])

    git.amend(repo, "initial, with a.py")

    assert git.run(repo, ["rev-list", "--count", "HEAD"]) == before
    assert not git.changed_files(repo)
    assert message(repo) == "initial, with a.py"


def test_head_message_without_a_commit_raises(tmp_path):
    fresh = str(tmp_path)
    sh(fresh, "init", "-q", "-b", "main")
    with pytest.raises(git.GitError):
        git.head_message(fresh)


# --- branches ---------------------------------------------------------------


@pytest.fixture
def remote(repo, tmp_path_factory):
    """A bare `origin` holding main, with the repo's main tracking it"""
    bare = str(tmp_path_factory.mktemp("origin"))
    sh(bare, "init", "-q", "--bare", "-b", "main")
    sh(repo, "remote", "add", "origin", bare)
    sh(repo, "push", "-q", "-u", "origin", "main")
    return bare


def clone(bare, tmp_path_factory):
    """A second working copy of `bare`, to push foreign commits from"""
    other = str(tmp_path_factory.mktemp("other"))
    sh(other, "clone", "-q", bare, ".")
    sh(other, "config", "user.email", "o@example.com")
    sh(other, "config", "user.name", "Other")
    return other


def commit_file(repo, name, text, message):
    write(repo, name, text)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)


def by_name(repo):
    return {r.name: r for r in git.branches(repo)}


def test_branches_lists_locals_remotes_and_tracking(repo, remote, tmp_path_factory):
    sh(repo, "branch", "feature/x")
    sh(repo, "push", "-q", "-u", "origin", "feature/x")
    sh(repo, "branch", "local-only")
    commit_file(repo, "ahead.txt", "a\n", "ahead")
    other = clone(remote, tmp_path_factory)
    commit_file(other, "behind.txt", "b\n", "behind")
    sh(other, "push", "-q", "origin", "main")
    sh(other, "push", "-q", "origin", "--delete", "feature/x")
    sh(repo, "fetch", "-q", "--prune")
    # the remote's symbolic HEAD is a row of for-each-ref too
    sh(repo, "remote", "set-head", "origin", "main")

    refs = by_name(repo)
    assert set(refs) == {"main", "feature/x", "local-only", "origin/main"}
    main = refs["main"]
    assert main.current and main.remote is None and main.upstream == "origin/main"
    assert (main.ahead, main.behind, main.gone) == (1, 1, False)
    assert refs["feature/x"].gone and refs["feature/x"].upstream == "origin/feature/x"
    assert refs["local-only"].upstream is None and not refs["local-only"].current
    assert (main.upstream_remote, main.upstream_branch) == ("origin", "main")
    assert refs["local-only"].upstream_remote is None and refs["local-only"].upstream_branch is None
    origin_main = refs["origin/main"]
    assert (origin_main.remote, origin_main.branch, origin_main.current) == ("origin", "main", False)
    assert len(origin_main.sha) >= 7
    # locals come before remotes, in git's own order
    assert [r.name for r in git.branches(repo)] == ["feature/x", "local-only", "main", "origin/main"]


def test_remotes_lists_a_remote_without_refs(repo, remote):
    sh(repo, "remote", "add", "github", "/nowhere")
    assert git.remotes(repo) == ["github", "origin"]


def test_head_sha_on_a_branch_detached_and_without_commits(repo, tmp_path):
    sha = git.head_sha(repo)
    assert len(sha) >= 7 and by_name(repo)["main"].current
    sh(repo, "checkout", "-q", "--detach")
    assert git.head_sha(repo) == sha and not any(r.current for r in git.branches(repo))
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    sh(empty, "init", "-q", "-b", "main")
    assert git.head_sha(empty) == "" and git.branches(empty) == []


def test_ref_log_carries_tags_dates_and_stats(repo, remote):
    commit_file(repo, "new.py", "one\ntwo\n", "added a file")
    sh(repo, "tag", "v1.0.0")
    sh(repo, "tag", "-a", "release", "-m", "annotated")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "empty")

    empty, added, initial = git.ref_log(repo, "main", 10)
    assert (empty.subject, empty.tags, empty.files) == ("empty", (), None)
    assert added.tags == ("release", "v1.0.0") or added.tags == ("v1.0.0", "release")
    assert (added.files, added.insertions, added.deletions) == (1, 2, 0)
    assert re.fullmatch(r"\d\d\.\d\d\.\d{4}", added.date) and added.author == "Test"
    assert initial.tags == () and initial.files == 3
    # a remote ref logs too, and the count caps the list
    assert [c.subject for c in git.ref_log(repo, "origin/main", 1)] == ["initial"]
    # the recent-commits panel keeps its relative dates and carries the same tags
    recent = git.recent_commits(repo, 2)
    assert recent[0].tags == () and recent[1].tags == added.tags and recent[0].date.endswith("ago")


def test_checkout_tracks_a_remote_branch_once_and_reuses_the_local(repo, remote, tmp_path_factory):
    other = clone(remote, tmp_path_factory)
    sh(other, "checkout", "-q", "-b", "topic")
    commit_file(other, "t.txt", "t\n", "topic work")
    sh(other, "push", "-q", "-u", "origin", "topic")
    sh(repo, "fetch", "-q")

    refs = by_name(repo)
    assert "topic" not in refs
    git.checkout(repo, refs["origin/topic"], local_names=[])
    refs = by_name(repo)
    assert refs["topic"].current and refs["topic"].upstream == "origin/topic"
    assert os.path.exists(os.path.join(repo, "t.txt"))
    # back on main, the remote row now checks out the existing local branch rather than failing
    git.checkout(repo, refs["main"])
    assert by_name(repo)["main"].current
    git.checkout(repo, refs["origin/topic"], local_names=["topic"])
    assert by_name(repo)["topic"].current


def test_checkout_detached(repo, remote):
    git.checkout_detached(repo, "origin/main")
    refs = by_name(repo)
    assert not any(r.current for r in refs.values()) and git.head_sha(repo) == refs["origin/main"].sha


def test_delete_branch_refuses_unmerged_then_forces(repo):
    sh(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "s.txt", "s\n", "side work")
    sh(repo, "checkout", "-q", "main")
    with pytest.raises(git.GitError) as caught:
        git.delete_branch(repo, "side")
    assert "not fully merged" in caught.value.stderr
    assert "side" in by_name(repo)
    git.delete_branch(repo, "side", force=True)
    assert "side" not in by_name(repo)
    # a merged branch goes without force
    sh(repo, "branch", "merged")
    git.delete_branch(repo, "merged")
    assert "merged" not in by_name(repo)


def test_fetch_into_fast_forwards_and_refuses_non_ff(repo, remote, tmp_path_factory):
    sh(repo, "checkout", "-q", "-b", "work")
    other = clone(remote, tmp_path_factory)
    commit_file(other, "n.txt", "n\n", "upstream work")
    sh(other, "push", "-q", "origin", "main")

    git.fetch_into(repo, "origin", "main", "main")
    refs = by_name(repo)
    assert refs["main"].sha == refs["origin/main"].sha and refs["main"].ahead == 0
    # diverge main, then the fast-forward is refused
    sh(repo, "checkout", "-q", "main")
    commit_file(repo, "m.txt", "m\n", "local work")
    sh(repo, "checkout", "-q", "work")
    commit_file(other, "o.txt", "o\n", "more upstream work")
    sh(other, "push", "-q", "origin", "main")
    with pytest.raises(git.GitError):
        git.fetch_into(repo, "origin", "main", "main")
    # nor does git update the checked-out branch this way
    with pytest.raises(git.GitError):
        git.fetch_into(repo, "origin", "main", "work")


def test_pull_fast_forwards_the_current_branch(repo, remote, tmp_path_factory):
    other = clone(remote, tmp_path_factory)
    commit_file(other, "n.txt", "n\n", "upstream work")
    sh(other, "push", "-q", "origin", "main")
    git.pull(repo)
    assert os.path.exists(os.path.join(repo, "n.txt"))
    assert by_name(repo)["main"].behind == 0


def test_push_sets_the_upstream_once(repo, remote):
    sh(repo, "checkout", "-q", "-b", "topic")
    commit_file(repo, "t.txt", "t\n", "topic work")
    assert by_name(repo)["topic"].upstream is None
    git.push(repo, "origin", "topic", set_upstream=True)
    topic = by_name(repo)["topic"]
    assert topic.upstream == "origin/topic" and (topic.ahead, topic.behind) == (0, 0)
    commit_file(repo, "t.txt", "tt\n", "more topic work")
    assert by_name(repo)["topic"].ahead == 1
    git.push(repo, "origin", "topic")
    assert by_name(repo)["topic"].ahead == 0


def test_push_goes_to_a_differently_named_upstream(repo, remote):
    sh(repo, "checkout", "-q", "-b", "foo")
    commit_file(repo, "f.txt", "f\n", "foo work")
    sh(repo, "push", "-q", "-u", "origin", "foo:bar")
    foo = by_name(repo)["foo"]
    assert (foo.upstream, foo.upstream_remote, foo.upstream_branch) == ("origin/bar", "origin", "bar")
    commit_file(repo, "f.txt", "ff\n", "more foo work")
    git.push(repo, "origin", "foo", remote_branch="bar")
    refs = by_name(repo)
    assert refs["foo"].ahead == 0 and "origin/foo" not in refs
    # an upstream that is a local branch has no remote
    sh(repo, "branch", "--set-upstream-to=main", "foo")
    foo = by_name(repo)["foo"]
    assert (foo.upstream, foo.upstream_remote, foo.upstream_branch) == ("main", None, None)


def test_fetch_all_prunes_a_deleted_remote_branch(repo, remote, tmp_path_factory):
    other = clone(remote, tmp_path_factory)
    sh(other, "checkout", "-q", "-b", "gone")
    commit_file(other, "g.txt", "g\n", "to be deleted")
    sh(other, "push", "-q", "origin", "gone")
    git.fetch_all(repo)
    assert "origin/gone" in by_name(repo)
    sh(other, "push", "-q", "origin", "--delete", "gone")
    git.fetch_all(repo)
    assert "origin/gone" not in by_name(repo)


def test_run_passes_env_and_closes_stdin(repo):
    assert git.run(repo, ["var", "GIT_EDITOR"], env={"GIT_EDITOR": "xyz-editor"}).strip() == "xyz-editor"
    # the inherited environment stays: git still finds its config and the user's PATH
    assert git.run(repo, ["config", "user.name"]).strip() == "Test"


def test_ref_paths_point_into_the_git_dir(repo, tmp_path_factory):
    paths = git.ref_paths(repo)
    assert [os.path.relpath(p, repo) for p in paths] == [".git/" + name for name in git.REF_PATH_NAMES]
    assert os.path.exists(paths[0]) and os.path.isdir(paths[4])
    assert git.ref_paths(str(tmp_path_factory.mktemp("not-a-repo"))) == ()
    # the directories a slashed branch and a remote put their refs in: those are the ones their updates touch
    sh(repo, "branch", "feature/x")
    sh(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    nested = [os.path.relpath(p, repo) for p in git.ref_paths(repo)[len(git.REF_PATH_NAMES):]]
    assert nested == [".git/refs/heads/feature", ".git/refs/remotes/origin"]


def test_ref_paths_walk_again_only_when_a_ref_directory_moved(repo, monkeypatch):
    sh(repo, "branch", "feature/x")
    git.ref_paths(repo)
    walks = []
    real_walk = os.walk
    monkeypatch.setattr(git.os, "walk", lambda root, *args: walks.append(root) or real_walk(root, *args))
    git.ref_paths(repo)
    assert walks == []  # nothing moved: the directories found last time stand
    # a branch two levels down adds a directory inside a nested one, whose mtime moves
    sh(repo, "branch", "feature/deep/y")
    nested = [os.path.relpath(p, repo) for p in git.ref_paths(repo)[len(git.REF_PATH_NAMES):]]
    assert walks and nested == [".git/refs/heads/feature", ".git/refs/heads/feature/deep"]


def test_run_keeps_gits_messages_in_english(repo):
    # a localized git would translate the refusal delete_branch tells apart by its text
    assert "LC_ALL=C" in git.run(repo, ["-c", "alias.showenv=!env", "showenv"]).split("\n")
