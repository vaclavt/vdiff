""" The colour-scheme scopes vDiff paints changed lines with, provisioned into a scheme override

view.add_regions repaints text with the region scope's foreground, so one scope cannot give both a
red/green background and the file's syntax colours. This module generates a scope per (background
kind, colour) pair carrying both, and render paints each run of same-coloured tokens with it.

Sublime picks up a new override only on a restart, so the palette is probed and written in one pass
rather than a colour at a time.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

import sublime

LOG = logging.getLogger("vdiff")

# the full-line tint under the token layer, the scheme's own: the token layer repaints the text on
# top so the tint's foreground never shows, and a scope the scheme defines needs no restart.
SCOPE_DEL = "diff.deleted"
SCOPE_INS = "diff.inserted"
SCOPE_DEL_CHAR = "diff.deleted.char"
SCOPE_INS_CHAR = "diff.inserted.char"

# background kinds, and the tint each one draws over
KINDS: Tuple[str, ...] = ("del", "ins", "del.char", "ins.char")
KIND_TINT: Dict[str, str] = {"del": SCOPE_DEL, "ins": SCOPE_INS, "del.char": SCOPE_DEL_CHAR, "ins.char": SCOPE_INS_CHAR}

# tints to provision when the active scheme defines none of its own (some third-party schemes)
FALLBACK_TINTS: Dict[str, str] = {
    SCOPE_DEL: "hsla(357, 45%, 60%, 0.15)",
    SCOPE_DEL_CHAR: "hsla(357, 60%, 60%, 0.30)",
    SCOPE_INS: "hsla(114, 31%, 68%, 0.15)",
    SCOPE_INS_CHAR: "hsla(114, 45%, 68%, 0.30)",
}

# scope roots to probe for the scheme's palette: schemes colour by these standard names, so one pass
# yields the working set. No API returns a scheme's palette — style_for_scope takes one scope at a
# time, and reading the file would mean re-implementing sublime's override and .tmTheme merging.
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
# schemes the reader has already been told need a restart
_notified: Set[Optional[str]] = set()


def reset() -> None:
    """Drop the caches, so the next ensure() re-reads and re-probes. Only the tests need this"""
    global _active
    _palettes.clear()
    _notified.clear()
    _active = None


def claim_restart_notice(view: sublime.View) -> bool:
    """Whether the reader still has to be told to restart, asked once per scheme

    True only when a restart helps — provisioned but not yet loaded; it cannot fix one we never wrote.
    """
    if not (_active and _palettes.get(_active)) or tokens_resolved(view) or _active in _notified:
        return False
    _notified.add(_active)
    return True


def scope_background(view: sublime.View, scope: str) -> Optional[str]:
    """The scheme's background for a scope, or None; style_for_scope can fail on a closing view"""
    return _style(view, scope).get("background")


def _style(view: sublime.View, scope: str) -> Dict[str, Any]:
    try:
        return view.style_for_scope(scope)
    except Exception:
        LOG.debug("style_for_scope failed for %s", scope, exc_info=True)
        return {}


def foreground(view: sublime.View, scope: str) -> Optional[str]:
    """The scheme's foreground for a scope, normalised by sublime to #rrggbb"""
    return _style(view, scope).get("foreground")


def tokens_resolved(view: sublime.View) -> bool:
    """Whether the generated (tint + colour) scopes are live

    The token layer must never paint with a scope sublime does not know: it would draw that text in
    sublime's unknown-scope green. A new override loads only on a restart, so the writing session sees false.
    """
    colours = (_active and _palettes.get(_active)) or {}
    if not colours:
        return False
    # they are all written by the same pass into the same file, so any one of them answers for all
    return bool(scope_background(view, _scope_name(KINDS[0], next(iter(colours.values())))))


def scope_for(kind: str, colour: str) -> Optional[str]:
    """The generated scope carrying `kind`'s background and `colour`, or None if never provisioned"""
    index = _palettes.get(_active or "", {}).get(colour)
    return None if index is None else _scope_name(kind, index)


def token_key(scope: str) -> str:
    """The add_regions key for a generated scope; one naming rule, spelled here only"""
    return scope.replace("vdiff.", "vdiff.tok.", 1)


