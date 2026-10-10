"""The colour-scheme scopes vDiff paints changed lines with, provisioned into a scheme override

view.add_regions repaints text with the region scope's foreground, so one scope cannot give both a
red/green background and the file's syntax colours. This module generates a scope per (background
kind, colour) pair carrying both, and render paints each run of same-coloured tokens with it.

Sublime picks up a new override only on a restart, so the palette is probed and written in one pass
rather than a colour at a time
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

import sublime

from .utils import settings

LOG = logging.getLogger("vdiff")

# tints under the token layer: the scheme's own diff scopes need no restart, and the token layer repaints their text
SCOPE_DEL = "diff.deleted"
SCOPE_INS = "diff.inserted"
SCOPE_DEL_CHAR = "diff.deleted.char"
SCOPE_INS_CHAR = "diff.inserted.char"
# context rows of the all-files view: the editor background, nudged so the colour still paints
SCOPE_CTX = "vdiff.context"

# background kinds (tinted ones paint changed lines, "ctx" the all-files context rows) and their tint scopes
TINT_KINDS: Tuple[str, ...] = ("del", "ins", "del.char", "ins.char")
KINDS: Tuple[str, ...] = TINT_KINDS + ("ctx",)
KIND_TINT: Dict[str, str] = {"del": SCOPE_DEL, "ins": SCOPE_INS, "del.char": SCOPE_DEL_CHAR, "ins.char": SCOPE_INS_CHAR,
                             "ctx": SCOPE_CTX}

# all-files file heading, its status letter and its -/+ counts; their colours come from settings, these when one is empty
SCOPE_FILE_HEADER = "vdiff.file.header"
SCOPE_FILE_DEL_COUNT = "vdiff.file.del.count"
SCOPE_FILE_INS_COUNT = "vdiff.file.ins.count"
SCOPE_FILE_STATUS = "vdiff.file.status"
FILE_HEADER_BACKGROUND = "#0000FF"
FILE_STATUS_FOREGROUND = "#ffffff"
# sublime's former markup.deleted / markup.inserted colours
FILE_DEL_FOREGROUND = "#d64646"
FILE_INS_FOREGROUND = "#46d646"

# colours the user may set; an empty line colour keeps the colour scheme's tint or the plain-text look
COLOUR_SETTINGS: Tuple[str, ...] = ("deleted_line_background", "inserted_line_background",
                                    "line_numbers_foreground", "line_numbers_background",
                                    "file_header_background", "file_header_deleted_foreground",
                                    "file_header_inserted_foreground", "file_header_status_foreground")
# the scopes those colours go into: the changed-line tints and the all-files line-number column
SCOPE_LINE_DEL = "vdiff.line.del"
SCOPE_LINE_INS = "vdiff.line.ins"
SCOPE_LINE_NUMBERS = "vdiff.line.numbers"

# tints to provision when the active scheme defines none of its own (some third-party schemes)
FALLBACK_TINTS: Dict[str, str] = {
    SCOPE_DEL: "hsla(357, 45%, 60%, 0.15)",
    SCOPE_DEL_CHAR: "hsla(357, 60%, 60%, 0.30)",
    SCOPE_INS: "hsla(114, 31%, 68%, 0.15)",
    SCOPE_INS_CHAR: "hsla(114, 45%, 68%, 0.30)",
}

# standard scope roots probed for the palette; no API returns it whole, and parsing the scheme
# file would mean re-doing sublime's merging
PALETTE_PROBES: Tuple[str, ...] = (
    "source",
    "comment",
    "string",
    "constant.numeric",
    "constant.language",
    "constant.character.escape",
    "variable",
    "variable.parameter",
    "variable.function",
    "keyword",
    "keyword.operator",
    "storage.type",
    "storage.modifier",
    "entity.name.function",
    "entity.name.class",
    "entity.name.tag",
    "entity.other.attribute-name",
    "entity.other.inherited-class",
    "support.function",
    "support.class",
    "support.type",
    "support.constant",
    "punctuation.definition",
    "punctuation.separator",
    "meta.function-call",
    "invalid",
)

# {override file name: {colour: index}} for every scheme provisioned this session
_palettes: Dict[str, Dict[str, int]] = {}
# the override name the last ensure() resolved, so scope_for knows which palette to read
_active: Optional[str] = None
# schemes the user has already been told need a restart
_notified: Set[Optional[str]] = set()
# {override file name: the user colours it was provisioned with}, so changing one re-provisions
_provisioned_with: Dict[str, Dict[str, str]] = {}


def reset() -> None:
    """Drop the caches, so the next ensure() re-reads and re-probes. Only the tests need this"""
    global _active
    _palettes.clear()
    _notified.clear()
    _provisioned_with.clear()
    _active = None


def claim_restart_notice(view: sublime.View) -> bool:
    """Whether to tell the user to restart; True at most once per scheme

    Only when the override is written but not yet loaded
    """
    if not (_active and _palettes.get(_active)) or tokens_resolved(view, KINDS) or _active in _notified:
        return False
    _notified.add(_active)
    return True


def scope_background(view: sublime.View, scope: str) -> Optional[str]:
    """The scheme's background for a scope, or None"""
    return _style(view, scope).get("background")


