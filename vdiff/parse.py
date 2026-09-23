""" Unified diff -> lines with old/new numbers, change blocks, word-level ranges; no sublime imports """
import difflib
import re
from collections import namedtuple

# kind is " " (context), "-" (deleted) or "+" (inserted); the missing number is None
Line = namedtuple("Line", "kind old_no new_no text")
Diff = namedtuple("Diff", "lines binary")

# word-level marks only when the pair is mostly the same line, and never on huge lines (quadratic)
CHAR_DIFF_MIN_RATIO = 0.5
CHAR_DIFF_MAX_CHARS = 2000

_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)")


def parse_unified(text):
    """Parse one file's unified diff (any number of hunks) into a Diff"""
    lines = []
    binary = False
    old_no = new_no = 0
    in_hunk = False
    for raw in text.split("\n"):
        if raw.startswith("@@"):
            match = _HUNK.match(raw)
            old_no, new_no = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
            in_hunk = True
            continue
        if not in_hunk:
            if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                binary = True
            continue
        if raw == "":
            # only the trailing split remainder; an empty context line is " "
            continue
        marker, body = raw[0], raw[1:]
        if marker == " ":
            lines.append(Line(" ", old_no, new_no, body))
            old_no += 1
            new_no += 1
        elif marker == "-":
            lines.append(Line("-", old_no, None, body))
            old_no += 1
        elif marker == "+":
            lines.append(Line("+", None, new_no, body))
            new_no += 1
        elif raw.startswith("diff "):
            # a second file in the output; we asked for one, ignore the rest
            break
        # "\ No newline at end of file" and anything else is skipped
    return Diff(lines, binary)


def change_blocks(lines):
    """[(start, end)) index ranges of consecutive changed lines"""
    blocks = []
    start = None
    for i, line in enumerate(lines):
        if line.kind != " ":
            if start is None:
                start = i
        elif start is not None:
            blocks.append((start, i))
            start = None
    if start is not None:
        blocks.append((start, len(lines)))
    return blocks


def counts(lines):
    """(insertions, deletions)"""
    ins = dele = 0
    for line in lines:
        if line.kind == "+":
            ins += 1
        elif line.kind == "-":
            dele += 1
    return ins, dele


def paired_lines(lines, block):
    """(deleted_index, inserted_index) pairs of a block with equal - and + counts"""
    start, end = block
    deleted = [i for i in range(start, end) if lines[i].kind == "-"]
    inserted = [i for i in range(start, end) if lines[i].kind == "+"]
    if not deleted or len(deleted) != len(inserted):
        return []
    return list(zip(deleted, inserted))


def char_ranges(old, new):
    """Character ranges that differ: ([(a, b) in old], [(a, b) in new]); empty when too different or too long"""
    if len(old) + len(new) > CHAR_DIFF_MAX_CHARS:
        return [], []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    if matcher.quick_ratio() < CHAR_DIFF_MIN_RATIO or matcher.ratio() < CHAR_DIFF_MIN_RATIO:
        return [], []
    old_ranges = []
    new_ranges = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            old_ranges.append((i1, i2))
        if tag in ("replace", "insert") and j2 > j1:
            new_ranges.append((j1, j2))
    return old_ranges, new_ranges


def line_for_open(lines, index):
    """New-file line number to open for a row: a deleted row maps to the next surviving line"""
    if not lines:
        return 1
    index = max(0, min(index, len(lines) - 1))
    for line in lines[index:]:
        if line.new_no is not None:
            return line.new_no
    for line in reversed(lines[:index]):
        if line.new_no is not None:
            return line.new_no
    return 1
