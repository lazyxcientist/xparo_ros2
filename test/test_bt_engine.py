"""Behaviour Tree redesign Phase 9 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): xml_parser/expr/node_registry/tree_builder/
executor. QUICK_DELIVERY_TREE_FRAGMENT is the real example tree's inner
content (custom_behaviors/quick_delivery_tree.xml), embedded rather than
read from disk so this test doesn't depend on that file's current state.
"""
from unittest.mock import MagicMock

import pytest
from py_trees import common

from xparo.bt_engine import expr, tree_builder
from xparo.bt_engine.executor import BehaviorTreeExecutor
from xparo.bt_engine.node_registry import NODE_REGISTRY
from xparo.bt_engine.xml_parser import TreeParseError, parse_fragment, single_root_child

QUICK_DELIVERY_TREE_FRAGMENT = """
<Sequence>
      <Script
        code="audio_short_punchy_path := robot_task_backend_path + '/sound/short-punchy-sine-wave.mp3'" />

      <PlayAudio file_path="{audio_short_punchy_path}" async="true" volume="90" />
      <DockRobot dock_method="undocking" dock_name="{dock_name}" dock_type="{dock_type}"
        max_staging_time="{dock_max_staging_time}" _skipIf="!docking_feature_enabled" />

      <CheckBatteryLevel min_level="20.0" />

      <ParamSet node_name="velocity_smoother" param_name="max_velocity"
        param_value="{max_speed_array}" param_type="double_array" />

      <Sequence>
        <LoadNextDelivery />
        <Parallel success_count="2" failure_count="1">
          <SpeakText text="{message_to_play}" voice="en_US-amy-medium" speed="0.9" volume="100" />
          <NotifyPatient tray="{current_tray}" />
        </Parallel>
        <SpeakText text="Thank you for using our service. Have a great day!"
          voice="en_US-amy-medium" speed="0.9" volume="100" />
      </Sequence>

      <DockRobot dock_method="docking" dock_name="{dock_name}" dock_type="{dock_type}"
        max_staging_time="{dock_max_staging_time}" _skipIf="!docking_feature_enabled" />
    </Sequence>
"""


# --- ad-hoc dummy leaves, registered only for this test module, matching
# the plan's own testing spec ("tree_builder builds the real example tree
# with ad-hoc dummy leaves") -- Phase 10 has real ones for these tags now,
# but these dummies keep this test module's original intent (proving the
# build/tick/skip pipeline works generically) independent of any one leaf
# implementation's own behaviour, which get their own tests instead.
def _dummy_leaf_builder(name, attrs, blackboard, children, ros_node):
    import py_trees as pt

    class _Dummy(pt.behaviour.Behaviour):
        def update(self):
            return common.Status.SUCCESS

    return _Dummy(name=name)


@pytest.fixture(autouse=True)
def _register_dummy_leaves():
    dummy_tags = [
        "PlayAudio", "DockRobot", "CheckBatteryLevel", "ParamSet",
        "LoadNextDelivery", "SpeakText", "NotifyPatient",
    ]
    saved = {tag: NODE_REGISTRY.get(tag) for tag in dummy_tags}
    for tag in dummy_tags:
        NODE_REGISTRY[tag] = _dummy_leaf_builder
    yield
    for tag, original in saved.items():
        if original is None:
            NODE_REGISTRY.pop(tag, None)
        else:
            NODE_REGISTRY[tag] = original


class TestXmlParser:
    def test_single_root_fragment_parses_directly(self):
        root = single_root_child(parse_fragment('<PlayAudio file_path="{sound}" />'))
        assert root.tag == "PlayAudio"

    def test_multi_top_level_fragment_needs_wrapping(self):
        root = single_root_child(parse_fragment(QUICK_DELIVERY_TREE_FRAGMENT))
        assert root.tag == "Sequence"

    def test_empty_fragment_raises(self):
        with pytest.raises(TreeParseError):
            parse_fragment("")

    def test_malformed_xml_raises(self):
        with pytest.raises(TreeParseError):
            parse_fragment('<Sequence><Unclosed attr="{oops}"></Sequence>')

    def test_ambiguous_multi_root_raises(self):
        with pytest.raises(TreeParseError):
            single_root_child(parse_fragment("<PlayAudio /><DockRobot />"))