def _style(view: sublime.View, scope: str) -> Dict[str, Any]:
    """The scheme's style for a scope, or {} when style_for_scope fails"""
    try:
        return view.style_for_scope(scope)
    except Exception:
        LOG.debug("style_for_scope failed for %s", scope, exc_info=True)
        return {}


def foreground(view: sublime.View, scope: str) -> Optional[str]:
    """The scheme's foreground for a scope, normalised by sublime to #rrggbb"""
    return _style(view, scope).get("foreground")


def tokens_resolved(view: sublime.View, kinds: Tuple[str, ...] = TINT_KINDS) -> bool:
    """Whether the generated (tint + colour) scopes are live

    The token layer must never paint with a scope sublime does not know: it would draw that text in
    sublime's unknown-scope green. A new override loads only on a restart, so the writing session sees false
    """
    colours = (_active and _palettes.get(_active)) or {}
    if not colours:
        return False
    # a kind added since the override was written loads only after a restart, so check each kind
    # probe a colour unlike the default foreground (an unknown scope falls back to it);
    # ctx's background may go unreported
    default = foreground(view, "text.plain")
    colour, index = next(((c, i) for c, i in colours.items() if c != default), next(iter(colours.items())))
    # live when the rule's background shows, or its distinct foreground does
    return all(scope_background(view, _scope_name(kind, index)) or
               (colour != default and foreground(view, _scope_name(kind, index)) == colour)
               for kind in kinds)


def scope_for(kind: str, colour: str) -> Optional[str]:
    """The generated scope carrying `kind`'s background and `colour`, or None if never provisioned"""
    index = _palettes.get(_active or "", {}).get(colour)
    return None if index is None else _scope_name(kind, index)


def token_key(scope: str) -> str:
    """The add_regions key for a generated scope"""
    return scope.replace("vdiff.", "vdiff.tok.", 1)


def token_keys() -> List[str]:
    """Every region key the token layer can use, so a re-render can erase them all"""
    colours = _palettes.get(_active or "", {})
    return [token_key(_scope_name(kind, i)) for kind in KINDS for i in sorted(colours.values())]


def user_colours() -> Dict[str, str]:
    """{setting: colour} for COLOUR_SETTINGS, "" where unset"""
    return {name: settings().get(name) or "" for name in COLOUR_SETTINGS}


def line_scope(view: sublime.View, kind: str) -> str:
    """Tint scope for a changed line, "del" or "ins": the user's colour once its rule is live, else the scheme's"""
    own, setting, scheme = ((SCOPE_LINE_DEL, "deleted_line_background", SCOPE_DEL) if kind == "del" else
                            (SCOPE_LINE_INS, "inserted_line_background", SCOPE_INS))
    return own if settings().get(setting) and scope_background(view, own) else scheme


def line_numbers_scope(view: sublime.View) -> str:
    """Scope for the all-files line-number column: the user's colours once their rule is live, else plain text"""
    if (settings().get("line_numbers_foreground") or settings().get("line_numbers_background")) \
            and scope_background(view, SCOPE_LINE_NUMBERS):
        return SCOPE_LINE_NUMBERS
    return "text.plain"


def _scope_name(kind: str, index: int) -> str:
    """Generated scope name for a background kind and palette index"""
    return f"vdiff.{kind}.c{index}"


def ensure(view: sublime.View) -> None:
    """Provision the scopes for the active scheme, once per scheme per session

    A new override loads only on a restart; tokens_resolved() says when it is live
    """
    global _active
    name = override_name(view)
    _active = name
    user = user_colours()
    if not name or (name in _palettes and _provisioned_with.get(name) == user):
        return
    _provisioned_with[name] = user
    # merge into the existing override, if any
    path = os.path.join(sublime.packages_path(), "User", name)
    try:
        scheme: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                # invalid JSON raises here, leaving the file untouched; render still shows the changed lines
                scheme = sublime.decode_value(f.read()) or {}
        rules = scheme.setdefault("rules", [])
        # probe the scheme, record the palette, and write only when a rule changed
        tints = _tints(view, user)
        colours = {colour: i for i, colour in enumerate(_probe_palette(view))}
        _palettes[name] = colours
        if not _merge_rules(rules, tints, colours, view, user):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scheme, f, indent=4)
        LOG.info("provisioned %d colours into colour scheme override %s", len(colours), path)
    except Exception:
        LOG.warning("could not write colour scheme override %s", path, exc_info=True)
        # mark the scheme as handled so this session does not retry it
        _palettes.setdefault(name, {})


def _probe_palette(view: sublime.View) -> List[str]:
    """The distinct foregrounds the active scheme uses, in probe order so indices stay meaningful"""
    seen: List[str] = []
    for scope in PALETTE_PROBES:
        colour = foreground(view, scope)
        if colour and colour not in seen:
            seen.append(colour)
    return seen


