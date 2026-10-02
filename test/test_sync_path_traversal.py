"""2026-09-28 stress test finding F11 (CRITICAL): engine.py's on_ws_message
custom_aiml/custom_maps/custom_Sets sync branches built their write path as
`os.path.join(dir, name + ext)` with no containment check at all -- a name
of "../../etc/whatever" (a project member types this into the dashboard's
"add/rename a behaviour tree or environment file" box) wrote straight
through to that traversed location, past the whole package root, with
fully attacker-controlled content. Confirmed live against a real robot
process before this fix.

Django now rejects such a name before it's ever saved or relayed (apps/
analytics/models.py's validate_custom_file_name, applied in
data_analyis.py's ADD_custom_folder_file/RENAME_custom_folder_file) -- but
this robot must not depend on that alone (a different/older server, or any
future sender of these same sync keys, might not enforce it). These tests
cover the robot's OWN defense: remote_ops.safe_path_in, applied at the
point of every write in on_ws_message.
"""
import os

import pytest


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-traversal-test", **kwargs)
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path / "custom_behaviors")
    engine.files["xparo_custom_evns_folder_path"] = str(tmp_path / "custom_envs")
    custom_files_dir = tmp_path / "custom_files"
    custom_files_dir.mkdir()
    engine.files["xparo_custom_files_folder_path"] = str(custom_files_dir)
    return engine


class TestCustomAimlSyncRejectsTraversal:
    def test_a_normal_name_still_writes_correctly(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.on_ws_message('ws', {"custom_aiml": {"my_tree": "<AlwaysSuccess/>"}})
        written = tmp_path / "custom_behaviors" / "custom_aiml" / "my_tree.xml"
        assert written.exists()
        assert "AlwaysSuccess" in written.read_text()

    def test_a_traversal_name_is_refused_and_writes_nothing_outside_the_folder(self, tmp_path):
        engine = _make_engine(tmp_path)
        canary = tmp_path / "custom_behaviors" / "canary.txt"
        (tmp_path / "custom_behaviors").mkdir(parents=True, exist_ok=True)
        canary.write_text("ORIGINAL\n")

        engine.on_ws_message('ws', {"custom_aiml": {
            "../canary": "<Sequence><AlwaysSuccess name=\"PWNED\"/></Sequence>",
        }})

        # Nothing landed at the traversed location (canary.xml one level up)...
        assert not (tmp_path / "custom_behaviors" / "canary.xml").exists()
        # ...the canary itself is untouched...
        assert canary.read_text() == "ORIGINAL\n"
        # ...and nothing leaked into custom_aiml/ under that name either.
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        assert not any("canary" in p.name for p in aiml_dir.iterdir()) if aiml_dir.exists() else True

    def test_a_bad_entry_does_not_block_the_rest_of_the_same_batch(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.on_ws_message('ws', {"custom_aiml": {
            "../../escape_attempt": "<AlwaysFailure/>",
            "good_tree": "<AlwaysSuccess/>",
        }})
        assert (tmp_path / "custom_behaviors" / "custom_aiml" / "good_tree.xml").exists()
        assert not any(
            "escape_attempt" in p.name
            for base in (tmp_path, tmp_path / "custom_behaviors")
            for p in (base.iterdir() if base.exists() else [])
        )

    def test_deep_traversal_toward_the_filesystem_root_is_also_refused(self, tmp_path):
        engine = _make_engine(tmp_path)
        deep = "../" * 20 + "tmp_traversal_canary"
        engine.on_ws_message('ws', {"custom_aiml": {deep: "<AlwaysSuccess/>"}})
        assert not os.path.exists("/tmp_traversal_canary.xml")


class TestCustomMapsSyncRejectsTraversal:
    def test_a_normal_env_name_still_writes_correctly(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.on_ws_message('ws', {"custom_maps": {"my_env": "KEY=value\n"}})
        written = tmp_path / "custom_envs" / "custom_maps" / "my_env.env"
        assert written.exists()
        assert written.read_text() == "KEY=value\n"

    def test_a_traversal_name_is_refused(self, tmp_path):
        engine = _make_engine(tmp_path)
        (tmp_path / "custom_envs").mkdir(parents=True, exist_ok=True)
        canary = tmp_path / "custom_envs" / "canary.env"
        canary.write_text("ORIGINAL=1\n")

        engine.on_ws_message('ws', {"custom_maps": {"../canary": "PWNED=true\n"}})

        assert canary.read_text() == "ORIGINAL=1\n"
        assert not (tmp_path / "custom_envs" / "canary.env.env").exists()


class TestCustomSetsSyncRejectsTraversal:
    """custom_Sets has NO forced extension at all -- kk is the whole
    filename -- so this guard matters even more here than for
    custom_aiml/custom_maps."""

    def test_a_normal_name_still_writes_correctly(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.on_ws_message('ws', {"custom_Sets": {"notes.txt": "hello"}})
        written = tmp_path / "custom_files" / "notes.txt"
        assert written.exists()
        assert written.read_text() == "hello"

    def test_a_traversal_name_is_refused(self, tmp_path):
        engine = _make_engine(tmp_path)
        canary = tmp_path / "canary_above_custom_files.txt"
        canary.write_text("ORIGINAL\n")

        engine.on_ws_message('ws', {"custom_Sets": {"../canary_above_custom_files.txt": "PWNED"}})

        assert canary.read_text() == "ORIGINAL\n"

    def test_lowercase_custom_sets_key_is_equally_guarded(self, tmp_path):
        engine = _make_engine(tmp_path)
        canary = tmp_path / "canary2.txt"
        canary.write_text("ORIGINAL\n")
        engine.on_ws_message('ws', {"custom_sets": {"../canary2.txt": "PWNED"}})
        assert canary.read_text() == "ORIGINAL\n"


class TestSafePathIn:
    def test_returns_none_for_anything_that_escapes(self, tmp_path):
        from xparo.remote_ops import safe_path_in
        base = tmp_path / "base"
        base.mkdir()
        assert safe_path_in(str(base), "../escaped") is None
        assert safe_path_in(str(base), "../../../../etc/passwd") is None
        assert safe_path_in(str(base), "/etc/passwd") is None

    def test_returns_the_resolved_path_for_a_safe_name(self, tmp_path):
        from xparo.remote_ops import safe_path_in
        base = tmp_path / "base"
        base.mkdir()
        result = safe_path_in(str(base), "plain_name.xml")
        assert result is not None
        assert result == (base / "plain_name.xml").resolve()

    def test_a_name_that_is_exactly_the_base_dir_is_allowed(self, tmp_path):
        # Defensive edge case: an empty/"." name resolving to base itself
        # (not something callers here ever pass, but the guard's own logic
        # explicitly allows target == base rather than misclassifying it).
        from xparo.remote_ops import safe_path_in
        base = tmp_path / "base"
        base.mkdir()
        assert safe_path_in(str(base), ".") == base.resolve()
