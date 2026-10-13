"""Event listener: state cleanup, refresh when the tab comes back or a file is saved, the keymap's mode context"""
from typing import Optional

import sublime
import sublime_plugin

from . import render, state
from .commands import refresh_if_changed, restore_view

# the keymap context naming the review view's mode; a setting context would not compare strings reliably
MODE_CONTEXT = "vdiff_mode"


class VdiffEventListener(sublime_plugin.EventListener):
    def on_pre_close(self, view: sublime.View) -> None:
        """Forget the closing view's state"""
        state.drop(view)

    def on_activated(self, view: sublime.View) -> None:
        """Re-render when the tab comes back and the file, the index or the branches changed meanwhile"""
        if state.is_review_view(view):
            refresh_if_changed(view, state.get(view) or restore_view(view))

    def on_selection_modified(self, view: sublime.View) -> None:
        """In the all-files view the header follows the caret from file to file"""
        if state.is_review_view(view):
            st = state.get(view)
            if st is not None:
                render.follow_caret(view, st)

    def on_post_save(self, view: sublime.View) -> None:
        """Review views showing the saved file re-render; the fingerprint decides which ones"""
        for view_id, st in state.tracked():
            review = sublime.View(view_id)
            if review.is_valid():
                refresh_if_changed(review, st)

    def on_query_context(self, view: sublime.View, key: str, operator: int, operand: object, match_all: bool) -> Optional[bool]:
        """Answer the keymap's `vdiff_mode` context: whether the review view shows "diff" or "branches"

        Anything else is left to other packages (None)
        """
        if key != MODE_CONTEXT or not state.is_review_view(view):
            return None
        mode = view.settings().get(state.MODE_SETTING) or state.MODE_DIFF
        if operator == sublime.OP_EQUAL:
            return mode == operand
        if operator == sublime.OP_NOT_EQUAL:
            return mode != operand
        return None
