"""Per-view state of review views, keyed by view id; the essentials are mirrored into view settings for restore"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

if TYPE_CHECKING:
    # annotation-only, so this module keeps importing neither sublime nor the rest of the package
    import sublime

    from .branches import Paint
    from .git import Commit, CommitInfo, FileChange, Ref
    from .parse import Block, Line

# view setting marking a review view (keymap context "setting.vdiff_view")
VIEW_FLAG = "vdiff_view"
# view setting naming what the view shows, "diff" or "branches", so the keymap can bind a key per mode
MODE_SETTING = "vdiff_mode"
MODE_DIFF = "diff"
MODE_BRANCHES = "branches"
# view setting holding what the view shows, so a restored tab can re-render
VIEW_DATA = "vdiff"


class Kept(NamedTuple):
    """What a quick panel goes back to on Esc"""

    sha: Optional[str]
    commit: Optional["CommitInfo"]
    change: Optional["FileChange"]
    files: List["FileChange"]
    all_mode: bool
    mode: str = MODE_DIFF


class BranchRow(NamedTuple):
    """What one row of the branches view holds: the branch of its left cell, the index of its right-column
    item (0 the working tree, i + 1 the i-th commit, -1 none) and the coloured spans of its right cell
    as (start, end, "del" | "ins") offsets from the row's start"""

    ref: Optional["Ref"]
    commit: int
    spans: Tuple[Tuple[int, int, str], ...] = ()


def find_change(files: Sequence["FileChange"], path: Optional[str]) -> int:
    """Index of the FileChange with `path`, or -1"""
    for i, change in enumerate(files):
        if change.path == path:
            return i
    return -1


