"""Multi-language custom BT node system, Phase 6/7: runners.py's
BashProcessNode edge cases (exit-code -> status mapping, output parsing,
timeout) not already covered by test_custom_node_files_sync.py's
happy-path coverage. JS/C++ factory wiring is covered end-to-end there
too (via sync_custom_node_files); ProcessBackedNode's own generic
protocol machinery (both factories build on it) is covered by
test_process_runner.py.
"""
import os
import stat

import pytest
from py_trees import common

from xparo.bt_engine.runners import BashProcessNode


def _write_script(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class TestBashProcessNode:
    def test_exit_0_is_success(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", "#!/usr/bin/env bash\nexit 0\n")
        node = BashProcessNode("n", {}, {}, script_path=script)
        assert node.update() == common.Status.SUCCESS

    def test_exit_1_is_failure(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", "#!/usr/bin/env bash\nexit 1\n")
        node = BashProcessNode("n", {}, {}, script_path=script)
        assert node.update() == common.Status.FAILURE

    def test_exit_2_is_running(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", "#!/usr/bin/env bash\nexit 2\n")
        node = BashProcessNode("n", {}, {}, script_path=script)
        assert node.update() == common.Status.RUNNING

    def test_an_exit_code_outside_0_1_2_is_treated_as_failure_not_silently_accepted(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", "#!/usr/bin/env bash\nexit 7\n")
        node = BashProcessNode("n", {}, {}, script_path=script)
        assert node.update() == common.Status.FAILURE

    def test_inputs_arrive_as_upper_cased_env_vars(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", '#!/usr/bin/env bash\n[ "$GOAL" = "dock_A" ] && exit 0 || exit 1\n')
        node = BashProcessNode("n", {"goal": "dock_A"}, {}, script_path=script)
        assert node.update() == common.Status.SUCCESS

    def test_kv_stdout_lines_are_written_to_the_target_blackboard_key(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", '#!/usr/bin/env bash\necho "DISTANCE_REMAINING=3.5"\nexit 0\n')
        blackboard = {}
        node = BashProcessNode("n", {"distance_remaining": "nav.distance"}, blackboard, script_path=script)
        node.update()
        assert blackboard == {"nav.distance": "3.5"}

    def test_a_hung_script_times_out_as_failure(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", "#!/usr/bin/env bash\nsleep 10\n")
        node = BashProcessNode("n", {}, {}, script_path=script)
        node.TICK_TIMEOUT_S = 0.3
        assert node.update() == common.Status.FAILURE

    def test_a_missing_script_is_a_controlled_failure_not_an_exception(self, tmp_path):
        node = BashProcessNode("n", {}, {}, script_path=str(tmp_path / "does_not_exist.sh"))
        assert node.update() == common.Status.FAILURE

    def test_halt_is_a_safe_noop_no_live_child_to_kill(self, tmp_path):
        script = _write_script(tmp_path, "s.sh", "#!/usr/bin/env bash\nexit 2\n")
        node = BashProcessNode("n", {}, {}, script_path=script)
        node.update()
        node.halt()  # must not raise