def _tints(view: sublime.View, user: Dict[str, str]) -> Dict[str, str]:
    """{tint scope: background} for the scheme's diff colours, falling back to ours per colour pair

    All or nothing per pair: taking only the half a scheme defines would put our hue next to its own.
    A line colour the user set wins over both
    """
    tints: Dict[str, str] = {}
    for line_scope, char_scope in ((SCOPE_DEL, SCOPE_DEL_CHAR), (SCOPE_INS, SCOPE_INS_CHAR)):
        line = scope_background(view, line_scope)
        char = scope_background(view, char_scope)
        tints[line_scope] = line if line and char else FALLBACK_TINTS[line_scope]
        tints[char_scope] = char if line and char else FALLBACK_TINTS[char_scope]
    for scope, setting in ((SCOPE_DEL, "deleted_line_background"), (SCOPE_INS, "inserted_line_background")):
        if user[setting]:
            tints[scope] = user[setting]
    # a region whose background is missing, transparent or equal to the editor's is filled with its
    # foreground, so context colours carry the editor background nudged by an invisible step
    tints[SCOPE_CTX] = _nudge(_global_background(view))
    return tints


def _global_background(view: sublime.View) -> str:
    """The scheme's editor background; style_for_scope reports none for scopes that do not set one"""
    try:
        background = view.style().get("background")
    except Exception:
        LOG.debug("view.style failed", exc_info=True)
        background = None
    return background or "#000000"


def _nudge(colour: str) -> str:
    """#rrggbb[aa] with blue moved one step, towards the middle so it never overflows"""
    if not (colour.startswith("#") and len(colour) in (7, 9)):
        return colour
    blue = int(colour[5:7], 16)
    return f"{colour[:5]}{blue + 1 if blue < 128 else blue - 1:02x}{colour[7:]}"


def _put_rule(rules: List[Dict[str, str]], present: Dict[Optional[str], Dict[str, str]], wanted: Dict[str, str]) -> bool:
    """Add `wanted`, or make the rule with its scope equal to it; True when anything changed"""
    rule = present.get(wanted["scope"])
    if rule is None:
        rules.append(wanted)
    elif rule != wanted:
        rule.clear()
        rule.update(wanted)
    else:
        return False
    return True


def _merge_rules(rules: List[Dict[str, str]], tints: Dict[str, str], colours: Dict[str, int],
                 view: sublime.View, user: Dict[str, str]) -> bool:
    """Bring the scheme's rules up to date, in place; True when anything changed

    Each (kind, colour) rule carries a background and a foreground, as a background alone would flatten
    the line's colours; plain tints are added only when missing and never overwritten, so a hand-tuned one survives
    """
    present = {rule.get("scope"): rule for rule in rules}
    # the all-files file heading and its deletion and insertion counts
    header = user["file_header_background"] or FILE_HEADER_BACKGROUND
    wanted = [{"scope": SCOPE_FILE_HEADER, "background": header}]
    for scope, colour in ((SCOPE_FILE_STATUS, user["file_header_status_foreground"] or FILE_STATUS_FOREGROUND),
                          (SCOPE_FILE_DEL_COUNT, user["file_header_deleted_foreground"] or FILE_DEL_FOREGROUND),
                          (SCOPE_FILE_INS_COUNT, user["file_header_inserted_foreground"] or FILE_INS_FOREGROUND)):
        wanted.append({"scope": scope, "background": header, "foreground": colour, "font_style": "bold"})
    # the user's line colours; an unset one keeps any old rule, which render then ignores
    for scope, setting in ((SCOPE_LINE_DEL, "deleted_line_background"), (SCOPE_LINE_INS, "inserted_line_background")):
        if user[setting]:
            wanted.append({"scope": scope, "background": user[setting]})
    if user["line_numbers_foreground"] or user["line_numbers_background"]:
        # both halves, as a region without a background is filled with its foreground
        wanted.append({"scope": SCOPE_LINE_NUMBERS,
                       "foreground": user["line_numbers_foreground"] or foreground(view, "text.plain") or "#ffffff",
                       "background": user["line_numbers_background"] or _nudge(_global_background(view))})
    changed = False
    for rule in wanted:
        changed = _put_rule(rules, present, rule) or changed
    # plain tints the scheme lacks; the context tint gets no rule of its own
    for scope, background in tints.items():
        if scope != SCOPE_CTX and scope not in present and not scope_background(view, scope):
            rules.append({"scope": scope, "background": background})
            changed = True
    # one rule per (kind, colour) pair; a palette shifted under an earlier session's rule is ours to correct
    for colour, index in colours.items():
        for kind in KINDS:
            pair = {"scope": _scope_name(kind, index), "background": tints[KIND_TINT[kind]], "foreground": colour}
            changed = _put_rule(rules, present, pair) or changed
    return changed


def override_name(view: sublime.View) -> Optional[str]:
    """File name in Packages/User that overrides the active scheme (same base name, .sublime-color-scheme)"""
    try:
        # already resolved through "auto" and dark/light settings by sublime
        name = sublime.ui_info()["color_scheme"]["resolved_value"]
    except Exception:
        name = view.settings().get("color_scheme") or ""
    # no usable scheme name: nothing to override
    if not name or name == "auto":
        return None
    root, _ = os.path.splitext(os.path.basename(name))
    return root + ".sublime-color-scheme"
