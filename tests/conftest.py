import os
import sys

import pytest

# tests import the package directly; sublime/sublime_plugin come from tests/stubs
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "stubs"))


@pytest.fixture(autouse=True)
def scheme_override_sandbox(tmp_path, monkeypatch):
    """Keep the colour-scheme override palette.py writes inside the test's own tmp_path

    Both caches are per session and every stub view shares one id, so they would otherwise leak and
    make override assertions depend on test order.
    """
    import sublime

    from vdiff import palette, state

    palette.reset()
    monkeypatch.setattr(state, "STATES", {})
    monkeypatch.setattr(sublime, "packages_path", lambda: str(tmp_path))


@pytest.fixture
def rings(monkeypatch):
    """Record visual bells instead of drawing them"""
    import sublime  # noqa: F401  (the stub must be importable before vdiff.render)

    from vdiff import render

    bells = []
    monkeypatch.setattr(render, "flash", lambda view: bells.append(view))
    return bells


def put_caret(view, row):
    """Move the stub view's caret to the start of `row` (0 is the first row below the header)"""
    import sublime

    from vdiff import render

    view.sel().clear()
    view.sel().add(sublime.Region(render.index_point(view, row)))
