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




def colour_option(monkeypatch, name, value):
    """Set one of the colour settings for this test"""
    from vdiff import utils
    monkeypatch.setitem(sublime.load_settings(utils.SETTINGS_FILE), name, value)


def test_user_line_colours_get_rules_and_tint_the_token_layer(tmp_path, monkeypatch):
    colour_option(monkeypatch, "deleted_line_background", "#ff000040")
    colour_option(monkeypatch, "line_numbers_foreground", "#888888")
    view = sublime.View()
    palette.ensure(view)

    rules = by_scope(tmp_path)
    assert rules[palette.SCOPE_LINE_DEL] == {"scope": palette.SCOPE_LINE_DEL, "background": "#ff000040"}
    assert palette.SCOPE_LINE_INS not in rules
    # the token layer repaints deleted text, so it carries the same background
    colour = view.styles["comment"]["foreground"]
    assert rules[palette.scope_for("del", colour)]["background"] == "#ff000040"
    # a foreground alone still gets a background, or sublime would fill the region with the foreground
    numbers = rules[palette.SCOPE_LINE_NUMBERS]
    assert numbers["foreground"] == "#888888" and numbers["background"]


def test_changing_a_colour_setting_rewrites_the_override(tmp_path, monkeypatch):
    view = sublime.View()
    palette.ensure(view)
    assert palette.SCOPE_LINE_INS not in by_scope(tmp_path)
    colour_option(monkeypatch, "inserted_line_background", "#00ff0040")
    palette.ensure(view)
    assert by_scope(tmp_path)[palette.SCOPE_LINE_INS]["background"] == "#00ff0040"


def test_render_uses_the_user_scopes_only_once_set_and_live(monkeypatch):
    view = sublime.View()
    # unset: the scheme's diff tint and the plain-text line numbers, as before
    assert palette.line_scope(view, "del") == palette.SCOPE_DEL
    assert palette.line_numbers_scope(view) == "text.plain"
    colour_option(monkeypatch, "deleted_line_background", "#ff000040")
    colour_option(monkeypatch, "line_numbers_background", "#222222")
    # set, but the override is not loaded yet: an unknown scope would paint nothing
    assert palette.line_scope(view, "del") == palette.SCOPE_DEL
    view.override_loaded = True
    assert palette.line_scope(view, "del") == palette.SCOPE_LINE_DEL
    assert palette.line_scope(view, "ins") == palette.SCOPE_INS
    assert palette.line_numbers_scope(view) == palette.SCOPE_LINE_NUMBERS


def test_file_header_colours_come_from_settings(tmp_path, monkeypatch):
    palette.ensure(sublime.View())
    rules = by_scope(tmp_path)
    # unset: the built-in defaults
    assert rules[palette.SCOPE_FILE_HEADER]["background"] == palette.FILE_HEADER_BACKGROUND
    colour_option(monkeypatch, "file_header_background", "#202040")
    colour_option(monkeypatch, "file_header_inserted_foreground", "#00ff00")
    palette.ensure(sublime.View())
    rules = by_scope(tmp_path)
    assert rules[palette.SCOPE_FILE_HEADER]["background"] == "#202040"
    # the counts sit on the heading, so they share its background
    assert rules[palette.SCOPE_FILE_INS_COUNT]["background"] == "#202040"
    assert rules[palette.SCOPE_FILE_INS_COUNT]["foreground"] == "#00ff00"
    assert rules[palette.SCOPE_FILE_DEL_COUNT]["foreground"] == palette.FILE_DEL_FOREGROUND


def test_the_status_letter_is_white_on_the_heading_unless_set(tmp_path, monkeypatch):
    palette.ensure(sublime.View())
    rule = by_scope(tmp_path)[palette.SCOPE_FILE_STATUS]
    assert rule["foreground"] == palette.FILE_STATUS_FOREGROUND == "#ffffff"
    assert rule["background"] == by_scope(tmp_path)[palette.SCOPE_FILE_HEADER]["background"]
    colour_option(monkeypatch, "file_header_status_foreground", "#ffff00")
    palette.ensure(sublime.View())
    assert by_scope(tmp_path)[palette.SCOPE_FILE_STATUS]["foreground"] == "#ffff00"
