"""Multi-language custom BT node system, Phase 4/5: write_output
(xparo/bt_engine/nodes/base.py) -- the output-side mirror of the existing,
already-tested-elsewhere resolve_attrs input convention. This was
confirmed to be genuinely greenfield before implementing it: no leaf node
anywhere in this repo wrote a value back to the blackboard via an
output-port-attribute convention before this.
"""
from xparo.bt_engine.nodes.base import write_output


class TestWriteOutput:
    def test_writes_the_value_under_the_key_named_by_the_raw_attribute(self):
        blackboard = {}
        write_output({"distance_remaining": "nav.distance"}, blackboard, "distance_remaining", 14.2)
        assert blackboard == {"nav.distance": 14.2}

    def test_the_output_port_key_itself_is_never_used_as_the_blackboard_key(self):
        """The raw attribute's VALUE is the target -- not the port's own
        name -- confirmed distinct from resolve_attrs' input convention,
        which resolves "{name}" placeholders rather than treating the
        raw value itself as a key name."""
        blackboard = {}
        write_output({"distance_remaining": "nav.distance"}, blackboard, "distance_remaining", 14.2)
        assert "distance_remaining" not in blackboard

    def test_an_unwired_output_port_is_a_silent_noop(self):
        """No attribute at all for this key means whoever placed the node
        never wired this output anywhere -- a normal, valid choice, not
        an error the way a missing *required input* is."""
        blackboard = {"unrelated": "kept"}
        write_output({}, blackboard, "distance_remaining", 14.2)
        assert blackboard == {"unrelated": "kept"}

    def test_an_empty_string_attribute_is_also_treated_as_unwired(self):
        blackboard = {}
        write_output({"distance_remaining": ""}, blackboard, "distance_remaining", 14.2)
        assert blackboard == {}

    def test_overwrites_an_existing_value_at_the_target_key(self):
        blackboard = {"nav.distance": 0.0}
        write_output({"distance_remaining": "nav.distance"}, blackboard, "distance_remaining", 14.2)
        assert blackboard["nav.distance"] == 14.2

    def test_two_different_output_ports_can_write_to_different_keys_in_the_same_call_site(self):
        blackboard = {}
        attrs = {"distance_remaining": "nav.distance", "battery_level": "robot.battery"}
        write_output(attrs, blackboard, "distance_remaining", 14.2)
        write_output(attrs, blackboard, "battery_level", 82)
        assert blackboard == {"nav.distance": 14.2, "robot.battery": 82}
