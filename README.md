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

## Keys inside the review view

| key | action |
|---|---|
| `f` / `c` | changed files / recent commits panel |
| `s` / `u` / `d` | stage / unstage / discard the file (uncommitted mode); `s` then moves on to the next unstaged file |
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

The header links do the same with the mouse. The all-files view shows five context lines around each change by default; configure this with `all_files_context` in `vDiff.sublime-settings`.

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