class TestExpr:
    def test_script_assignment(self):
        bb = {"robot_task_backend_path": "/opt/xparo"}
        assigned = expr.run_script(
            "audio_short_punchy_path := robot_task_backend_path + '/sound/short-punchy-sine-wave.mp3'", bb
        )
        assert assigned == {"audio_short_punchy_path"}
        assert bb["audio_short_punchy_path"] == "/opt/xparo/sound/short-punchy-sine-wave.mp3"

    def test_multiple_assignments_semicolon_separated(self):
        bb = {}
        expr.run_script("a := 1; b := a + 2", bb)
        assert bb == {"a": 1, "b": 3}

    def test_skipif_style_bang_negation(self):
        assert expr.evaluate_condition("!docking_feature_enabled", {"docking_feature_enabled": False}) is True
        assert expr.evaluate_condition("!docking_feature_enabled", {"docking_feature_enabled": True}) is False

    def test_undefined_variable_in_condition_is_falsy_not_an_error(self):
        assert expr.evaluate_condition("!never_set", {}) is False

    def test_undefined_variable_in_plain_evaluate_raises(self):
        with pytest.raises(expr.ExpressionError):
            expr.evaluate("never_set + 1", {})

    def test_does_not_execute_arbitrary_code(self):
        """Mirrors the outer Django repo's engine.py precedent
        (test_eval_key_is_not_specially_handled) against reintroducing an
        eval()-shaped RCE -- calls of any kind must be rejected outright,
        not sandboxed."""
        with pytest.raises(expr.ExpressionError):
            expr.evaluate('__import__("os").system("echo pwned")', {})

    def test_rejects_attribute_and_subscript_access(self):
        with pytest.raises(expr.ExpressionError):
            expr.evaluate("x.__class__", {"x": 1})
        with pytest.raises(expr.ExpressionError):
            expr.evaluate("x[0]", {"x": [1]})


