"""Git access via subprocess; results are plain tuples, no sublime imports (testable)"""
import functools
import os
import re
import subprocess
from typing import Dict, List, Optional, NamedTuple, Sequence, Tuple


class FileChange(NamedTuple):
    """Path, one-letter status (see STATUS_NAMES), staged flag, numstat counts (None for binary), rename source

    `staged` means something of the file is in the index, `unstaged` that the working tree differs from the
    index (an untracked file is unstaged); a partially staged file carries both
    """

    path: str
    status: str
    staged: bool
    insertions: Optional[int]
    deletions: Optional[int]
    old_path: Optional[str]
    unstaged: bool = False

    @property
    def has_unstaged(self) -> bool:
        """Whether something of the file is left to stage: a file with nothing staged is, whatever its flag says"""
        return self.unstaged or not self.staged


class WorkTree(NamedTuple):
    """The working tree's changes summed up: how many files are staged / unstaged, and the diffstat of them all"""

    staged: int
    unstaged: int
    files: int
    insertions: int
    deletions: int


def worktree_stats(changes: Sequence[FileChange]) -> WorkTree:
    """Sum the working tree listing: a partially staged file counts on both sides; binary files carry None counts and add nothing"""
    return WorkTree(sum(1 for c in changes if c.staged), sum(1 for c in changes if c.has_unstaged), len(changes),
                    sum(c.insertions or 0 for c in changes), sum(c.deletions or 0 for c in changes))


class Commit(NamedTuple):
    """One log entry; files/insertions/deletions are None without a diffstat (merges, empty commits)

    `date` is relative from recent_commits and dd.mm.yyyy from ref_log, which is also what fills `tags`
    """

    sha: str
    short: str
    author: str
    date: str
    subject: str
    files: Optional[int]
    insertions: Optional[int]
    deletions: Optional[int]
    tags: Tuple[str, ...] = ()


class Ref(NamedTuple):
    """One local or remote branch

    `name` is what git calls it ("main", "origin/main"), `branch` the part after the remote; ahead/behind
    count against the upstream, `gone` says the upstream was deleted. `upstream_remote` is the remote the
    upstream lives on, None without an upstream or when it is a local branch
    """

    name: str
    branch: str
    remote: Optional[str]
    sha: str
    upstream: Optional[str]
    ahead: int
    behind: int
    gone: bool
    current: bool
    upstream_remote: Optional[str] = None

    @property
    def upstream_branch(self) -> Optional[str]:
        """The upstream's name on its remote ("bar" for origin/bar), None without a remote upstream"""
        if self.upstream and self.upstream_remote:
            return self.upstream[len(self.upstream_remote) + 1:]
        return None


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

# environment of the network commands: no terminal to answer a credential prompt or edit a merge
# message in, so git fails fast instead of hanging the worker thread
NO_PROMPT: Dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
# git's messages in English whatever the user's locale: delete_branch's refusal is told apart by its text
PLAIN_MESSAGES: Dict[str, str] = {"LC_ALL": "C", "LANGUAGE": "C"}

# the ref namespaces listed by branches(); remotes carry a symbolic HEAD that is skipped
_HEADS = "refs/heads/"
_REMOTES = "refs/remotes/"
# for-each-ref knows no %x1f escape, so the separator goes in literally
_REF_FORMAT = "\x1f".join(("%(refname)", "%(objectname:short)", "%(upstream:short)", "%(upstream:track,nobracket)", "%(HEAD)",
                           "%(symref)", "%(upstream:remotename)"))
# sha, short, author, date (as --date says), subject and the decorations the tags are read from
_LOG_FORMAT = "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%D"
_RELATIVE_DATE = "--date=relative"
_DAY_DATE = "--date=format:%d.%m.%Y"

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


