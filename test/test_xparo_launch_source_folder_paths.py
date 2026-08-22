"""_derive_source_folder (launch/xparo_launch.py) -- bidirectional file
sync (see /home/scientist/.claude/plans/breezy-splashing-koala.md): a
"local" launch (developing against a local Django server, this exact
machine/checkout -- xparo_environment's own existing meaning) should sync
custom_behaviors/custom_envs content into the real, git-tracked SOURCE
tree the owner actually hand-edits, not only ever the colcon-generated
install tree -- confirmed live (via a real LaunchContext + this repo's
actual colcon workspace) to actually resolve this way at launch time, not
just in this pure-function unit test; see this session's own manual
verification for that.

Loaded via importlib against the file's explicit path, same reasoning
and pattern as test_xparo_launch_rosbag_topics.py's own loader (self-
contained per file, not shared -- `launch/` here is a plain data
directory, not a proper importable submodule, and its name collides with
the real `launch` ROS2 package this file imports from at its own top
level).
"""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCH_FILE = os.path.normpath(os.path.join(HERE, '..', 'launch', 'xparo_launch.py'))


def _load_xparo_launch_module():
    spec = importlib.util.spec_from_file_location('xparo_launch_under_test', LAUNCH_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def xparo_launch():
    return _load_xparo_launch_module()


class TestDeriveSourceFolder:
    def test_derives_the_real_src_xparo_path_from_a_standard_colcon_workspace_layout(self, xparo_launch, tmp_path):
        ws = tmp_path / "ros_packages"
        (ws / "src" / "xparo").mkdir(parents=True)
        install_share_dir = ws / "install" / "xparo" / "share" / "xparo"
        install_share_dir.mkdir(parents=True)

        result = xparo_launch._derive_source_folder(str(install_share_dir), "custom_behaviors")

        assert result == str(ws / "src" / "xparo" / "custom_behaviors")

    def test_falls_back_to_the_install_tree_path_when_no_source_checkout_exists(self, xparo_launch, tmp_path):
        # A real, separate-machine production deployment -- no src/ at all.
        install_share_dir = tmp_path / "ros_packages" / "install" / "xparo" / "share" / "xparo"
        install_share_dir.mkdir(parents=True)

        result = xparo_launch._derive_source_folder(str(install_share_dir), "custom_behaviors")

        assert result == str(install_share_dir / "custom_behaviors")

    def test_this_repos_own_real_workspace_resolves_to_its_real_source_tree(self, xparo_launch):
        # Not a synthetic tmp_path -- this repo's own actual, currently
        # checked-out layout, the exact case this feature exists for.
        real_install_share_dir = os.path.normpath(
            os.path.join(HERE, '..', '..', '..', 'install', 'xparo', 'share', 'xparo'),
        )
        expected_source_dir = os.path.normpath(os.path.join(HERE, '..', 'custom_behaviors'))

        result = xparo_launch._derive_source_folder(real_install_share_dir, "custom_behaviors")

        assert result == expected_source_dir
