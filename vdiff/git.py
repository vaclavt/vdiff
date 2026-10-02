"""Git access via subprocess; results are plain tuples, no sublime imports (testable)"""
import os
import re
import subprocess
from typing import Dict, List, Optional, NamedTuple, Sequence, Tuple


class FileChange(NamedTuple):
    """Path, one-letter status (see STATUS_NAMES), staged flag, numstat counts (None for binary), rename source"""

    path: str
    status: str
    staged: bool
    insertions: Optional[int]
    deletions: Optional[int]
    old_path: Optional[str]


class Commit(NamedTuple):
    """One log entry, date relative; files/insertions/deletions are None without a diffstat (merges, empty commits)"""

    sha: str
    short: str
    author: str
    date: str
    subject: str
    files: Optional[int]
    insertions: Optional[int]
    deletions: Optional[int]


class CommitInfo(NamedTuple):
    """One commit: its hashes, author, date, subject and body"""

    sha: str
    short: str
    author: str
    date: str
    subject: str
    body: str


# a numstat entry: insertions, deletions (both None for a binary file) and the rename source
Numstat = Tuple[Optional[int], Optional[int], Optional[str]]

# hash of the empty tree: the diff base for a repository without commits
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# huge context turns a unified diff into the whole file with changes interleaved
FULL_CONTEXT = "-U999999"

# overridable from settings (sublime's PATH may not have a user-installed git)
GIT_BINARY: str = "git"