class TestTreeBuilder:
    def test_builds_the_real_example_tree_with_dummy_leaves(self):
        blackboard = {
            "robot_task_backend_path": "/opt/xparo",
            "dock_name": "dock-1", "dock_type": "auto", "dock_max_staging_time": "30",
            "max_speed_array": "[1,1,1]", "message_to_play": "hello", "current_tray": "A",
            "docking_feature_enabled": False,
        }
        root = tree_builder.build_tree(QUICK_DELIVERY_TREE_FRAGMENT, blackboard)
        assert root is not None

    def test_skipif_guarded_node_is_skipped(self):
        blackboard = {
            "robot_task_backend_path": "/opt/xparo",
            "dock_name": "dock-1", "dock_type": "auto", "dock_max_staging_time": "30",
            "max_speed_array": "[1,1,1]", "message_to_play": "hello", "current_tray": "A",
            "docking_feature_enabled": False,  # -> !docking_feature_enabled is True -> skip
        }
        root = tree_builder.build_tree(QUICK_DELIVERY_TREE_FRAGMENT, blackboard)
        tree = __import__("py_trees").trees.BehaviourTree(root)
        # Tick enough times for the whole sequence to resolve (dummy leaves
        # all succeed immediately) -- the DockRobot dummy would also
        # succeed if ticked, so this test only proves something by
        # checking it was never *entered* (its dummy status stays INVALID).
        # Excludes the "DockRobot (conditional)" wrapper itself, which
        # correctly resolves to SUCCESS (skip == treated as succeeded) --
        # only the leaf it wraps must never have actually ticked.
        dock_robot_nodes = [n for n in root.iterate() if n.name == "DockRobot"]
        assert dock_robot_nodes
        for _ in range(5):
            tree.tick()
            if root.status in (common.Status.SUCCESS, common.Status.FAILURE):
                break
        for node in dock_robot_nodes:
            assert node.status == common.Status.INVALID, f"{node.name} should never have ticked"

    def test_unknown_tag_raises(self):
        with pytest.raises(tree_builder.UnknownNodeError):
            tree_builder.build_tree('<TotallyMadeUpTag foo="bar" />', {})

    def test_an_explicit_name_attribute_becomes_the_node_name(self):
        """The BT editor canvas (DownloadButton.js's buildNodesFromXml)
        uses the XML `name` attribute as the React Flow node's id, and
        live updates' node_name is matched against that id directly
        (moveRobotToNode) -- a node built without reading this attribute
        would never be highlightable on the canvas by name.
        """
        root = tree_builder.build_tree('<PlayAudio name="intro_audio" file_path="/x.mp3" />', {})
        assert root.name == "intro_audio"

    def test_no_name_attribute_falls_back_to_the_bare_tag(self):
        root = tree_builder.build_tree('<PlayAudio file_path="/x.mp3" />', {})
        assert root.name == "PlayAudio"

    def test_same_tag_used_twice_without_names_collide_matching_the_canvas_own_degraded_case(self):
        """Documents the honest limitation, not a bug: quick_delivery_tree.xml
        itself has two <DockRobot> and two <Sequence> nodes with no name=
        attribute -- both engines (this one and the canvas) fall back to
        the bare tag in that case, so live updates for either DockRobot
        are genuinely ambiguous until the tree's author gives each one a
        unique name= (which resolves it on both sides identically)."""
        root = tree_builder.build_tree(QUICK_DELIVERY_TREE_FRAGMENT, {
            "robot_task_backend_path": "/opt/xparo",
            "dock_name": "dock-1", "dock_type": "auto", "dock_max_staging_time": "30",
            "max_speed_array": "[1,1,1]", "message_to_play": "hello", "current_tray": "A",
            "docking_feature_enabled": False,
        })
        dock_robot_names = {n.name for n in root.iterate() if n.name == "DockRobot"}
        assert dock_robot_names == {"DockRobot"}  # both share this name -- indistinguishable

    def test_giving_each_dock_robot_a_unique_name_disambiguates_them(self):
        fragment = QUICK_DELIVERY_TREE_FRAGMENT.replace(
            'dock_method="undocking"', 'name="dock_out" dock_method="undocking"'
        ).replace(
            'dock_method="docking"', 'name="dock_in" dock_method="docking"'
        )
        root = tree_builder.build_tree(fragment, {
            "robot_task_backend_path": "/opt/xparo",
            "dock_name": "dock-1", "dock_type": "auto", "dock_max_staging_time": "30",
            "max_speed_array": "[1,1,1]", "message_to_play": "hello", "current_tray": "A",
            "docking_feature_enabled": False,
        })
        names = {n.name for n in root.iterate() if n.name in ("dock_out", "dock_in")}
        assert names == {"dock_out", "dock_in"}

    def test_conditional_wrapper_does_not_leak_into_node_names_of_unconditional_nodes(self):
        root = tree_builder.build_tree('<Sequence><LoadNextDelivery /></Sequence>', {})
        assert all("(conditional)" not in n.name for n in root.iterate())

    def test_sequence_has_memory_and_does_not_restart_already_succeeded_siblings(self):
        """Real bug caught live, not in a test first: mapping <Sequence> to
        py_trees' memory=False (BT.CPP v3's meaning) made a real multi-step
        tree (quick_delivery_tree.xml) loop forever -- every tick where a
        later child was still RUNNING reset current_child back to index 0,
        so an already-SUCCEEDED earlier sibling with internal state (like
        every stub action node's simulated-delay timer) got re-initialised
        and restarted from scratch, forever, never reaching the child after
        the RUNNING one. BTCPP_format="4" (every tree in this repo) means
        plain <Sequence> has memory (skip already-succeeded children,
        resume from whichever is RUNNING) -- confirmed by fixing this
        exact symptom, not just by reading BT.CPP's changelog.
        """
        import py_trees as pt

        class _RunsTwiceThenSucceeds(pt.behaviour.Behaviour):
            def __init__(self, name):
                super().__init__(name=name)
                self.ticks = 0

            def initialise(self):
                self.ticks = 0

            def update(self):
                self.ticks += 1
                return common.Status.SUCCESS if self.ticks >= 2 else common.Status.RUNNING

        first_leaf = _RunsTwiceThenSucceeds(name="first")
        second_leaf = _RunsTwiceThenSucceeds(name="second")

        NODE_REGISTRY["_FirstLeaf"] = lambda name, attrs, blackboard, children, ros_node: first_leaf
        NODE_REGISTRY["_SecondLeaf"] = lambda name, attrs, blackboard, children, ros_node: second_leaf
        try:
            root = tree_builder.build_tree("<Sequence><_FirstLeaf /><_SecondLeaf /></Sequence>", {})
            tree = __import__("py_trees").trees.BehaviourTree(root)
            for _ in range(6):
                tree.tick()
                if root.status in (common.Status.SUCCESS, common.Status.FAILURE):
                    break
        finally:
            del NODE_REGISTRY["_FirstLeaf"]
            del NODE_REGISTRY["_SecondLeaf"]

        assert root.status == common.Status.SUCCESS, "the sequence never converged -- looks like the memory=False bug is back"
        # If the first leaf had been wrongly restarted after the second
        # went RUNNING, it would have ticked far more than twice.
        assert first_leaf.ticks == 2


