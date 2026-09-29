""" The colour-scheme scopes vDiff paints changed lines with, provisioned into a scheme override

view.add_regions repaints the text it covers with the region scope's foreground, so one scope cannot
give both a red/green background and the file's own syntax colours. To get both, this module generates
a scope per (background kind, syntax colour) pair carrying both properties, and render paints each run
of same-coloured tokens with the matching one.

Sublime only picks up a colour-scheme override it has not seen before on a restart, so the palette is
probed and written in one pass rather than growing a colour at a time.
"""
import json
import logging
import os

import sublime

LOG = logging.getLogger("vdiff")

# the full-line tint under the token layer. The scheme's own, not ours: the token layer repaints the
# text on top, so the tint's foreground never shows, and a scope the scheme already defines needs no
# provisioning and so no restart before the changed lines have their colours.
SCOPE_DEL = "diff.deleted"
SCOPE_INS = "diff.inserted"
SCOPE_DEL_CHAR = "diff.deleted.char"
SCOPE_INS_CHAR = "diff.inserted.char"

# background kinds, and the tint each one draws over
KINDS = ("del", "ins", "del.char", "ins.char")
KIND_TINT = {"del": SCOPE_DEL, "ins": SCOPE_INS, "del.char": SCOPE_DEL_CHAR, "ins.char": SCOPE_INS_CHAR}

# tints to provision when the active scheme defines none of its own (some third-party schemes)
FALLBACK_TINTS = {
    SCOPE_DEL: "hsla(357, 45%, 60%, 0.15)",
    SCOPE_DEL_CHAR: "hsla(357, 60%, 60%, 0.30)",
    SCOPE_INS: "hsla(114, 31%, 68%, 0.15)",
    SCOPE_INS_CHAR: "hsla(114, 45%, 68%, 0.30)",
}

# scope roots to resolve for the scheme's palette. Colour schemes colour by these standard names, so
# one pass over them yields the working set in one write. There is no API that returns a scheme's own
# palette: style_for_scope resolves one scope at a time, and reading the scheme file would mean
# re-implementing sublime's merging of overrides and .tmTheme inheritance.
PALETTE_PROBES = (
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
_palettes = {}
# the override name the last ensure() resolved, so scope_for knows which palette to read
_active = None
# schemes the reader has already been told need a restart
_notified = set()


def reset():
    """Drop the caches, so the next ensure() re-reads and re-probes. Only the tests need this"""
    global _active
    _palettes.clear()
    _notified.clear()
    _active = None


def claim_restart_notice(view):
    """Whether the reader still has to be told to restart, asked once per scheme

    True only when a restart would actually help: the palette is provisioned but sublime has not
    loaded it yet. A scheme we failed to provision at all is not something restarting fixes.
    """
    if not _palettes.get(_active) or tokens_resolved(view) or _active in _notified:
        return False
    _notified.add(_active)
    return True


def scope_background(view, scope):
    """The scheme's background for a scope, or None; style_for_scope can fail on a closing view"""
    return _style(view, scope).get("background")


def _style(view, scope):
    try:
        return view.style_for_scope(scope)
    except Exception:
        LOG.debug("style_for_scope failed for %s", scope, exc_info=True)
        return {}


def foreground(view, scope):
    """The scheme's foreground for a scope, normalised by sublime to #rrggbb"""
    return _style(view, scope).get("foreground")


def tokens_resolved(view):
    """Whether the generated (tint + colour) scopes are live

    The token layer must never paint with a scope sublime does not know: it would draw that text in
    the bright green it uses for an unknown scope. Sublime loads an override it has not seen before
    only on a restart, so this stays false for the session that first wrote one.
    """
    colours = _palettes.get(_active) or {}
    if not colours:
        return False
    # they are all written by the same pass into the same file, so any one of them answers for all
    return bool(scope_background(view, _scope_name(KINDS[0], next(iter(colours.values())))))


def scope_for(kind, colour):
    """The generated scope carrying `kind`'s background and `colour`, or None if never provisioned"""
    index = _palettes.get(_active, {}).get(colour)
    return None if index is None else _scope_name(kind, index)


def token_key(scope):
    """The add_regions key for a generated scope; one naming rule, spelled here only"""
    return scope.replace("vdiff.", "vdiff.tok.", 1)


def token_keys():
    """Every region key the token layer can use, so a re-render can erase them all"""
    colours = _palettes.get(_active, {})
    return [token_key(_scope_name(kind, i)) for kind in KINDS for i in sorted(colours.values())]


def _scope_name(kind, index):
    return f"vdiff.{kind}.c{index}"


def ensure(view):
    """Provision the scopes for the active scheme, once per scheme per session

    Whether the rules have taken effect is a separate question, asked by tokens_resolved(), because
    sublime loads an override it has not seen before only on a restart.
    """
    global _active
    name = override_name(view)
    _active = name
    if not name or name in _palettes:
        return
    path = os.path.join(sublime.packages_path(), "User", name)
    try:
        scheme = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                # a scheme the user has hand-edited may hold comments decode_value rejects; the raise
                # leaves the file untouched, and render keeps the changed lines visible meanwhile
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


def _probe_palette(view):
    """The distinct foregrounds the active scheme uses, in probe order so indices stay meaningful"""
    seen = []
    for scope in PALETTE_PROBES:
        colour = foreground(view, scope)
        if colour and colour not in seen:
            seen.append(colour)
    return seen


def _tints(view):
    """{tint scope: background} for the scheme's diff colours, falling back to ours per colour pair

    All or nothing per pair: a scheme that defines diff.deleted but not diff.deleted.char would
    otherwise get its line tint next to our char tint, in a different hue.
    """
    tints = {}
    for line_scope, char_scope in ((SCOPE_DEL, SCOPE_DEL_CHAR), (SCOPE_INS, SCOPE_INS_CHAR)):
        line = scope_background(view, line_scope)
        char = scope_background(view, char_scope)
        tints[line_scope] = line if line and char else FALLBACK_TINTS[line_scope]
        tints[char_scope] = char if line and char else FALLBACK_TINTS[char_scope]
    return tints


def _merge_rules(rules, tints, colours, view):
    """Bring the scheme's rules up to date, in place; True when anything changed

    Each (background kind, palette colour) pair needs a rule carrying both, because add_regions draws
    the text it covers in the region scope's own foreground: a background alone would flatten the
    changed line to the scheme's default. The plain tints are only written for a scheme that has none
    of its own, and never overwritten, so one tuned by hand survives.
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


def override_name(view):
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