def run(repo: str, args: Sequence[str], ok_codes: Tuple[int, ...] = (0,), env: Optional[Dict[str, str]] = None) -> str:
    """Run git in `repo`, return stdout as text; `env` adds to or overrides the inherited environment

    stdin is closed: nothing git or its helpers could ask would get an answer from the worker thread
    """
    cmd = [GIT_BINARY, "-c", "core.quotepath=off", "--no-pager"] + list(args)
    environment = dict(os.environ, **PLAIN_MESSAGES, **(env or {}))
    proc = subprocess.run(cmd, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", env=environment)
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


@functools.lru_cache(maxsize=None)
def _git_paths(repo: str, names: Tuple[str, ...]) -> Tuple[str, ...]:
    """Absolute paths of files of the repository's git directory, () outside a repository

    A worktree or submodule keeps them outside <repo>/.git, which there is a file, not a directory
    """
    try:
        # one --git-path per name, answered one per line
        text = run(repo, ["rev-parse"] + [arg for name in names for arg in ("--git-path", name)])
    except (GitError, OSError):
        return ()
    return tuple(os.path.join(repo, line) for line in text.split("\n") if line)


def index_path(repo: str) -> Optional[str]:
    """Absolute path of the repository's index file, None outside a repository"""
    paths = _git_paths(repo, ("index",))
    return paths[0] if paths else None


# what any checkout, commit, fetch, push or branch change touches: refs are written through a lock-file
# rename (bumping the directory's mtime) and every HEAD move appends to logs/HEAD
REF_PATH_NAMES: Tuple[str, ...] = ("HEAD", "logs/HEAD", "packed-refs", "FETCH_HEAD", "refs/heads", "refs/remotes")
REF_DIRS: Tuple[str, ...] = ("refs/heads", "refs/remotes")


# {repo: (the ref directories walked, roots first, and their mtimes then)}: a directory's mtime moves when an
# entry is added to or removed from it, so the walk is only redone when one of them moved
_nested_refs: Dict[str, Tuple[Tuple[str, ...], Tuple[Optional[float], ...]]] = {}


def mtime(path: str) -> Optional[float]:
    """A file's or directory's mtime, None when it is gone"""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def ref_paths(repo: str) -> Tuple[str, ...]:
    """Absolute paths whose mtimes change when the branches do; () outside a repository

    REF_PATH_NAMES, then every directory below the ref directories: a ref's rename bumps only the directory
    it is in, and remote-tracking refs and slashed branches live one or more levels down
    """
    paths = _git_paths(repo, REF_PATH_NAMES)
    if not paths:
        return paths
    roots = tuple(paths[REF_PATH_NAMES.index(name)] for name in REF_DIRS)
    cached = _nested_refs.get(repo)
    if cached is not None:
        walked, mtimes = cached
        if tuple(mtime(d) for d in walked) == mtimes:
            return paths + walked[len(roots):]
    found: List[str] = []
    for root in roots:
        for directory, dirs, _ in os.walk(root):
            dirs.sort()
            if directory != root:
                found.append(directory)
    walked = roots + tuple(found)
    _nested_refs[repo] = (walked, tuple(mtime(d) for d in walked))
    return paths + tuple(found)


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
        # any index change means staged, any worktree one unstaged; the worktree letter wins over the index letter
        x, y = xy[0], xy[1]
        staged, unstaged = x not in (".", "?"), y != "."
        letter = y if y != "." else x
        # untracked files have no numstat: their lines count as insertions
        if letter == "?":
            lines = _count_lines(os.path.join(repo, path))
            ins, dele, old = lines, (None if lines is None else 0), None
        else:
            ins, dele, old = counts.get(path, (0, 0, None))
        changes.append(FileChange(path, letter, staged, ins, dele, old, unstaged))
    changes.sort(key=lambda c: c.path)
    return changes


def _log(repo: str, args: Sequence[str]) -> List[Commit]:
    """`log <args>` in _LOG_FORMAT with --shortstat -> commits, newest first

    A six-field line starts a commit (its tags come from the %D decoration list); the shortstat line
    after it fills in its totals
    """
    commits: List[Commit] = []
    for line in run(repo, ["log", _LOG_FORMAT, "--shortstat"] + list(args)).split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 6:
            sha, short, author, date, subject, decorations = parts
            tags = tuple(d[5:] for d in decorations.split(", ") if d.startswith("tag: "))
            commits.append(Commit(sha, short, author, date, subject, None, None, None, tags))
            continue
        if commits and commits[-1].files is None:
            stat = _SHORTSTAT.match(line)
            if stat:
                files, ins, dele = (int(g or 0) for g in stat.groups())
                commits[-1] = commits[-1]._replace(files=files, insertions=ins, deletions=dele)
    return commits


def recent_commits(repo: str, count: int) -> List[Commit]:
    """Last `count` commits, newest first, dates relative; empty list without commits"""
    if not has_head(repo):
        return []
    return _log(repo, ["-n", str(count), _RELATIVE_DATE])


def ref_log(repo: str, ref: str, count: int) -> List[Commit]:
    """Last `count` commits reachable from `ref`, newest first, with dd.mm.yyyy dates"""
    return _log(repo, [ref, "-n", str(count), _DAY_DATE, "--"])


# --- branches ---------------------------------------------------------------


def remotes(repo: str) -> List[str]:
    """Configured remote names, fetched or not"""
    return [line for line in run(repo, ["remote"]).split("\n") if line]


def head_sha(repo: str) -> str:
    """HEAD's short hash, "" before the first commit; which branch it is on, if any, branches() marks"""
    # -q --verify: exit 1 and no output without a commit
    return run(repo, ["rev-parse", "-q", "--verify", "--short", "HEAD"], ok_codes=(0, 1)).strip()


def _track(text: str) -> Tuple[int, int, bool]:
    """`%(upstream:track,nobracket)` -> (ahead, behind, gone)"""
    ahead = re.search(r"ahead (\d+)", text)
    behind = re.search(r"behind (\d+)", text)
    return int(ahead.group(1)) if ahead else 0, int(behind.group(1)) if behind else 0, text.strip() == "gone"


def branches(repo: str) -> List[Ref]:
    """Local branches, then remote ones, in git's ref order; a remote's symbolic HEAD is left out"""
    refs: List[Ref] = []
    for line in run(repo, ["for-each-ref", f"--format={_REF_FORMAT}", _HEADS, _REMOTES]).split("\n"):
        parts = line.split("\x1f")
        if len(parts) != 7:
            continue
        refname, sha, upstream, track, marker, symref, remotename = parts
        if symref:
            continue
        ahead, behind, gone = _track(track)
        if refname.startswith(_HEADS):
            name = refname[len(_HEADS):]
            # "." is git's name for the local repository: an upstream that is a local branch
            upstream_remote = remotename if upstream and remotename != "." else None
            refs.append(Ref(name, name, None, sha, upstream or None, ahead, behind, gone, marker == "*", upstream_remote))
        elif refname.startswith(_REMOTES):
            name = refname[len(_REMOTES):]
            # the remote is the first path segment; a remote named with a slash is not supported
            remote, _, branch = name.partition("/")
            refs.append(Ref(name, branch, remote, sha, None, 0, 0, False, False))
    return refs


def checkout(repo: str, ref: Ref, local_names: Sequence[str] = ()) -> None:
    """Check out a local branch, or a remote one as a same-named local branch tracking it

    An existing local branch of that name is checked out as it is. The remote is named explicitly:
    git's own guess fails when two remotes carry the branch
    """
    if ref.remote is None or ref.branch in local_names:
        run(repo, ["checkout", "-q", ref.branch, "--"])
    else:
        run(repo, ["checkout", "-q", "--track", f"{ref.remote}/{ref.branch}", "--"])


def checkout_detached(repo: str, name: str) -> None:
    """Check out `name` (a remote branch, say) with HEAD detached"""
    run(repo, ["checkout", "-q", "--detach", name, "--"])


def pull(repo: str) -> None:
    """Pull the current branch from its upstream, as the user's pull.* configuration says"""
    run(repo, ["pull", "-q"], env=NO_PROMPT)


def fetch_into(repo: str, remote: str, remote_branch: str, branch: str) -> None:
    """Fast-forward the local `branch` to `remote`'s `remote_branch` without checking it out

    git refuses a non-fast-forward update and a branch that is checked out, which the error says
    """
    run(repo, ["fetch", "-q", remote, f"{remote_branch}:{branch}"], env=NO_PROMPT)


def push(repo: str, remote: str, branch: str, set_upstream: bool = False, remote_branch: Optional[str] = None) -> None:
    """Push `branch` to `remote`, as `remote_branch` there when that differs, recording it as the upstream when asked"""
    refspec = f"{branch}:{remote_branch}" if remote_branch else branch
    run(repo, ["push", "-q"] + (["-u"] if set_upstream else []) + [remote, refspec], env=NO_PROMPT)


def fetch_all(repo: str) -> None:
    """Fetch every remote, dropping remote branches that no longer exist"""
    run(repo, ["fetch", "-q", "--all", "--prune"], env=NO_PROMPT)


def delete_branch(repo: str, branch: str, force: bool = False) -> None:
    """Delete a local branch; without `force` git refuses one that is not fully merged ("not fully merged" in stderr)"""
    run(repo, ["branch", "-D" if force else "-d", branch])


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


def stage_all(repo: str) -> None:
    """Stage every change in the working tree, including deletions and untracked files"""
    run(repo, ["add", "-A"])


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


def _unstage_path(repo: str, path: str) -> None:
    """Move the staged changes under `path` back to the working tree; before the first commit, drop them from the index"""
    if has_head(repo):
        run(repo, ["reset", "-q", "--", path])
    else:
        run(repo, ["rm", "-r", "-q", "--cached", "--", path])


def unstage(repo: str, change: FileChange) -> None:
    """Move the file's staged changes back to the working tree"""
    _unstage_path(repo, change.path)


def unstage_all(repo: str) -> None:
    """Move every staged change back to the working tree"""
    _unstage_path(repo, ".")


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