@dataclass(eq=False)
class ViewState:
    """Everything a review view needs to re-render itself and act on its file"""

    repo: str
    sha: Optional[str] = None  # None in working-tree mode, the reviewed commit's hash otherwise
    commit: Optional["CommitInfo"] = None  # set when sha is
    change: Optional["FileChange"] = None  # file currently shown
    requested: Optional["FileChange"] = None  # file asked for, maybe still loading; navigation counts from here
    files: List["FileChange"] = field(default_factory=list)  # what the user is browsing (next/prev file)
    lines: List["Line"] = field(default_factory=list)  # currently rendered
    blocks: List["Block"] = field(default_factory=list)  # parse.change_blocks(lines)
    binary: bool = False
    token: int = 0  # bumped per show request; a stale async result is dropped
    rendered_token: int = -1  # token of the request that produced the current render
    highlight: int = 0  # bumped per quick-panel highlight, to debounce previews
    fingerprint: Optional[Tuple[Any, ...]] = None  # what the last render saw; see render.fingerprint
    drawn: Optional[Tuple[Any, ...]] = None  # what the last render drew, as describe() put it
    all_mode: bool = False
    all_diffs: Dict[str, Tuple[List["Line"], List["Block"], bool]] = field(default_factory=dict)
    all_colours: Dict[str, Dict[Tuple[Optional[int], Optional[int]], List[Tuple[int, int, str]]]] = field(default_factory=dict)
    collapsed: set = field(default_factory=set)
    all_rows: List[Tuple[Optional["FileChange"], Optional["Line"], bool]] = field(default_factory=list)
    all_change_rows: List[int] = field(default_factory=list)
    all_change_targets: List[Tuple[str, int]] = field(default_factory=list)
    all_change_row_targets: List[Tuple[str, int]] = field(default_factory=list)
    # the branches view: what it lists, which branch is selected and what each row holds
    mode: str = MODE_DIFF
    branch_ref: Optional[str] = None  # name of the selected branch, e.g. "main" or "origin/main"
    branch_head_sha: str = ""  # HEAD's short hash, "" before the first commit
    branch_refs: List["Ref"] = field(default_factory=list)
    branch_remotes: List[str] = field(default_factory=list)
    branch_commits: List["Commit"] = field(default_factory=list)
    branch_changes: List["FileChange"] = field(default_factory=list)  # the working tree, listed with the branches
    branch_rows: List[BranchRow] = field(default_factory=list)
    branch_width: int = 0  # the left column's width in the last render
    branch_paint: Optional["Paint"] = None  # the last render's row geometry and scopes, see branches.Paint
    header: Optional[Tuple[str, int]] = None  # the header phantom last drawn: its html and the buffer's change_count then

    @property
    def in_commit(self) -> bool:
        """Whether a commit is being reviewed rather than the working tree"""
        return self.sha is not None

    def describe(self) -> Tuple[Any, ...]:
        """What the state says is shown: the branches view, or a diff's commit, mode and file"""
        if self.in_branches:
            return (self.mode,)
        return self.mode, self.sha, self.all_mode, self.change.path if self.change else None

    @property
    def in_branches(self) -> bool:
        """Whether the branches view is shown rather than a diff"""
        return self.mode == MODE_BRANCHES

    def selected_ref(self) -> Optional["Ref"]:
        """The selected branch, or None when it is not listed (any more)"""
        return next((r for r in self.branch_refs if r.name == self.branch_ref), None)

    def current_branch(self) -> Optional["Ref"]:
        """The checked-out branch, None when detached or before the first commit"""
        return next((r for r in self.branch_refs if r.current), None)

    def index(self) -> int:
        """Position in `files` of the file being shown or loaded, or -1

        `requested` leads: a diff arrives asynchronously, so counting from `change` would make n / p
        step from what was on screen before the pending file landed
        """
        change = self.requested or self.change
        return find_change(self.files, change.path) if change else -1

    def kept(self) -> Kept:
        """Snapshot of what to go back to on Esc"""
        return Kept(self.sha, self.commit, self.change, list(self.files), self.all_mode, self.mode)

    def enter(self, sha: Optional[str] = None, commit: Optional["CommitInfo"] = None) -> Kept:
        """Switch to browsing a commit (or the working tree); returns what to restore on Esc"""
        kept = self.kept()
        if sha != self.sha:
            self.change = self.requested = None
        self.sha, self.commit = sha, commit
        return kept

    def restore(self, kept: Kept) -> None:
        """Go back to a snapshot; its file becomes the requested one"""
        self.sha, self.commit, self.change, self.files, self.all_mode, self.mode = kept
        self.requested = kept.change

    def set_all_diffs(self, diffs: Dict[str, Tuple[List["Line"], List["Block"], bool]]) -> None:
        """Replace the all-files diffs; a file's cached colours survive only if its diff is unchanged"""
        self.all_colours = {
            path: colours
            for path, colours in self.all_colours.items()
            if path in diffs and path in self.all_diffs and diffs[path][0] == self.all_diffs[path][0]
        }
        self.all_diffs = diffs

    def persist(self, view: "sublime.View") -> None:
        """Mirror the essentials into view settings (survives a restart)"""
        data: Dict[str, Any] = {"repo": self.repo, "sha": self.sha}
        if self.in_branches:
            data["mode"] = MODE_BRANCHES
            data["ref"] = self.branch_ref
        if self.all_mode:
            data["all_mode"] = True
        if self.change:
            data["path"] = self.change.path
            data["status"] = self.change.status
            data["staged"] = self.change.staged
        view.settings().set(VIEW_DATA, data)


STATES: Dict[int, "ViewState"] = {}


def get(view: "sublime.View") -> Optional[ViewState]:
    """State of a review view, or None when not tracked"""
    return STATES.get(view.id())


def tracked() -> List[Tuple[int, ViewState]]:
    """Snapshot of (view id, state) for every tracked review view"""
    return list(STATES.items())


def put(view: "sublime.View", st: ViewState) -> ViewState:
    """Track `st` as the view's state and return it"""
    STATES[view.id()] = st
    return st


def drop(view: "sublime.View") -> None:
    """Forget the view's state, if any"""
    STATES.pop(view.id(), None)


def is_review_view(view: Optional["sublime.View"]) -> bool:
    """Whether the view carries the review-view flag"""
    return bool(view and view.settings().get(VIEW_FLAG))
