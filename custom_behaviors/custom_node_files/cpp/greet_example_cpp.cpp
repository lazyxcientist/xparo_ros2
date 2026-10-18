// Example custom BT Action node (C++) -- ships with the repo so the
// multi-language custom-node pipeline is testable with zero Django/
// dashboard interaction: a fresh `colcon build --symlink-install` alone
// registers this tag (see engine.py's sync_custom_node_files,
// examples_manifest.json). Genuinely working, not a TODO stub.
//
// Deliberately no halt() override -- BT::SyncActionNode::halt() is
// declared "override final" in the real installed behaviortree_cpp
// headers (confirmed by actually compiling against the real library);
// this node ticks synchronously to completion, nothing to cancel.
#include <behaviortree_cpp/behavior_tree.h>

class GreetExample : public BT::SyncActionNode
{
public:
    GreetExample(const std::string& name, const BT::NodeConfig& config)
        : BT::SyncActionNode(name, config) {}

    static BT::PortsList providedPorts()
    {
        return {
            BT::InputPort<std::string>("name"),
            BT::OutputPort<std::string>("greeting")
        };
    }

    BT::NodeStatus tick() override
    {
        auto name_input = getInput<std::string>("name");
        std::string who = (name_input && !name_input.value().empty()) ? name_input.value() : "robot";

        std::string greeting = "Hello, " + who + "! XPARO custom node pipeline is working.";
        // stderr, not stdout -- the host process's stdout is the
        // JSON-lines tick protocol (ProcessBackedNode/js_host.js's shared
        // convention); anything else on stdout corrupts it.
        std::cerr << "[GreetExample] " << greeting << std::endl;

        setOutput("greeting", greeting);
        return BT::NodeStatus::SUCCESS;
    }
};
