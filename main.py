""" vDiff plugin entry point: review uncommitted changes and recent commits inside Sublime Text

Sublime reloads a plugin only when this file changes; after editing a submodule, save this file to reload everything.
"""
import logging
import sys

import sublime

# sublime reloads only this file: drop the submodules so an edited one is re-imported below.
# Open review views lose their in-memory state and are rebuilt from their settings by _restore_all.
for _name in [n for n in sys.modules if n.startswith(__package__ + ".vdiff")]:
    del sys.modules[_name]

# imported for sublime's command discovery, not called here
from .vdiff.commands import (
    VdiffActionCommand,
    VdiffCommitsCommand,
    VdiffFilesCommand,
    restore_view,
)
from .vdiff.listeners import VdiffEventListener
from .vdiff.render import VdiffReplaceCommand
from .vdiff import state
from .vdiff.utils import settings


def plugin_loaded():
    """Set the log level and re-render review views restored from the last session"""
    level = logging.DEBUG if settings().get("debug", False) else logging.INFO
    logging.basicConfig()
    logging.getLogger("vdiff").setLevel(level)
    # the API is ready, but views are still being restored
    sublime.set_timeout(_restore_all, 500)


def _restore_all():
    for window in sublime.windows():
        for view in window.views():
            if state.is_review_view(view) and state.get(view) is None:
                restore_view(view)