class TestExecutor:
    def test_ticks_a_synthetic_tree_and_emits_ordered_live_updates(self):
        mock_engine = MagicMock()
        mock_engine.add_live_update = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)

        status, blackboard = executor.run('<Sequence><LoadNextDelivery /></Sequence>', tick_rate_hz=0)

        assert status == common.Status.SUCCESS
        assert mock_engine.add_live_update.call_count >= 1
        events = [call.args[0] for call in mock_engine.add_live_update.call_args_list]
        for event in events:
            assert set(event.keys()) == {"node_name", "node_type", "uid", "prev", "curr", "timestamp", "datetime"}
        # The root Sequence's own final event must be SUCCESS -- the whole
        # point of the live-update feed is that curr reflects reality.
        root_events = [e for e in events if e["node_name"] == "Sequence"]
        assert root_events[-1]["curr"] == "SUCCESS"

    def test_node_type_reports_the_registration_tag_distinct_from_an_explicit_name(self):
        """Mirrors this project's own prior C++ RosTopicLogger exactly
        (node.name() vs node.registrationName()) -- node_type must stay
        the tag ("LoadNextDelivery") even when node_name is a custom
        name= attribute, not silently collapse to the same value."""
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)

        executor.run('<LoadNextDelivery name="grab_the_next_order" />', tick_rate_hz=0)

        events = [call.args[0] for call in mock_engine.add_live_update.call_args_list]
        assert events
        for event in events:
            assert event["node_name"] == "grab_the_next_order"
            assert event["node_type"] == "LoadNextDelivery"

    def test_node_type_without_an_explicit_name_falls_back_to_the_tag_for_both_fields(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)

        executor.run('<LoadNextDelivery />', tick_rate_hz=0)

        events = [call.args[0] for call in mock_engine.add_live_update.call_args_list]
        assert events
        for event in events:
            assert event["node_name"] == "LoadNextDelivery"
            assert event["node_type"] == "LoadNextDelivery"

    def test_prev_status_is_invalid_on_first_transition(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        executor.run('<LoadNextDelivery />', tick_rate_hz=0)
        first_event = mock_engine.add_live_update.call_args_list[0].args[0]
        assert first_event["prev"] == "INVALID"

    def test_a_sibling_never_reached_emits_no_spurious_event(self):
        """Real bug caught live: root.iterate() walks the *whole* tree,
        including a second Sequence child that a first-child FAILURE means
        never actually gets ticked. That sibling still sits at py_trees'
        own default Status.INVALID, indistinguishable by value from "never
        seen before" -- comparing against a bare dict.get() (defaulting to
        None) treated that as a real "None -> INVALID" transition and
        emitted a phantom event for a node that was never entered at all.
        """
        mock_engine = MagicMock()

        import py_trees as pt

        class _Fail(pt.behaviour.Behaviour):
            def update(self):
                return common.Status.FAILURE

        NODE_REGISTRY["AlwaysFails"] = lambda name, attrs, blackboard, children, ros_node: _Fail(name=name)
        try:
            executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
            executor.run(
                '<Sequence><AlwaysFails /><LoadNextDelivery /></Sequence>', tick_rate_hz=0,
            )
        finally:
            del NODE_REGISTRY["AlwaysFails"]

        events = [call.args[0] for call in mock_engine.add_live_update.call_args_list]
        assert not any(e["node_name"] == "LoadNextDelivery" for e in events), (
            "LoadNextDelivery was never reached (the Sequence failed on its first "
            "child) but still generated a live-update event"
        )

    def test_returns_the_blackboard_after_running(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        _, blackboard = executor.run(
            '<Script code="x := 1 + 2" />', blackboard={}, tick_rate_hz=0
        )
        assert blackboard["x"] == 3
