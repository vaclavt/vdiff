# vDiff

Review what a coding agent changed without leaving Sublime Text: the whole file, deletions in red,
insertions in green, in the file's own syntax highlighting. Stage, unstage or discard the file, or jump
to the real file at the caret's line.

Install: symlink this directory into `Packages` as `vDiff` (Python 3.8 plugin host, no dependencies).
On first use vDiff writes the scopes it paints changes with into `Packages/User/<your scheme>.sublime-color-scheme`;
Sublime loads a scheme file it has not seen before only at startup, so restart it once to get the colours.

Command Palette:

- **vDiff: Changed Files** – uncommitted changes (staged, unstaged, untracked). Arrowing through
  the list previews each file; Enter keeps it, Esc goes back to what was shown.
- **vDiff: Recent Commits** – last N commits, then the files of the chosen commit. A key binding to
  `vdiff_commits` with `{"sha": "HEAD"}` skips the list and reviews the last commit directly.

Keys inside the review view:

| key | action |
|---|---|
| `f` / `c` | changed files / recent commits panel |
| `s` / `u` / `d` | stage / unstage / discard the file (uncommitted mode); `s` then moves on to the next unstaged file |
| `C` | commit the staged files, typing the message in an input panel |
| `m` | amend the last commit: edit its subject, keep its body, fold in anything staged |
| `o` or Enter | open the real file at this line |
| `]` / `[` | next / previous change (wraps at the ends) |
| `n` / `p` | next / previous file of the current list (wraps at the ends) |
| `j` / `k` | move down / up one line (vim keys) |
| `r` | refresh |
| `?` | show this key list in a popup |
| Esc | close the view |

The header links do the same with the mouse. Settings: `vDiff.sublime-settings`.
