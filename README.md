# vDiff

Review what a coding agent changed without leaving Sublime Text: the whole file, deletions in red,
insertions in green, in the file's own syntax highlighting. Stage, unstage or discard the file, or jump
to the real file at the caret's line.

Install: symlink this directory into `Packages` as `vDiff` (Python 3.8 plugin host, no dependencies).

Command Palette:

- **vDiff: Changed Files** – uncommitted changes (staged, unstaged, untracked). Arrowing through
  the list previews each file; Enter keeps it, Esc goes back to what was shown.
- **vDiff: Recent Commits** – last N commits, then the files of the chosen commit. A key binding to
  `vdiff_commits` with `{"sha": "HEAD"}` skips the list and reviews the last commit directly.

Keys inside the review view:

| key | action |
|---|---|
| `f` / `c` | changed files / recent commits panel |
| `s` / `u` / `d` | stage / unstage / discard the file (uncommitted mode) |
| `o` or Enter | open the real file at this line |
| `]` / `[` | next / previous change |
| `n` / `p` | next / previous file of the current list |
| `j` / `k` | move down / up one line (vim keys) |
| `r` | refresh |
| `?` | show this key list in a popup |
| Esc | close the view |

The header links do the same with the mouse. Settings: `vDiff.sublime-settings`.
