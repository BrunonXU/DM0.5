// Read-only command traffic monitor. It creates subscriptions, never publishers.
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <control_msgs/msg/gripper_command.hpp>
#include <geometry_msgs/msg/twist_stamped.hpp>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <vector>

void received(const std::string & key, const rclcpp::MessageInfo & info) {
  const auto & gid = info.get_rmw_message_info().publisher_gid;
  std::ostringstream out;
  // RMW message identity matches rmw_get_gid_for_publisher (not graph GUID).
  for (size_t i=0; i<16; ++i)
    out << std::hex << std::setfill('0') << std::setw(2) << unsigned(gid.data[i]);
  std::cout << "CMD " << key << " " << out.str() << std::endl;
}
int main(int argc, char **argv) {
  if (argc != 6) return 2;
  std::string name=argv[1];
  rclcpp::init(argc,argv);
  auto node=std::make_shared<rclcpp::Node>(name,
      rclcpp::NodeOptions().enable_rosout(false).start_parameter_services(false));
  auto qos=rclcpp::QoS(100).best_effort().durability_volatile();
  auto arms=node->create_subscription<sensor_msgs::msg::JointState>(argv[2],qos,
    [](sensor_msgs::msg::JointState::ConstSharedPtr, const rclcpp::MessageInfo & info){received("arms_command",info);});
  auto left=node->create_subscription<control_msgs::msg::GripperCommand>(argv[3],qos,
    [](control_msgs::msg::GripperCommand::ConstSharedPtr, const rclcpp::MessageInfo & info){received("left_gripper_command",info);});
  auto right=node->create_subscription<control_msgs::msg::GripperCommand>(argv[4],qos,
    [](control_msgs::msg::GripperCommand::ConstSharedPtr, const rclcpp::MessageInfo & info){received("right_gripper_command",info);});
  auto base=node->create_subscription<geometry_msgs::msg::TwistStamped>(argv[5],qos,
    [](geometry_msgs::msg::TwistStamped::ConstSharedPtr, const rclcpp::MessageInfo & info){received("base_command",info);});
  auto heartbeat=node->create_wall_timer(std::chrono::milliseconds(100),[]{std::cout<<"ALIVE"<<std::endl;});
  std::cout<<"READY"<<std::endl;
  rclcpp::spin(node);
  rclcpp::shutdown();
}
