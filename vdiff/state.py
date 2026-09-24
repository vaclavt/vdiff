""" Per-view state of review views, keyed by view id; the essentials are mirrored into view settings for restore """
from collections import namedtuple
from dataclasses import dataclass, field

# view setting marking a review view (keymap context "setting.vdiff_view")
VIEW_FLAG = "vdiff_view"
# view setting holding what the view shows, so a restored tab can re-render
VIEW_DATA = "vdiff"

# what a quick panel goes back to on Esc
Kept = namedtuple("Kept", "sha commit change files")


def find_change(files, path):
    """Index of the FileChange with `path`, or -1"""
    for i, change in enumerate(files):
        if change.path == path:
            return i
    return -1


@dataclass(eq=False)
class ViewState:
    """Everything a review view needs to re-render itself and act on its file

    `sha` is None in working-tree mode and the reviewed commit's hash otherwise.
    """

    repo: str
    sha: object = None  # None in working-tree mode, the reviewed commit's hash otherwise
    commit: object = None  # git.CommitInfo when sha is set
    change: object = None  # git.FileChange currently shown
    files: list = field(default_factory=list)  # git.FileChange list the user is browsing (next/prev file)
    lines: list = field(default_factory=list)  # parse.Line list currently rendered
    blocks: list = field(default_factory=list)  # parse.change_blocks(lines)
    binary: bool = False
    token: int = 0  # bumped per show request; a stale async result is dropped
    rendered_token: int = -1  # token of the request that produced the current render
    highlight: int = 0  # bumped per quick-panel highlight, to debounce previews
    fingerprint: object = None  # (file mtime, index mtime) at render time, working-tree mode

    @property
    def in_commit(self):
        return self.sha is not None

    def index(self):
        """Position of the shown file in `files`, or -1"""
        return find_change(self.files, self.change.path) if self.change else -1

    def kept(self):
        return Kept(self.sha, self.commit, self.change, list(self.files))

    def enter(self, sha=None, commit=None):
        """Switch to browsing a commit (or the working tree); returns what to restore on Esc"""
        kept = self.kept()
        if sha != self.sha:
            self.change = None
        self.sha, self.commit = sha, commit
        return kept

    def restore(self, kept):
        self.sha, self.commit, self.change, self.files = kept

    def persist(self, view):
        """Mirror the essentials into view settings (survives a restart)"""
        data = {"repo": self.repo, "sha": self.sha}
        if self.change:
            data["path"] = self.change.path
            data["status"] = self.change.status
            data["staged"] = self.change.staged
        view.settings().set(VIEW_DATA, data)


STATES = {}


def get(view):
    return STATES.get(view.id())


def tracked():
    """Snapshot of (view id, state) for every tracked review view"""
    return list(STATES.items())


def put(view, st):
    STATES[view.id()] = st
    return st


def drop(view):
    STATES.pop(view.id(), None)


def is_review_view(view):
    return bool(view and view.settings().get(VIEW_FLAG))
