""" Small helpers shared across vdiff modules """
import logging

import sublime

from . import git

SETTINGS_FILE = "vDiff.sublime-settings"

LOG = logging.getLogger("vdiff")

_settings = None


def settings():
    """Cached settings handle; a sublime Settings object stays live"""
    global _settings
    if _settings is None:
        _settings = sublime.load_settings(SETTINGS_FILE)
        _settings.add_on_change("vdiff.git_binary", apply_git_binary)
        apply_git_binary()
    return _settings


def apply_git_binary():
    """Push the configured git executable into the (sublime-free) git module"""
    git.GIT_BINARY = settings().get("git_binary") or "git"


def status(message):
    """Status-bar message with the plugin prefix"""
    sublime.status_message("vDiff: {}".format(message))


def run_async(work, done):
    """Run `work()` on sublime's worker thread, hand the result to `done` on the main thread"""

    def job():
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
