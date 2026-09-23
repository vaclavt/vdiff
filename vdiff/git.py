""" Git access: every call is one subprocess; results are plain tuples, no sublime imports (testable) """
import os
import subprocess
from collections import namedtuple

# path, one-letter status (M A D R ?), staged flag, numstat counts (None for binary), rename source
FileChange = namedtuple("FileChange", "path status staged insertions deletions old_path")
Commit = namedtuple("Commit", "sha short author date subject")
CommitInfo = namedtuple("CommitInfo", "sha short author date subject body")

# hash of the empty tree: the diff base for a repository without commits
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# huge context turns a unified diff into the whole file with changes interleaved
FULL_CONTEXT = "-U999999"

# overridable from settings (sublime's PATH may not have a user-installed git)
GIT_BINARY = "git"

STATUS_NAMES = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
    "U": "unmerged",
    "?": "untracked",
}

_INFO_FORMAT = "%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%b"
_DATE_FORMAT = "--date=format:%Y-%m-%d %H:%M"


class GitError(Exception):
    """git exited with an unexpected code; str() is the first stderr line, `stderr` the whole of it"""

    def __init__(self, stderr, code):
        self.stderr = stderr
        self.code = code
        first = stderr.strip().splitlines()
        super().__init__(first[0] if first else "git exited with {}".format(code))


