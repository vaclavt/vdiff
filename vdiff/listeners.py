""" Event listener: state cleanup, refresh when the tab comes back or a file is saved """
import sublime
import sublime_plugin

from . import state
from .commands import refresh_if_changed, restore_view


class VdiffEventListener(sublime_plugin.EventListener):
    def on_pre_close(self, view: sublime.View) -> None:
        state.drop(view)

    def on_activated(self, view: sublime.View) -> None:
        """Re-render when the tab comes back and the file or index changed meanwhile (agent edits)"""
        if state.is_review_view(view):
            refresh_if_changed(view, state.get(view) or restore_view(view))

    def on_post_save(self, view: sublime.View) -> None:
        """Review views showing the saved file re-render; the fingerprint decides which ones"""
        for view_id, st in state.tracked():
            review = sublime.View(view_id)
            if review.is_valid():
                refresh_if_changed(review, st)