def token_keys() -> List[str]:
    """Every region key the token layer can use, so a re-render can erase them all"""
    colours = _palettes.get(_active or "", {})
    return [token_key(_scope_name(kind, i)) for kind in KINDS for i in sorted(colours.values())]


def _scope_name(kind: str, index: int) -> str:
    return f"vdiff.{kind}.c{index}"


def ensure(view: sublime.View) -> None:
    """Provision the scopes for the active scheme, once per scheme per session

    Whether they have taken effect is tokens_resolved()'s question: a new override loads only on a restart.
    """
    global _active
    name = override_name(view)
    _active = name
    if not name or name in _palettes:
        return
    path = os.path.join(sublime.packages_path(), "User", name)
    try:
        scheme: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                # a hand-edited scheme may hold comments decode_value rejects: the raise leaves the
                # file untouched, and render still shows the changed lines
                scheme = sublime.decode_value(f.read()) or {}
        rules = scheme.setdefault("rules", [])
        tints = _tints(view)
        colours = {colour: i for i, colour in enumerate(_probe_palette(view))}
        _palettes[name] = colours
        if not _merge_rules(rules, tints, colours, view):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scheme, f, indent=4)
        LOG.info("provisioned %d colours into colour scheme override %s", len(colours), path)
    except Exception:
        LOG.warning("could not write colour scheme override %s", path, exc_info=True)
        _palettes.setdefault(name, {})


def _probe_palette(view: sublime.View) -> List[str]:
    """The distinct foregrounds the active scheme uses, in probe order so indices stay meaningful"""
    seen: List[str] = []
    for scope in PALETTE_PROBES:
        colour = foreground(view, scope)
        if colour and colour not in seen:
            seen.append(colour)
    return seen


def _tints(view: sublime.View) -> Dict[str, str]:
    """{tint scope: background} for the scheme's diff colours, falling back to ours per colour pair

    All or nothing per pair: taking only the half a scheme defines would put our hue next to its own.
    """
    tints: Dict[str, str] = {}
    for line_scope, char_scope in ((SCOPE_DEL, SCOPE_DEL_CHAR), (SCOPE_INS, SCOPE_INS_CHAR)):
        line = scope_background(view, line_scope)
        char = scope_background(view, char_scope)
        tints[line_scope] = line if line and char else FALLBACK_TINTS[line_scope]
        tints[char_scope] = char if line and char else FALLBACK_TINTS[char_scope]
    return tints


def _merge_rules(rules: List[Dict[str, str]], tints: Dict[str, str], colours: Dict[str, int],
                 view: sublime.View) -> bool:
    """Bring the scheme's rules up to date, in place; True when anything changed

    add_regions draws text in the region scope's own foreground, so each (kind, colour) pair needs a
    rule carrying both; a background alone would flatten the line. Plain tints are written only when
    the scheme has none, and never overwritten, so a hand-tuned one survives.
    """
    present = {rule.get("scope"): rule for rule in rules}
    changed = False
    for scope, background in tints.items():
        if scope not in present and not scope_background(view, scope):
            rules.append({"scope": scope, "background": background})
            changed = True
    for colour, index in colours.items():
        for kind in KINDS:
            wanted = {"scope": _scope_name(kind, index), "background": tints[KIND_TINT[kind]],
                      "foreground": colour}
            rule = present.get(wanted["scope"])
            if rule is None:
                rules.append(wanted)
                changed = True
            elif rule != wanted:
                # the palette shifted under a rule an earlier session wrote; ours to keep correct
                rule.clear()
                rule.update(wanted)
                changed = True
    return changed


def override_name(view: sublime.View) -> Optional[str]:
    """File name in Packages/User that overrides the active scheme (same base name, .sublime-color-scheme)"""
    try:
        # already resolved through "auto" and dark/light settings by sublime
        name = sublime.ui_info()["color_scheme"]["resolved_value"]
    except Exception:
        name = view.settings().get("color_scheme") or ""
    if not name or name == "auto":
        return None
    root, _ = os.path.splitext(os.path.basename(name))
    return root + ".sublime-color-scheme"