STATUS_NAMES: Dict[str, str] = {
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

# " 3 files changed, 10 insertions(+), 2 deletions(-)" - either count clause is dropped when zero
_SHORTSTAT = re.compile(r"\s*(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")


class GitError(Exception):
    """Git exited with an unexpected code; str() is the first stderr line, `stderr` the whole of it"""

    def __init__(self, stderr: str, code: int) -> None:
        """Keep stderr and the exit code"""
        self.stderr = stderr
        self.code = code
        first = stderr.strip().splitlines()
        super().__init__(first[0] if first else f"git exited with {code}")


def run(repo: str, args: Sequence[str], ok_codes: Tuple[int, ...] = (0,)) -> str:
    """Run git in `repo`, return stdout as text"""
    cmd = [GIT_BINARY, "-c", "core.quotepath=off", "--no-pager"] + list(args)
    proc = subprocess.run(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    if proc.returncode not in ok_codes:
        raise GitError(proc.stderr, proc.returncode)
    return proc.stdout


def repo_root(path: Optional[str]) -> Optional[str]:
    """Top-level directory of the repository containing `path`, or None"""
    if not path:
        return None
    # start from the directory itself, or the file's parent
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(directory):
        return None
    # outside a repository git fails, which reads as None
    try:
        root = run(directory, ["rev-parse", "--show-toplevel"]).strip()
    except (GitError, OSError):
        return None
    return root or None


def has_head(repo: str) -> bool:
    """False in a repository without commits"""
    try:
        run(repo, ["rev-parse", "--verify", "-q", "HEAD"])
        return True
    except GitError:
        return False


def base_ref(repo: str) -> str:
    """HEAD, or the empty tree when there is no commit yet"""
    return "HEAD" if has_head(repo) else EMPTY_TREE


def _int_or_none(value: str) -> Optional[int]:
    """A numstat count as int, None for "-" (binary)"""
    return None if value == "-" else int(value)


def _parse_numstat(text: str) -> Dict[str, Numstat]:
    """`--numstat -z` output -> {path: (insertions, deletions, old_path)}"""
    result: Dict[str, Numstat] = {}
    # each NUL field is one "insertions<TAB>deletions<TAB>path" entry
    fields = iter(text.split("\0"))
    for entry in fields:
        parts = entry.split("\t")
        if len(parts) < 3:
            continue
        ins, dele, path = parts[:3]
        old_path = None
        if path == "":
            # rename: counts, then old and new path as separate NUL fields
            old_path = next(fields, "") or None
            path = next(fields, "")
        result[path] = (_int_or_none(ins), _int_or_none(dele), old_path)
    return result


def _parse_name_status(text: str) -> List[Tuple[str, str, Optional[str]]]:
    """`--name-status -z` output -> [(status letter, path, old_path)]"""
    result: List[Tuple[str, str, Optional[str]]] = []
    # a status field, then its path; a rename or copy adds the new path after the old one
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


def _count_lines(path: str) -> Optional[int]:
    """Line count of a working-tree file, None when binary or unreadable"""
    try:
        with open(path, "rb") as f:
            # a NUL in the first 8000 bytes marks a binary file
            head = f.read(8000)
            if b"\0" in head:
                return None
            count = head.count(b"\n")
            last = head[-1:]
            # the rest in 1 MiB chunks, remembering the final byte
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return None
    # an empty file has no lines; a last line without a newline still counts
    if not last:
        return 0
    return count + (0 if last == b"\n" else 1)


def changed_files(repo: str) -> List[FileChange]:
    """Working tree changes vs HEAD: staged, unstaged and untracked, sorted by path"""
    status = run(repo, ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--no-renames"])
    entries = [e for e in status.split("\0") if e]
    # "# branch.oid (initial)" means there is no commit yet
    base = EMPTY_TREE if "# branch.oid (initial)" in entries else "HEAD"
    counts = _parse_numstat(run(repo, ["diff", "--numstat", "-z", "--no-renames", base]))

    # "1" ordinary, "u" unmerged, "?" untracked entries; anything else (branch headers) is skipped
    changes: List[FileChange] = []
    for entry in entries:
        kind = entry[0]
        if kind == "1":
            fields = entry.split(" ", 8)
            xy, path = fields[1], fields[-1]
        elif kind == "u":
            xy, path = "UU", entry.split(" ", 10)[-1]
        elif kind == "?":
            xy, path = ".?", entry[2:]
        else:
            continue
        # any index change means staged; the worktree letter wins over the index letter
        x, y = xy[0], xy[1]
        staged = x not in (".", "?")
        letter = y if y != "." else x
        # untracked files have no numstat: their lines count as insertions
        if letter == "?":
            lines = _count_lines(os.path.join(repo, path))
            ins, dele, old = lines, (None if lines is None else 0), None
        else:
            ins, dele, old = counts.get(path, (0, 0, None))
        changes.append(FileChange(path, letter, staged, ins, dele, old))
    changes.sort(key=lambda c: c.path)
    return changes


def recent_commits(repo: str, count: int) -> List[Commit]:
    """Last `count` commits, newest first; empty list without commits"""
    if not has_head(repo):
        return []
    # --shortstat adds per-commit totals to the same call
    text = run(repo, ["log", "-n", str(count), "--format=%H%x1f%h%x1f%an%x1f%ar%x1f%s", "--shortstat"])
    # a five-field line starts a commit; the shortstat line after it fills in its totals
    commits: List[Commit] = []
    for line in text.split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 5:
            sha, short, author, date, subject = parts
            commits.append(Commit(sha, short, author, date, subject, files=None, insertions=None, deletions=None))
            continue
        if commits and commits[-1].files is None:
            stat = _SHORTSTAT.match(line)
            if stat:
                files, ins, dele = (int(g or 0) for g in stat.groups())
                commits[-1] = commits[-1]._replace(files=files, insertions=ins, deletions=dele)
    return commits


def commit_with_files(repo: str, sha: str) -> Tuple[CommitInfo, List[FileChange]]:
    """(CommitInfo, [FileChange]) of one commit vs its first parent, files sorted by path; two git calls"""
    show = ["show", "--first-parent", "-m", "-z"]
    text = run(repo, show + [f"--format={_INFO_FORMAT}%x00", _DATE_FORMAT, "--name-status", sha])
    # our %x00 ends the header; strip the separator git adds before the file list
    info_text, _, files_text = text.partition("\0")
    files_text = files_text.lstrip("\0\n")
    # pad missing trailing fields so CommitInfo always gets six
    parts = info_text.split("\x1f")
    while len(parts) < 6:
        parts.append("")
    info = CommitInfo(*parts[:6])

    # second call: per-file counts, matched to the name-status list by path
    counts = _parse_numstat(run(repo, show + ["--format=", "--numstat", sha]))
    changes: List[FileChange] = []
    for status, path, old_path in _parse_name_status(files_text):
        ins, dele, _ = counts.get(path, (None, None, None))
        changes.append(FileChange(path, status, False, ins, dele, old_path))
    changes.sort(key=lambda c: c.path)
    return info, changes


def _in_tree(repo: str, ref: str, path: str) -> bool:
    """Whether `path` exists in the tree of `ref`"""
    try:
        run(repo, ["cat-file", "-e", f"{ref}:{path}"])
        return True
    except GitError:
        return False


def full_diff(repo: str, path: str, sha: Optional[str] = None, untracked: bool = False) -> str:
    """Unified diff of one file with the whole file as context

    Working tree vs HEAD by default (staged and unstaged merged), a commit vs its first parent with
    `sha`, the whole file as insertions when untracked
    """
    common = ["--no-color", "--no-ext-diff", FULL_CONTEXT]
    # untracked: against /dev/null, where exit code 1 only means the files differ
    if untracked:
        return run(repo, ["diff", "--no-index"] + common + ["--", os.devnull, path], ok_codes=(0, 1))
    if sha:
        return run(repo, ["show", "--format=", "--first-parent", "-m"] + common + [sha, "--", path])
    return run(repo, ["diff"] + common + [base_ref(repo), "--", path])


def stage(repo: str, change: FileChange) -> None:
    """Stage the whole file, including its deletion"""
    run(repo, ["add", "-A", "--", change.path])


def commit(repo: str, message: str) -> None:
    """Commit what is staged; `message` goes as one argv element, unquoted"""
    run(repo, ["commit", "-m", message])


def head_message(repo: str) -> str:
    """HEAD's full message, surrounding newlines stripped"""
    return run(repo, ["log", "-1", "--format=%B"]).strip("\n")


def amend(repo: str, message: str, allow_empty: bool = False) -> None:
    """Rewrite HEAD with `message`, folding in whatever is staged

    `allow_empty` lets a reword through when HEAD itself is empty (no diff vs its parent)
    """
    run(repo, ["commit", "--amend"] + (["--allow-empty"] if allow_empty else []) + ["-m", message])


def unstage(repo: str, change: FileChange) -> None:
    """Move the file's staged changes back to the working tree"""
    if has_head(repo):
        run(repo, ["reset", "-q", "--", change.path])
    else:
        run(repo, ["rm", "-r", "-q", "--cached", "--", change.path])


def discard(repo: str, change: FileChange) -> None:
    """Throw away the file's staged and unstaged changes: restore HEAD's version, or delete a new file"""
    if change.status == "?":
        run(repo, ["clean", "-f", "--", change.path])
        return
    base = base_ref(repo)
    if change.status == "A" or not _in_tree(repo, base, change.path):
        run(repo, ["rm", "-r", "-f", "-q", "--", change.path])
        return
    run(repo, ["checkout", base, "--", change.path])
