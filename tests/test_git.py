import os
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
    changes = {c.path: c for c in git.changed_files(repo)}
    assert set(changes) == {"a.py", "b.txt", "gone.py", "sub/new.py"}
    assert changes["a.py"] == git.FileChange("a.py", "M", False, 1, 1, None)
    assert changes["b.txt"] == git.FileChange("b.txt", "M", True, 1, 0, None)
    assert changes["gone.py"].status == "D" and changes["gone.py"].deletions == 1
    assert changes["sub/new.py"] == git.FileChange("sub/new.py", "?", False, 2, 0, None)


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
