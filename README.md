# vDiff

Review what a coding agent changed without leaving Sublime Text: the whole file, deletions in red,
insertions in green, in the file's own syntax highlighting. Stage, unstage or discard the file, or jump
to the real file at the caret's line.

Needs Sublime Text build 4213 or later, the build that replaced the Python 3.8 plugin host with
Python 3.14. No dependencies.

## Install with Package Control

vDiff is distributed through the shared channel at
[vaclavt/sublime-chanel](https://github.com/vaclavt/sublime-chanel), not the default one. Open
**Preferences → Package Settings → Package Control → Settings** and put the channel URL in the
`channels` array of the right-hand (User) pane:

```json
{
    "channels": [
        "https://packagecontrol.io/channel_v3.json",
        "https://raw.githubusercontent.com/vaclavt/sublime-chanel/main/channel.json"
    ]
}
```

Keep `https://packagecontrol.io/channel_v3.json` in the list — dropping it hides every default
package. Then run **Package Control: Install Package** from the Command Palette and pick **vDiff**.
Upgrades arrive through **Package Control: Upgrade Package** like any other package.

## Install from source

Clone or symlink the repository into the Sublime Text `Packages` directory as `vDiff`:

```bash
git clone https://github.com/vaclavt/vdiff.git \
  "$HOME/Library/Application Support/Sublime Text/Packages/vDiff"
```

Use one method or the other, not both: an unpacked `Packages/vDiff` directory overrides the
`vDiff.sublime-package` that Package Control installs.

## Command Palette

- **vDiff: Changed Files** – uncommitted changes (staged, unstaged, untracked). Arrowing through
  the list previews each file; Enter keeps it, Esc goes back to what was shown.
- **vDiff: Recent Commits** – last N commits, then the files of the chosen commit. A key binding to
  `vdiff_commits` with `{"sha": "HEAD"}` skips the list and reviews the last commit directly.
- **vDiff: Branches** – local branches and remotes on the left, the commits of the selected branch on
  the right; checkout, pull, push, fetch and delete from the keyboard.

## Keys inside the review view

| key | action |
|---|---|
| `f` / `c` | changed files / recent commits panel |
| `b` | the branches view |
| `s` / `u` / `d` | stage / unstage / discard the file (uncommitted mode); `s` then moves on to the next unstaged file |
| `S` / `U` | stage all unstaged files / unstage all staged files |
| `C` | commit the staged files, typing the message in an input panel |
| `m` | amend the last commit: edit its subject, keep its body, fold in anything staged |
| `o` or Enter | open the real file at this line |
| `]` / `[` | next / previous change (wraps at the ends) |
| `n` / `p` | next / previous file of the current list (wraps at the ends) |
| `v` | switch between the file-by-file and all-files views |
| `left` / `right` | collapse / expand the file under the caret in the all-files view |
| `z` | collapse all files in the all-files view, or expand them all when every one is collapsed |
| `j` / `k` | move down / up one line (vim keys) |
| `r` | refresh |
| `?` | show this key list in a popup |
| Esc | close the view |

The header links do the same with the mouse.

## Keys inside the branches view

`b` turns the review view into the branches view: local branches and remotes in the left column, the
last N commits (`commits_count`) of the selected branch in the right one, each with its author, date,
tags and diffstat. A local branch shows how it stands against its upstream on the right of its cell:
`↑2` commits ahead, `↓1` behind, `gone` when the upstream was deleted, nothing when level or untracked.
The right column starts with the working tree: how many files are staged and unstaged (a partially
staged file counts in both) and their diffstat, or `No Changes`. The selected branch is the tinted cell;
the caret marks a commit, or that entry.

| key | action |
|---|---|
| `J` / `K` | select the next / previous branch, local ones first, then each remote's |
| `j` / `k` | move to the next / previous commit |
| Enter | checkout the selected branch; a remote branch becomes a local branch of the same name tracking it (an existing one is reused) |
| `shift+enter` | checkout a remote branch detached |
| `L` | pull: `git pull` on the checked-out branch, a fast-forward from the upstream for any other local branch |
| `P` | push to the upstream, or with `-u` to the branch's remote (origin by default) when it has none |
| `F` | fetch all remotes and prune the branches they dropped |
| `d` | delete the selected local branch, asking first (and again when it is not fully merged) |
| `o` or `v` | review the commit under the caret, or the changed files when it is on the working-tree entry |
| `f` / `c` | back to the changed files / recent commits; Esc in that panel returns here |
| `r` / `b` | reload |
| `?` | show this key list in a popup |
| Esc | close the view |

Pull, push and fetch run without a terminal, so credentials must come from a credential helper or an
ssh agent: git fails at once instead of waiting for a password. The selected branch takes its tint from
`file_header_background`; the -/+ counts and tags use `file_header_deleted_foreground` and
`file_header_inserted_foreground`, after the restart that loads their colour-scheme rules. The view reloads when it comes back into focus after a checkout, commit,
fetch or branch change was made elsewhere. A remote whose name contains a slash is listed wrongly. The all-files view shows five context lines around each change by default; configure this with `all_files_context` in `vDiff.sublime-settings`. The same file sets the background of deleted and inserted lines (`deleted_line_background`, `inserted_line_background`) and the colours of the all-files line-number column (`line_numbers_foreground`, `line_numbers_background`); left empty, they follow the colour scheme. Each all-files file heading shows the file's status letter (`s` when anything is staged, else `m`, `d`, `a`, `?` ...) before its -/+ counts, and takes its colours from `file_header_background`, `file_header_status_foreground`, `file_header_deleted_foreground` and `file_header_inserted_foreground`.

## The channel

`repository.json` in this repository is a Package Control **repository** file: its top-level key
is `packages`, and it describes vDiff itself — where the code lives and which releases exist.
`"tags": true` means Package Control reads the repository's git tags, so publishing a release is
just `git tag v1.0.1 && git push --tags`. A package whose tags are all unpushed resolves to zero
releases, and Package Control drops it from the list without an error — if vDiff is missing from
**Install Package**, check the tags reached GitHub.

The channel itself — the `channels`-array file listing `repository.json` for vDiff and vaclavt's
other packages — lives in [vaclavt/sublime-chanel](https://github.com/vaclavt/sublime-chanel), not
here. Add a package there instead of in this repository.
