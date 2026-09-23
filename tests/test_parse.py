from vdiff import parse

TWO_HUNKS = """diff --git a/x.py b/x.py
index 111..222 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +1,3 @@
 a
-b
+B
 c
@@ -10,2 +10,3 @@
 j
+k
 l
"""

NO_NEWLINE = """--- a/t.txt
+++ b/t.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""

UNTRACKED = """diff --git a/dev/null b/n.py
new file mode 100644
--- /dev/null
+++ b/n.py
@@ -0,0 +1,2 @@
+one
+two
"""

DELETED = """--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""

BINARY = """diff --git a/i.png b/i.png
index 111..222 100644
Binary files a/i.png and b/i.png differ
"""

EMPTY_CONTEXT = """--- a/e.py
+++ b/e.py
@@ -1,3 +1,3 @@
 x
 
-\ty
+\tz
"""


def test_two_hunks_numbering():
    diff = parse.parse_unified(TWO_HUNKS)
    kinds = [l.kind for l in diff.lines]
    assert kinds == [" ", "-", "+", " ", " ", "+", " "]
    assert diff.lines[1] == parse.Line("-", 2, None, "b")
    assert diff.lines[2] == parse.Line("+", None, 2, "B")
    assert diff.lines[3] == parse.Line(" ", 3, 3, "c")
    assert diff.lines[4] == parse.Line(" ", 10, 10, "j")
    assert diff.lines[5] == parse.Line("+", None, 11, "k")
    assert diff.lines[6] == parse.Line(" ", 11, 12, "l")
    assert not diff.binary
    assert parse.counts(diff.lines) == (2, 1)


def test_no_newline_marker_ignored():
    diff = parse.parse_unified(NO_NEWLINE)
    assert [l.text for l in diff.lines] == ["old", "new"]


def test_untracked_and_deleted_files():
    new = parse.parse_unified(UNTRACKED)
    assert [l.new_no for l in new.lines] == [1, 2] and all(l.kind == "+" for l in new.lines)
    gone = parse.parse_unified(DELETED)
    assert all(l.kind == "-" for l in gone.lines) and [l.old_no for l in gone.lines] == [1, 2]


def test_binary():
    diff = parse.parse_unified(BINARY)
    assert diff.binary and diff.lines == []


def test_empty_context_line_and_tabs():
    diff = parse.parse_unified(EMPTY_CONTEXT)
    assert [l.text for l in diff.lines] == ["x", "", "\ty", "\tz"]


def test_change_blocks_and_pairs():
    lines = parse.parse_unified(TWO_HUNKS).lines
    blocks = parse.change_blocks(lines)
    assert blocks == [(1, 3), (5, 6)]
    assert parse.paired_lines(lines, blocks[0]) == [(1, 2)]
    assert parse.paired_lines(lines, blocks[1]) == []


def test_char_ranges():
    old_r, new_r = parse.char_ranges("    self.viewport = xy", "    self.viewport = (x, y)")
    assert old_r == []
    assert new_r == [(20, 21), (22, 24), (25, 26)]
    assert parse.char_ranges("foo = bar", "foo = baz") == ([(8, 9)], [(8, 9)])
    assert parse.char_ranges("completely", "different!!!") == ([], [])
    assert parse.char_ranges("a" * 1500, "a" * 1499 + "b") == ([], [])


def test_line_for_open():
    lines = parse.parse_unified(TWO_HUNKS).lines
    assert parse.line_for_open(lines, 1) == 2  # deleted row -> following line
    assert parse.line_for_open(lines, 6) == 12
    assert parse.line_for_open([], 5) == 1
    only_deleted = parse.parse_unified(DELETED).lines
    assert parse.line_for_open(only_deleted, 1) == 1
