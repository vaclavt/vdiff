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

    Both caches are per session and the stub hands every view the same id, so they would otherwise
    leak between tests and make every assertion about the override depend on test order.
    """
    import sublime

    from vdiff import palette, state

    palette.reset()
    monkeypatch.setattr(state, "STATES", {})
    monkeypatch.setattr(sublime, "packages_path", lambda: str(tmp_path))
