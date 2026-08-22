// Written against BT.CPP's own node contract -- matches the framework
// this project's earlier C++ Behavior Tree implementation already used.
// No C++ runner exists in XPARO yet (Phase 4 onward); this is the real
// target shape a future runner will build/load, not a placeholder.
#include <behaviortree_cpp/behavior_tree.h>

class Dji : public BT::SyncActionNode
{
public:
    Dji(const std::string& name, const BT::NodeConfig& config)
        : BT::SyncActionNode(name, config) {}

    static BT::PortsList providedPorts()
    {
        return {
            BT::InputPort<std::string>("jkj")
        };
    }

    BT::NodeStatus tick() override
    {
        auto jkj = getInput<std::string>("jkj");

        // TODO: implement


        return BT::NodeStatus::SUCCESS;  // or RUNNING / FAILURE
    }

    void halt() override
    {
        // Only needed if tick() can return RUNNING.
        BT::SyncActionNode::halt();
    }
};
