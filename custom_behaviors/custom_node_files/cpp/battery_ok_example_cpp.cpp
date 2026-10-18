// Example custom BT Condition node (C++) -- see greet_example.cpp's
// comment for why this ships in the repo. A real deployment would read
// this robot's actual battery telemetry (no such subsystem exists in
// this repo yet, matching bt_engine/nodes/*.py's own honest-scoping
// stubs) -- this uses a fixed simulated reading so the example is
// deterministic and testable without any hardware at all.
//
// Deliberately no halt() override -- BT::ConditionNode::halt() is
// declared "override final" in the real installed behaviortree_cpp
// headers; a Condition never returns RUNNING, nothing to cancel.
#include <behaviortree_cpp/behavior_tree.h>

// TODO(hardware): replace with a real battery-telemetry read.
static const double SIMULATED_BATTERY_PERCENT = 76.0;

class BatteryOkExample : public BT::ConditionNode
{
public:
    BatteryOkExample(const std::string& name, const BT::NodeConfig& config)
        : BT::ConditionNode(name, config) {}

    static BT::PortsList providedPorts()
    {
        return {
            BT::InputPort<double>("min_level")
        };
    }

    BT::NodeStatus tick() override
    {
        auto min_level_input = getInput<double>("min_level");
        double min_level = min_level_input ? min_level_input.value() : 20.0;

        bool ok = SIMULATED_BATTERY_PERCENT >= min_level;
        // stderr, not stdout -- see greet_example_cpp.cpp's own comment.
        std::cerr << "[BatteryOkExample] battery=" << SIMULATED_BATTERY_PERCENT
                   << "% min_level=" << min_level
                   << " -> " << (ok ? "SUCCESS" : "FAILURE") << std::endl;
        return ok ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
    }
};
