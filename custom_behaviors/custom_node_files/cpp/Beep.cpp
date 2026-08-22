#include <behaviortree_cpp/behavior_tree.h>

class Beep : public BT::SyncActionNode
{
public:
    Beep(const std::string& name, const BT::NodeConfig& config)
        : BT::SyncActionNode(name, config) {}

    static BT::PortsList providedPorts()
    {
        return { BT::InputPort<std::string>("times") };
    }

    BT::NodeStatus tick() override
    {
        auto times = getInput<std::string>("times");
        std::cout << "[Beep] beeping " << times.value() << " time(s)" << std::endl;
        return BT::NodeStatus::SUCCESS;
    }
};