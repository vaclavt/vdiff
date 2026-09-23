import os
import sys

# tests import the package directly; sublime/sublime_plugin come from tests/stubs
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "stubs"))
