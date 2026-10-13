"""Small helpers shared across vdiff modules"""
import logging
from typing import Any, Callable, Optional

import sublime

from . import git

SETTINGS_FILE = "vDiff.sublime-settings"

DEBOUNCE_MS = 60

LOG = logging.getLogger("vdiff")

_settings: Optional[sublime.Settings] = None


def settings() -> sublime.Settings:
    """Cached settings handle; a sublime Settings object stays live"""
    global _settings
    if _settings is None:
        _settings = sublime.load_settings(SETTINGS_FILE)
        _settings.add_on_change("vdiff.git_binary", apply_git_binary)
        apply_git_binary()
    return _settings


def apply_git_binary() -> None:
    """Push the configured git executable into the (sublime-free) git module"""
    git.GIT_BINARY = settings().get("git_binary") or "git"


def commits_count() -> int:
    """How many commits the commits panel and the branches view list"""
    return int(settings().get("commits_count", 30) or 30)


def file_count(count: int, qualifier: str = "") -> str:
    """'1 file' / '3 files' for a message; a qualifier goes before the noun ('1 staged file')"""
    noun = "file" if count == 1 else "files"
    return f"{count} {qualifier} {noun}" if qualifier else f"{count} {noun}"


def status(message: str) -> None:
    """Status-bar message with the plugin prefix"""
    sublime.status_message(f"vDiff: {message}")


def run_async(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
    """Run `work()` on sublime's worker thread, hand the result to `done` on the main thread"""

    def job() -> None:
        """Worker-thread body: run `work`, pass on its result or report its error"""
        try:
            result = work()
        except Exception as error:
            # the traceback helps when debugging; the status message is what the user sees
            message = str(error) or error.__class__.__name__
            LOG.warning("vdiff worker failed: %s", message, exc_info=LOG.isEnabledFor(logging.DEBUG))
            sublime.set_timeout(lambda: status(message))
            return
        sublime.set_timeout(lambda: done(result))

    sublime.set_timeout_async(job)
