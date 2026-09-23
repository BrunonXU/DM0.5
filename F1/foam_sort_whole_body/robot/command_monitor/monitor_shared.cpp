// Subscription-only monitor in the caller process; no command publishers.
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <control_msgs/msg/gripper_command.hpp>
#include <geometry_msgs/msg/twist_stamped.hpp>
#include <thread>
#include <iomanip>
#include <sstream>
using Callback=void(*)(const char*);
struct Monitor {
  Callback emit;
  rclcpp::Context::SharedPtr context;
  rclcpp::Node::SharedPtr node;
  std::shared_ptr<rclcpp::executors::SingleThreadedExecutor> executor;
  std::vector<rclcpp::SubscriptionBase::SharedPtr> subs;
  rclcpp::TimerBase::SharedPtr timer;
  std::thread worker;
  void command(const char* key,const rclcpp::MessageInfo &info) {
    auto &gid=info.get_rmw_message_info().publisher_gid;
    std::ostringstream s;s<<"CMD "<<key<<" ";
    for(size_t i=0;i<RMW_GID_STORAGE_SIZE;++i)s<<std::hex<<std::setfill('0')<<std::setw(2)<<unsigned(gid.data[i]);
    emit(s.str().c_str());
  }
  template<class T> void subscribe(const char* topic,const char* key) {
    subs.push_back(node->create_subscription<T>(topic,rclcpp::QoS(100).best_effort().durability_volatile(),
      [this,key](typename T::ConstSharedPtr,const rclcpp::MessageInfo &info){command(key,info);}));
  }
  Monitor(Callback cb,const char* name,const char* arms,const char* left,const char* right,const char* base):emit(cb) {
    context=std::make_shared<rclcpp::Context>();context->init(0,nullptr);
    node=std::make_shared<rclcpp::Node>(name,rclcpp::NodeOptions().context(context).enable_rosout(false).start_parameter_services(false));
    subscribe<sensor_msgs::msg::JointState>(arms,"arms_command");
    subscribe<control_msgs::msg::GripperCommand>(left,"left_gripper_command");
    subscribe<control_msgs::msg::GripperCommand>(right,"right_gripper_command");
    subscribe<geometry_msgs::msg::TwistStamped>(base,"base_command");
    timer=node->create_wall_timer(std::chrono::milliseconds(100),[this]{emit("ALIVE");});
    rclcpp::ExecutorOptions options;options.context=context;
    executor=std::make_shared<rclcpp::executors::SingleThreadedExecutor>(options);executor->add_node(node);
    emit("READY");
    worker=std::thread([this]{try{executor->spin();}catch(...){emit("ERROR");}});
  }
  ~Monitor(){executor->cancel();if(worker.joinable())worker.join();context->shutdown("monitor closed");}
};
extern "C" void* f1_monitor_start(Callback cb,const char* name,const char* arms,const char* left,const char* right,const char* base) {
  try{return new Monitor(cb,name,arms,left,right,base);}catch(...){return nullptr;}
}
extern "C" void f1_monitor_stop(void* ptr){delete static_cast<Monitor*>(ptr);}
