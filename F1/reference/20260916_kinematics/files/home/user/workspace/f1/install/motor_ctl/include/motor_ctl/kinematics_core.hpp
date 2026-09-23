#ifndef MOTOR_CTL__KINEMATICS_CORE_HPP_
#define MOTOR_CTL__KINEMATICS_CORE_HPP_

#include <rclcpp/rclcpp.hpp>
#include <f1_msgs/srv/forward_kinematics.hpp>
#include <f1_msgs/srv/inverse_kinematics.hpp>
#include "FxRobot.h"

#include <memory>
#include <string>
#include <mutex>

namespace motor_ctl
{

  /**
   * @brief 运动学服务核心类
   *
   * 封装天机臂正/逆运动学SDK，提供ROS2 Service接口
   * - /kinematics/forward_kinematics  正运动学求解
   * - /kinematics/inverse_kinematics  逆运动学求解（含零空间优化）
   */
  class KinematicsCore
  {
  public:
    explicit KinematicsCore(rclcpp::Node::SharedPtr node);
    ~KinematicsCore();

    /**
     * @brief 初始化运动学参数并创建ROS2服务
     * @return true 初始化成功
     */
    bool init();

  private:
    // ---- 运动学初始化 ----

    /**
     * @brief 从配置文件加载运动学参数并初始化双臂
     * @param config_path 配置文件路径 (.MvKDCfg)
     * @return true 加载并初始化成功
     */
    bool loadAndInitKinematics(const std::string &config_path);

    // ---- Service 回调 ----

    /**
     * @brief 正运动学回调
     * 输入关节角度 → 输出末端位姿矩阵（可选零空间投影矩阵）
     */
    void forwardKinematicsCallback(
        const std::shared_ptr<f1_msgs::srv::ForwardKinematics::Request> request,
        std::shared_ptr<f1_msgs::srv::ForwardKinematics::Response> response);

    /**
     * @brief 逆运动学回调
     * 输入目标位姿 + 参考关节角度 → 输出求解关节角度
     * 支持可选的零空间(NSP)优化
     */
    void inverseKinematicsCallback(
        const std::shared_ptr<f1_msgs::srv::InverseKinematics::Request> request,
        std::shared_ptr<f1_msgs::srv::InverseKinematics::Response> response);

    // ---- 成员变量 ----

    rclcpp::Node::SharedPtr node_;

    // Service 服务器
    rclcpp::Service<f1_msgs::srv::ForwardKinematics>::SharedPtr fk_service_;
    rclcpp::Service<f1_msgs::srv::InverseKinematics>::SharedPtr ik_service_;

    // 运动学配置数据（从文件加载）
    FX_INT32L type_[2];
    FX_DOUBLE grv_[2][3];
    FX_DOUBLE dh_[2][8][4];
    FX_DOUBLE pnva_[2][7][4];
    FX_DOUBLE bd_[2][4][3];
    FX_DOUBLE mass_[2][7];
    FX_DOUBLE mcp_[2][7][3];
    FX_DOUBLE inertia_[2][7][6];

    // 初始化状态
    bool arm_initialized_[2]; // [0]=左臂A, [1]=右臂B

    // 互斥锁（保护全局运动学函数调用）
    std::mutex kine_mutex_;
  };

} // namespace motor_ctl

#endif // MOTOR_CTL__KINEMATICS_CORE_HPP_
