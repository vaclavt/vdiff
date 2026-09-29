import json

import sublime  # the stub, via conftest

from vdiff import palette


def scheme_file(tmp_path):
    """The override of whichever scheme the stub reports as active"""
    return tmp_path / "User" / palette.override_name(sublime.View())


def override(tmp_path):
    """The rules palette.py wrote into that override"""
    path = scheme_file(tmp_path)
    return json.loads(path.read_text())["rules"] if path.exists() else None


def by_scope(tmp_path):
    return {rule["scope"]: rule for rule in override(tmp_path)}


def test_a_scheme_with_diff_colours_needs_no_tint_rules(tmp_path):
    view = sublime.View()
    palette.ensure(view)

    # the tints are the scheme's own scopes, so only the generated colour rules are ours to write
    assert not [r for r in override(tmp_path) if r["scope"].startswith("diff.")]


def test_a_scheme_without_diff_colours_gets_our_tints(tmp_path):
    view = sublime.View()
    # all or nothing per pair: taking only the half a scheme has would put our hue next to its own
    del view.styles["diff.deleted.char"]
    del view.styles["diff.inserted"]
    del view.styles["diff.inserted.char"]
    palette.ensure(view)

    rules = by_scope(tmp_path)
    assert rules[palette.SCOPE_INS]["background"] == palette.FALLBACK_TINTS[palette.SCOPE_INS]
    assert rules[palette.SCOPE_DEL_CHAR]["background"] == palette.FALLBACK_TINTS[palette.SCOPE_DEL_CHAR]
    # the one colour it does define is left to the scheme
    assert palette.SCOPE_DEL not in rules


def test_every_palette_colour_gets_a_rule_per_background_kind(tmp_path):
    view = sublime.View()
    palette.ensure(view)

    rules = by_scope(tmp_path)
    colours = {view.styles[scope]["foreground"] for scope in ("comment", "string", "keyword")}
    for colour in colours:
        scope = palette.scope_for("ins", colour)
        assert scope is not None
        # both properties, which is the whole point: the tint alone would flatten the text
        assert rules[scope] == {"scope": scope, "background": view.styles["diff.inserted"]["background"],
                                "foreground": colour}
        for kind in palette.KINDS:
            assert palette.scope_for(kind, colour) in rules


def test_a_colour_the_scheme_does_not_use_has_no_scope():
    palette.ensure(sublime.View())
    assert palette.scope_for("ins", "#123456") is None


def test_the_override_is_written_once_per_scheme(tmp_path):
    view = sublime.View()
    palette.ensure(view)
    scheme_file(tmp_path).write_text(json.dumps({"rules": []}))

    # sublime reloads the whole scheme every time we touch the file, so nothing must rewrite it
    palette.ensure(view)
    assert override(tmp_path) == []


def test_a_hand_edited_override_is_never_clobbered(tmp_path):
    (tmp_path / "User").mkdir()
    path = tmp_path / "User" / "Mariana.sublime-color-scheme"
    # comments are legal in a .sublime-color-scheme but decode_value here is plain json
    path.write_text('{\n    // mine\n    "rules": []\n}')

    palette.ensure(sublime.View())
    assert "// mine" in path.read_text()


