# vDiff

Review what a coding agent changed without leaving Sublime Text: the whole file, deletions in red,
insertions in green, in the file's own syntax highlighting. Stage, unstage or discard the file, or jump
to the real file at the caret's line.

Needs Sublime Text build 4213 or later, the build that replaced the Python 3.8 plugin host with
Python 3.14. No dependencies.

## Install with Package Control

vDiff is not on the default channel, so add this one first. Open
**Preferences → Package Settings → Package Control → Settings** and put the channel URL in the
`channels` array of the right-hand (User) pane:

```json
{
    "channels": [
        "https://packagecontrol.io/channel_v3.json",
        "https://raw.githubusercontent.com/vaclavt/vdiff/main/channel.json"
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
| `j` / `k` | move down / up one line (vim keys) |
| `r` | refresh |
| `?` | show this key list in a popup |
| Esc | close the view |

The header links do the same with the mouse. Settings: `vDiff.sublime-settings`.

## The channel

Two files in this repository feed Package Control, and the distinction between them matters:

- `repository.json` — a **repository** file. Its top-level key is `packages`, and it describes
  vDiff itself: where the code lives and which releases exist. `"tags": true` means Package
  Control reads the repository's git tags, so publishing a release is just
  `git tag v1.0.1 && git push --tags`. A package whose tags are all unpushed resolves to zero
  releases, and Package Control drops it from the list without an error — if vDiff is missing
  from **Install Package**, check the tags reached GitHub.
- `channel.json` — a **channel** file. Its top-level key is `repositories`, and it is nothing but
  a list of repository-file URLs. This is the one users add to `channels`.

The indirection is what makes a channel worth having: users add one URL once, and every package
you later append to `repositories` shows up in their **Install Package** list without them
changing anything.

### Adding another package to the channel

Append the URL of that package's *repository* file to `repositories` in `channel.json`:

```json
{
    "$schema": "sublime://packagecontrol.io/schemas/channel",
    "schema_version": "4.0.0",
    "repositories": [
        "https://raw.githubusercontent.com/vaclavt/vdiff/main/repository.json",
        "https://raw.githubusercontent.com/vaclavt/vAI/main/channel.json"
    ]
}
```

Two things to watch:

- The URL must be a repository file (top-level `packages`), whatever it is named. vAI's
  `channel.json` is a repository file despite its name, so it belongs in a `repositories` array —
  Package Control rejects any file without a `repositories` key when it is listed under `channels`.
- Package Control fetches anonymously over HTTPS, so the URL has to be publicly readable. A
  package on a private host is not installable this way.

If you would rather skip the channel for a single package, a repository file can be added directly
to the `repositories` array of Package Control's settings instead.