def run(repo, args, ok_codes=(0,)):
    """Run git in `repo`, return stdout as text"""
    cmd = [GIT_BINARY, "-c", "core.quotepath=off", "--no-pager"] + list(args)
    proc = subprocess.run(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode not in ok_codes:
        raise GitError(proc.stderr.decode("utf-8", "replace"), proc.returncode)
    return proc.stdout.decode("utf-8", "replace")


def repo_root(path):
    """Top-level directory of the repository containing `path`, or None"""
    if not path:
        return None
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(directory):
        return None
    try:
        root = run(directory, ["rev-parse", "--show-toplevel"]).strip()
    except (GitError, OSError):
        return None
    return root or None


def has_head(repo):
    """False in a repository without commits"""
    try:
        run(repo, ["rev-parse", "--verify", "-q", "HEAD"])
        return True
    except GitError:
        return False


def base_ref(repo):
    """HEAD, or the empty tree when there is no commit yet"""
    return "HEAD" if has_head(repo) else EMPTY_TREE


def _parse_numstat(text):
    """`--numstat -z` output -> {path: (insertions, deletions, old_path)}"""
    result = {}
    fields = iter(text.split("\0"))
    for entry in fields:
        parts = entry.split("\t")
        if len(parts) < 3:
            continue
        ins, dele, path = parts[:3]
        old_path = None
        if path == "":
            # rename: counts, then old and new path as separate NUL fields
            old_path = next(fields, None)
            path = next(fields, "")
        result[path] = (None if ins == "-" else int(ins), None if dele == "-" else int(dele), old_path)
    return result


def _parse_name_status(text):
    """`--name-status -z` output -> [(status letter, path, old_path)]"""
    result = []
    fields = iter(text.split("\0"))
    for status in fields:
        if not status:
            continue
        path = next(fields, "")
        old_path = None
        if status[0] in "RC":
            old_path, path = path, next(fields, "")
        result.append((status[0], path, old_path))
    return result


def _count_lines(path):
    """Line count of a working-tree file, None when binary or unreadable"""
    try:
        with open(path, "rb") as f:
            head = f.read(8000)
            if b"\0" in head:
                return None
            count = head.count(b"\n")
            last = head[-1:]
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return None
    if not last:
        return 0
    return count + (0 if last == b"\n" else 1)


def changed_files(repo):
    """Working tree changes vs HEAD: staged, unstaged and untracked, sorted by path"""
    status = run(repo, ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--no-renames"])
    entries = [e for e in status.split("\0") if e]
    # "# branch.oid (initial)" means there is no commit yet
    base = EMPTY_TREE if "# branch.oid (initial)" in entries else "HEAD"
    counts = _parse_numstat(run(repo, ["diff", "--numstat", "-z", "--no-renames", base]))

    changes = []
    for entry in entries:
        kind = entry[0]
        if kind == "1":
            xy, path = entry.split(" ", 8)[1], entry.split(" ", 8)[-1]
        elif kind == "u":
            xy, path = "UU", entry.split(" ", 10)[-1]
        elif kind == "?":
            xy, path = ".?", entry[2:]
        else:
            continue
        x, y = xy[0], xy[1]
        staged = x not in (".", "?")
        letter = y if y != "." else x
        if letter == "?":
            lines = _count_lines(os.path.join(repo, path))
            ins, dele, old = lines, (None if lines is None else 0), None
        else:
            ins, dele, old = counts.get(path, (0, 0, None))
        changes.append(FileChange(path, letter, staged, ins, dele, old))
    changes.sort(key=lambda c: c.path)
    return changes


def recent_commits(repo, count):
    """Last `count` commits, newest first; empty list without commits"""
    if not has_head(repo):
        return []
    text = run(repo, ["log", "-n", str(count), "--format=%H%x1f%h%x1f%an%x1f%ar%x1f%s"])
    commits = []
    for line in text.split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 5:
            commits.append(Commit(*parts))
    return commits


def commit_with_files(repo, sha):
    """(CommitInfo, [FileChange]) of one commit vs its first parent, files sorted by path; two git calls"""
    show = ["show", "--first-parent", "-m", "-z"]
    text = run(repo, show + ["--format={}%x00".format(_INFO_FORMAT), _DATE_FORMAT, "--name-status", sha])
    # git terminates the header with its own NUL and a newline before the file list
    info_text, _, files_text = text.partition("\0")
    files_text = files_text.lstrip("\0\n")
    parts = info_text.split("\x1f")
    while len(parts) < 6:
        parts.append("")
    info = CommitInfo(*parts[:6])

    counts = _parse_numstat(run(repo, show + ["--format=", "--numstat", sha]))
    changes = []
    for status, path, old_path in _parse_name_status(files_text):
        ins, dele, _ = counts.get(path, (None, None, None))
        changes.append(FileChange(path, status, False, ins, dele, old_path))
    changes.sort(key=lambda c: c.path)
    return info, changes


def _in_tree(repo, ref, path):
    """Whether `path` exists in the tree of `ref`"""
    try:
        run(repo, ["cat-file", "-e", "{}:{}".format(ref, path)])
        return True
    except GitError:
        return False


def full_diff(repo, path, sha=None, untracked=False):
    """Unified diff of one file with the whole file as context

    Working tree vs HEAD (staged and unstaged merged) by default; a commit vs its
    first parent when `sha` is given; the whole file as insertions when untracked.
    """
    common = ["--no-color", "--no-ext-diff", FULL_CONTEXT]
    if untracked:
        return run(repo, ["diff", "--no-index"] + common + ["--", os.devnull, path], ok_codes=(0, 1))
    if sha:
        return run(repo, ["show", "--format=", "--first-parent", "-m"] + common + [sha, "--", path])
    return run(repo, ["diff"] + common + [base_ref(repo), "--", path])


def stage(repo, change):
    """Stage the whole file, including its deletion"""
    run(repo, ["add", "-A", "--", change.path])


def unstage(repo, change):
    """Move the file's staged changes back to the working tree"""
    if has_head(repo):
        run(repo, ["reset", "-q", "--", change.path])
    else:
        run(repo, ["rm", "-r", "-q", "--cached", "--", change.path])


def discard(repo, change):
    """Throw away the file's changes, staged and unstaged, restoring the HEAD version"""
    if change.status == "?":
        run(repo, ["clean", "-f", "--", change.path])
        return
    base = base_ref(repo)
    if change.status == "A" or not _in_tree(repo, base, change.path):
        run(repo, ["rm", "-r", "-f", "-q", "--", change.path])
        return
    run(repo, ["checkout", base, "--", change.path])
