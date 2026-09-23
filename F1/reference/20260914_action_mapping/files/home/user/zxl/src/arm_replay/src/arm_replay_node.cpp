// arm_replay_node: 从 rosbag2 mcap 文件回放录制的机械臂轨迹
// 作为 ArmExecute action server，接收 action_id，查找对应 bag 文件并回放

#include <rclcpp/rclcpp.hpp>
#include <rclcpp/utilities.hpp>
#include <rclcpp_action/rclcpp_action.hpp>
#include <rclcpp/serialization.hpp>
#include <rclcpp/serialized_message.hpp>
#include <rcl/logging.h>
#include <rcutils/logging.h>

#include <rosbag2_cpp/reader.hpp>
#include <rosbag2_cpp/converter_options.hpp>
#include <rosbag2_storage/storage_options.hpp>
#include <rosbag2_storage/serialized_bag_message.hpp>

#include <sensor_msgs/msg/joint_state.hpp>
#include <control_msgs/msg/gripper_command.hpp>
#include <arm_interface/action/arm_execute.hpp>

#include <yaml-cpp/yaml.h>

#include <spdlog/spdlog.h>
#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/sinks/rotating_file_sink.h>
#include <spdlog/sinks/dist_sink.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdarg>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <signal.h>
#include <string>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <unordered_map>
#include <vector>
#include <unistd.h>

using ArmExecute   = arm_interface::action::ArmExecute;
using GoalHandle   = rclcpp_action::ServerGoalHandle<ArmExecute>;
using namespace std::chrono_literals;

// ---- spdlog 日志集成 ----

static std::shared_ptr<spdlog::logger> g_logger;

static spdlog::level::level_enum severityToSpdlog(int severity)
{
  switch (severity) {
    case RCUTILS_LOG_SEVERITY_DEBUG: return spdlog::level::debug;
    case RCUTILS_LOG_SEVERITY_INFO:  return spdlog::level::info;
    case RCUTILS_LOG_SEVERITY_WARN:  return spdlog::level::warn;
    case RCUTILS_LOG_SEVERITY_ERROR: return spdlog::level::err;
    case RCUTILS_LOG_SEVERITY_FATAL: return spdlog::level::critical;
    default: return spdlog::level::info;
  }
}

static void spdlogOutputHandler(
  const rcutils_log_location_t * /*location*/,
  int severity, const char * name,
  rcutils_time_point_value_t /*timestamp*/,
  const char * format, va_list * args)
{
  if (!g_logger) return;
  char buf[4096];
  va_list args_copy;
  va_copy(args_copy, *args);
  vsnprintf(buf, sizeof(buf), format, args_copy);
  va_end(args_copy);
  std::string logger_name = name ? name : "unknown";
  g_logger->log(severityToSpdlog(severity), "[{}] {}", logger_name, buf);
}

static bool mkdirRecursive(const std::string & path)
{
  if (path.empty()) return false;
  struct stat st;
  if (stat(path.c_str(), &st) == 0) return S_ISDIR(st.st_mode);
  size_t pos = path.rfind('/');
  if (pos != std::string::npos && pos > 0) {
    if (!mkdirRecursive(path.substr(0, pos))) return false;
  }
  return mkdir(path.c_str(), 0755) == 0 || errno == EEXIST;
}

// Topic 重映射（与 ros2_bag_tool.py 保持一致）
// 录制时的原始 topic -> 回放时发布到的 topic
static const std::unordered_map<std::string, std::string> TOPIC_REMAP = {
  {"/hal/joint_states",               "/motion_ctl/joint_ctl"},
  {"/motion_ctl/gripper/left/state",   "/motion_ctl/gripper/left"},
  {"/motion_ctl/gripper/right/state",  "/motion_ctl/gripper/right"},
};

// 臂关节（14个）—— 从 hello.mcap 首帧提取
static const std::vector<std::string> ARM_JOINT_NAMES = {
  "arm_l_j1", "arm_l_j2", "arm_l_j3", "arm_l_j4", "arm_l_j5", "arm_l_j6", "arm_l_j7",
  "arm_r_j1", "arm_r_j2", "arm_r_j3", "arm_r_j4", "arm_r_j5", "arm_r_j6", "arm_r_j7",
};

static const std::vector<double> ARM_STANDBY_POSITIONS = {
  109.562, -89.832, -90.096, -110.238, -0.169, -0.037, 0.105,
  -111.875, -90.407, 90.615, -107.481, 5.216, -3.846, 4.330
};

// 头关节（3个）
static const std::vector<std::string> HEAD_JOINT_NAMES = {
  "head_j1", "head_j2", "head_j3"
};

static const std::vector<double> HEAD_STANDBY_POSITIONS = {
  0.0, 0.0, 0.0
};

// 归位/回放发布的完整关节列表（臂 + 头）
static const std::vector<std::string> STANDBY_JOINT_NAMES = []() {
  std::vector<std::string> v = ARM_JOINT_NAMES;
  v.insert(v.end(), HEAD_JOINT_NAMES.begin(), HEAD_JOINT_NAMES.end());
  return v;
}();

static const std::vector<double> STANDBY_POSITIONS = []() {
  std::vector<double> v = ARM_STANDBY_POSITIONS;
  v.insert(v.end(), HEAD_STANDBY_POSITIONS.begin(), HEAD_STANDBY_POSITIONS.end());
  return v;
}();

struct ActionMapping {
  std::string type;       // "standby" / "bag_replay" / "hold_pose"
  std::string bag_path;   // bag 目录路径（仅 bag_replay 需要）
  std::string head_path;  // 头部 bag 目录路径（仅 bag_replay 需要）
  std::string pose_name;  // 姿态名称（仅 hold_pose 需要）
  int default_height = 0; // 动作默认升降档位（0=不升降）
};

// 预定义姿态配置：关节位置 + 插值时长 + 升降档位
struct PoseConfig {
  std::vector<double> positions;  // 17 个关节角度（臂14 + 头3）
  double duration = 3.0;          // 插值时长（秒），默认 3s
  int body = 0;                   // 升降档位（0=不升降），优先级最高
};

// 升降档位定义：目标关节角度 + 参考时长
struct LiftLevel {
  std::string ends;      // 目标角度，如 "0,0,0"
  double duration;       // 参考时长（腰部由 Ruckig 自动规划）
};

class ArmReplayNode : public rclcpp::Node {
public:
  ArmReplayNode(const std::string & config_path)
  : Node("arm_replay_node")
  {
    // 1. 加载配置
    loadConfig(config_path);

    // 2. 创建 publisher（重映射后的 topic）
    joint_pub_ = create_publisher<sensor_msgs::msg::JointState>(
      "/motion_ctl/joint_ctl", 10);
    left_grip_pub_ = create_publisher<control_msgs::msg::GripperCommand>(
      "/motion_ctl/gripper/left", 10);
    right_grip_pub_ = create_publisher<control_msgs::msg::GripperCommand>(
      "/motion_ctl/gripper/right", 10);

    // 2.5 订阅当前关节状态（用于 standby 插值的起始位置）
    joint_state_sub_ = create_subscription<sensor_msgs::msg::JointState>(
      "/hal/joint_states", 10,
      [this](const sensor_msgs::msg::JointState::SharedPtr msg) {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        latest_joint_state_ = *msg;
        has_joint_state_ = true;
      });

    // 2.6 阻塞等待 /hal/joint_states 上报变化的关节数据（确认硬件在线）
    // 两阶段：① 首次检测到值变化 → ② 再等 5s 确认稳定
    {
      RCLCPP_INFO(get_logger(), "等待 /hal/joint_states 就绪...");
      auto start = std::chrono::steady_clock::now();
      std::vector<double> baseline;
      bool data_changed = false;
      auto changed_time = std::chrono::steady_clock::time_point();
      int last_log_sec = -1;

      while (rclcpp::ok()) {
        {
          std::lock_guard<std::mutex> lock(joint_state_mutex_);
          if (has_joint_state_ && latest_joint_state_.position.size() >= ARM_JOINT_NAMES.size()) {
            if (baseline.empty()) {
              // 阶段一：保存基线
              baseline = latest_joint_state_.position;
              RCLCPP_INFO(get_logger(), "/hal/joint_states 收到首帧数据，等待值变化...");
            } else if (!data_changed) {
              // 检测是否与基线不同
              for (size_t i = 0; i < ARM_JOINT_NAMES.size(); ++i) {
                if (latest_joint_state_.position[i] != baseline[i]) {
                  data_changed = true;
                  changed_time = std::chrono::steady_clock::now();
                  RCLCPP_INFO(get_logger(),
                    "检测到 /hal/joint_states 数据变化 (关节[%zu]: %.6f → %.6f)，等待 5 秒确认稳定...",
                    i, baseline[i], latest_joint_state_.position[i]);
                  break;
                }
              }
            }
          }
        }

        // 阶段二：检测到变化后，等待 5 秒
        if (data_changed) {
          auto waited = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - changed_time).count();
          if (waited >= 5) break;
        }

        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
          std::chrono::steady_clock::now() - start).count();
        if (static_cast<int>(elapsed / 5) > last_log_sec) {
          last_log_sec = static_cast<int>(elapsed / 5);
          if (data_changed) {
            auto remaining = 5 - std::chrono::duration_cast<std::chrono::seconds>(
              std::chrono::steady_clock::now() - changed_time).count();
            RCLCPP_INFO(get_logger(), "⏳ 已检测到数据变化，等待稳定 (剩余 %ld 秒)...",
              remaining > 0 ? remaining : 0);
          } else if (baseline.empty()) {
            RCLCPP_INFO(get_logger(), "⏳ 等待 /hal/joint_states 首次数据 (已等 %ld 秒)...", elapsed);
          } else {
            RCLCPP_INFO(get_logger(), "⏳ 等待 /hal/joint_states 数据变化 (已等 %ld 秒)...", elapsed);
          }
        }

        rclcpp::spin_some(this->get_node_base_interface());
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
      }
      RCLCPP_INFO(get_logger(), "✅ /hal/joint_states 数据就绪");
    }

    // 3. 创建 ArmExecute action server
    action_server_ = rclcpp_action::create_server<ArmExecute>(
      this, "arm/execute",
      std::bind(&ArmReplayNode::handleGoal, this,
                std::placeholders::_1, std::placeholders::_2),
      std::bind(&ArmReplayNode::handleCancel, this, std::placeholders::_1),
      std::bind(&ArmReplayNode::handleAccepted, this, std::placeholders::_1));

    RCLCPP_INFO(get_logger(), "机械臂回放 Action Server 已启动 (arm/execute)");
    RCLCPP_INFO(get_logger(), "已加载 %zu 个动作映射", action_map_.size());
  }

private:
  // ---- Action Server 回调 ----

  rclcpp_action::GoalResponse handleGoal(
    const rclcpp_action::GoalUUID & /*uuid*/,
    std::shared_ptr<const ArmExecute::Goal> goal)
  {
    auto now = std::chrono::steady_clock::now();
    double interval_ms = 0.0;
    if (last_request_time_.time_since_epoch().count() > 0) {
      interval_ms = std::chrono::duration<double, std::milli>(now - last_request_time_).count();
    }
    last_request_time_ = now;
    RCLCPP_INFO(get_logger(), "收到动作请求: action_id=%u (距上次 %.0fms)",
      goal->action_id, interval_ms);
    // 始终接受请求，排队顺序执行
    return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }

  rclcpp_action::CancelResponse handleCancel(
    std::shared_ptr<GoalHandle> /*goal_handle*/)
  {
    RCLCPP_INFO(get_logger(), "收到取消请求");
    return rclcpp_action::CancelResponse::ACCEPT;
  }

  void handleAccepted(std::shared_ptr<GoalHandle> goal_handle)
  {
    uint32_t action_id = goal_handle->get_goal()->action_id;
    {
      std::lock_guard<std::mutex> lock(queue_mutex_);
      goal_queue_.push_back(goal_handle);
      if (worker_running_) {
        // 工作线程已在运行，新 goal 排队等待
        RCLCPP_INFO(get_logger(), "动作 action_id=%u 排队等待 (队列长度=%zu)",
          action_id, goal_queue_.size());
        return;
      }
      worker_running_ = true;
    }
    RCLCPP_INFO(get_logger(), "动作 action_id=%u 立即执行", action_id);
    // 启动工作线程处理队列
    std::thread{std::bind(&ArmReplayNode::workerLoop, this)}.detach();
  }

  // 工作线程：顺序执行队列中的 goal
  void workerLoop()
  {
    while (true) {
      std::shared_ptr<GoalHandle> goal_handle;
      {
        std::lock_guard<std::mutex> lock(queue_mutex_);
        if (goal_queue_.empty()) {
          worker_running_ = false;
          return;
        }
        goal_handle = goal_queue_.front();
        goal_queue_.pop_front();
      }
      execute(goal_handle);
      // 执行间休息：让系统稳定，避免连续动作之间的指令冲突
      if (action_rest_ms_ > 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(action_rest_ms_));
      }
    }
  }

  // ---- 配置加载 ----

  void loadConfig(const std::string & config_path)
  {
    // 保存配置文件所在目录，用于解析 bag_path 的相对路径
    config_dir_ = std::filesystem::path(config_path).parent_path().string();

    try {
      YAML::Node root = YAML::LoadFile(config_path);
      // ---- 预定义姿态（hold_pose 使用）----
      if (root["poses"] && root["poses"].IsMap()) {
        for (const auto & kv : root["poses"]) {
          std::string pose_name = kv.first.as<std::string>();
          std::vector<double> positions;
          if (kv.second["arm"] && kv.second["arm"].IsSequence()) {
            for (const auto & v : kv.second["arm"])
              positions.push_back(v.as<double>());
          }
          if (kv.second["head"] && kv.second["head"].IsSequence()) {
            for (const auto & v : kv.second["head"])
              positions.push_back(v.as<double>());
          }
          if (positions.size() == STANDBY_JOINT_NAMES.size()) {
            PoseConfig pc;
            pc.positions = positions;
            if (kv.second["duration"])
              pc.duration = kv.second["duration"].as<double>();
            if (kv.second["body"])
              pc.body = kv.second["body"].as<int>();
            named_poses_[pose_name] = pc;
            RCLCPP_INFO(get_logger(), "  姿态 '%s': %zu 关节, duration=%.1fs, body=%d",
              pose_name.c_str(), positions.size(), pc.duration, pc.body);
          } else {
            RCLCPP_WARN(get_logger(),
              "  姿态 '%s' 关节数 %zu 不匹配预期 %zu，已跳过",
              pose_name.c_str(), positions.size(), STANDBY_JOINT_NAMES.size());
          }
        }
      }

      if (!root["actions"] || !root["actions"].IsSequence()) {
        RCLCPP_ERROR(get_logger(), "配置文件缺少 actions 列表");
        return;
      }

      for (const auto & item : root["actions"]) {
        uint32_t id = item["id"].as<uint32_t>();
        ActionMapping mapping;
        mapping.type = item["type"].as<std::string>();
        if (item["bag_path"]) {
          mapping.bag_path = item["bag_path"].as<std::string>();
        }
        if (item["head_path"]) {
          mapping.head_path = item["head_path"].as<std::string>();
        }
        if (item["pose"]) {
          mapping.pose_name = item["pose"].as<std::string>();
        }
        if (item["default_height"]) {
          mapping.default_height = item["default_height"].as<int>();
        }
        action_map_[id] = mapping;
        RCLCPP_INFO(get_logger(), "  action_id=%u -> type=%s%s%s%s%s",
          id, mapping.type.c_str(),
          mapping.bag_path.empty() ? "" :
            (" bag=" + mapping.bag_path).c_str(),
          mapping.head_path.empty() ? "" :
            (" head=" + mapping.head_path).c_str(),
          mapping.pose_name.empty() ? "" :
            (" pose=" + mapping.pose_name).c_str(),
          mapping.default_height > 0 ?
            (" height=" + std::to_string(mapping.default_height)).c_str() : "");

        // 验证 bag_replay 类型的路径是否存在
        if (mapping.type == "bag_replay") {
          bool has_error = false;
          if (mapping.bag_path.empty()) {
            RCLCPP_ERROR(get_logger(),
              "  [错误] action_id=%u: bag_replay 缺少 bag_path", id);
            has_error = true;
          } else {
            std::string resolved = resolveBagPath(mapping.bag_path);
            std::string mcap = findMcapFile(resolved);
            if (mcap.empty()) {
              RCLCPP_ERROR(get_logger(),
                "  [错误] action_id=%u: bag_path 不存在: %s", id, resolved.c_str());
              has_error = true;
            }
          }
          if (mapping.head_path.empty()) {
            RCLCPP_ERROR(get_logger(),
              "  [错误] action_id=%u: bag_replay 缺少 head_path", id);
            has_error = true;
          } else {
            std::string resolved = resolveBagPath(mapping.head_path);
            std::string mcap = findMcapFile(resolved);
            if (mcap.empty()) {
              RCLCPP_ERROR(get_logger(),
                "  [错误] action_id=%u: head_path 不存在: %s", id, resolved.c_str());
              has_error = true;
            }
          }
          if (has_error) {
            action_map_.erase(id);
          }
        }

        // 验证 hold_pose 类型的姿态是否存在
        if (mapping.type == "hold_pose") {
          if (mapping.pose_name.empty()) {
            RCLCPP_ERROR(get_logger(),
              "  [错误] action_id=%u: hold_pose 缺少 pose 字段", id);
            action_map_.erase(id);
          } else if (named_poses_.find(mapping.pose_name) == named_poses_.end()) {
            RCLCPP_ERROR(get_logger(),
              "  [错误] action_id=%u: 姿态 '%s' 未在 poses 段中定义", id, mapping.pose_name.c_str());
            action_map_.erase(id);
          }
        }
      }

      // ---- 升降控制配置（可选）----
      if (root["lift"]) {
        const auto & lift = root["lift"];
        if (lift["script_path"]) {
          lift_script_path_ = resolveScriptPath(lift["script_path"].as<std::string>());
        }
        if (lift["motors"]) {
          lift_motors_ = lift["motors"].as<std::string>();
        }
        // 默认档位：归位后腰部停在此档位（0=默认位，不参与点位升降）
        if (lift["default_level"]) {
          default_level_ = lift["default_level"].as<int>();
        }
        if (lift["height_levels"] && lift["height_levels"].IsMap()) {
          for (const auto & kv : lift["height_levels"]) {
            uint32_t lvl = kv.first.as<uint32_t>();
            LiftLevel ll;
            ll.ends = kv.second["ends"] ? kv.second["ends"].as<std::string>() : "";
            ll.duration = kv.second["duration"] ?
              kv.second["duration"].as<double>() : 5.0;
            lift_levels_[lvl] = ll;
            RCLCPP_INFO(get_logger(),
              "  升降档位 %u -> ends=%s duration=%.1fs",
              lvl, ll.ends.c_str(), ll.duration);
          }
        }
        if (lift_levels_.find(default_level_) == lift_levels_.end()) {
          RCLCPP_WARN(get_logger(),
            "  升降默认档位 %d 未在 height_levels 中定义，归位将无法降回默认位置",
            default_level_);
        }
        RCLCPP_INFO(get_logger(), "升降脚本: %s (电机 %s)",
          lift_script_path_.empty() ? "(未配置)" : lift_script_path_.c_str(),
          lift_motors_.c_str());
      }
    } catch (const std::exception & e) {
      RCLCPP_ERROR(get_logger(), "加载配置文件失败: %s", e.what());
    }
  }

  // ---- bag_path 解析 ----
  // 绝对路径直接使用；名称则在 share/arm_replay/bags/ 下查找

  // 解析升降脚本路径：绝对路径原样返回；相对路径（默认配置为
  // "smooth_interpolate_ros_rg.py"，脚本位于 workspace 的 src 目录下）按优先级查找：
  //   1. 配置文件目录向上两级（src/arm_replay/config -> src）
  //   2. 配置文件目录上一级（src/arm_replay 包目录）
  //   3. 配置文件同目录
  //   4. 可执行文件向上 4 级（colcon 布局 workspace 根）的 src 目录
  std::string resolveScriptPath(const std::string & script_path) const
  {
    if (script_path.empty()) return script_path;
    std::filesystem::path p(script_path);
    if (p.is_absolute()) return script_path;

    std::vector<std::string> candidates;
    if (!config_dir_.empty()) {
      // src/arm_replay/config/ -> src/
      candidates.push_back((std::filesystem::path(config_dir_) / "../../" / script_path).string());
      // src/arm_replay/
      candidates.push_back((std::filesystem::path(config_dir_) / "../" / script_path).string());
      // 配置文件同目录
      candidates.push_back((std::filesystem::path(config_dir_) / script_path).string());
    }
    // colcon 布局：install/<pkg>/lib/<pkg>/<exe> 向上 5 级为 workspace 根
    {
      std::error_code ec;
      auto exe = std::filesystem::read_symlink("/proc/self/exe", ec);
      if (!ec) {
        std::filesystem::path root =
          exe.parent_path().parent_path().parent_path()
             .parent_path().parent_path();
        candidates.push_back((root / "src" / script_path).string());
      }
    }

    for (const auto & c : candidates) {
      if (std::filesystem::exists(c)) {
        RCLCPP_INFO(get_logger(), "升降脚本相对路径已解析: %s -> %s",
          script_path.c_str(), c.c_str());
        return c;
      }
    }
    return script_path;  // 未找到则原样返回，由 runLiftScript 报明确错误
  }

  std::string resolveBagPath(const std::string & bag_path) const
  {
    if (bag_path.empty()) return bag_path;

    std::filesystem::path p(bag_path);
    if (p.is_absolute()) {
      return bag_path;
    }
    const char * ament_prefix = getenv("AMENT_PREFIX_PATH");
    if (!ament_prefix) {
      RCLCPP_WARN(get_logger(),
        "AMENT_PREFIX_PATH 未设置，无法解析 bag 名称: %s", bag_path.c_str());
      return bag_path;
    }

    std::string prefix(ament_prefix);
    const std::string bags_rel = "/share/arm_replay/bags/";

    // 遍历所有 prefix 路径
    std::string search = prefix;
    while (!search.empty()) {
      size_t colon_pos = search.find(':');
      std::string pfx = (colon_pos != std::string::npos)
                         ? search.substr(0, colon_pos) : search;
      std::string bags_dir = pfx + bags_rel;

      // 尝试 <bags_dir>/<name>.mcap
      std::string mcap_file = bags_dir + bag_path + ".mcap";
      if (std::filesystem::exists(mcap_file)) {
        RCLCPP_INFO(get_logger(), "bag 名称已解析: %s -> %s",
          bag_path.c_str(), mcap_file.c_str());
        return mcap_file;
      }

      // 尝试 <bags_dir>/<name>/ 目录
      std::string bag_dir = bags_dir + bag_path;
      if (std::filesystem::is_directory(bag_dir)) {
        RCLCPP_INFO(get_logger(), "bag 名称已解析: %s -> %s",
          bag_path.c_str(), bag_dir.c_str());
        return bag_dir;
      }

      if (colon_pos == std::string::npos) break;
      search = search.substr(colon_pos + 1);
    }

    RCLCPP_WARN(get_logger(),
      "bag 名称 '%s' 在 bags/ 目录下未找到", bag_path.c_str());
    return bag_path;
  }

  // ---- 查找 mcap 文件 ----

  std::string findMcapFile(const std::string & bag_path) const
  {
    namespace fs = std::filesystem;

    // 如果路径本身是 .mcap 文件
    if (fs::is_regular_file(bag_path) && bag_path.size() > 5 &&
        bag_path.substr(bag_path.size() - 5) == ".mcap")
    {
      return bag_path;
    }

    // 在目录中查找 .mcap 文件
    if (fs::is_directory(bag_path)) {
      for (const auto & entry : fs::directory_iterator(bag_path)) {
        if (entry.path().extension() == ".mcap") {
          return entry.path().string();
        }
      }
    }
    return "";
  }

  // ---- 反序列化 ----

  template<typename MsgT>
  MsgT deserialize(std::shared_ptr<rcutils_uint8_array_t> data)
  {
    rclcpp::SerializedMessage serialized_msg(*data);
    MsgT msg;
    rclcpp::Serialization<MsgT>().deserialize_message(&serialized_msg, &msg);
    return msg;
  }

  // ---- 执行逻辑 ----

  // ---- 升降控制（外部脚本 smooth_interpolate_ros_rg.py）----
  // 脚本每次从 /hal/joint_states 当前角度平滑插值到目标，可安全中断后重跑。
  // 阻塞等待完成；返回 true=执行完成，false=被取消/失败。
  bool runLiftScript(std::shared_ptr<GoalHandle> goal_handle, uint32_t level)
  {
    // 升降互斥：腰部电机同一时刻只允许一个升降脚本控制。
    // 若语音模式升降未走完就切回顺序模式（standby 降回默认），两个脚本会
    // 同时下发腰部电机指令互相覆盖，导致升降过快停止/抖动。
    // 第二个升降请求阻塞等待第一个自然走完（约 5-6s）后再执行。
    // try_lock 检测互斥是否触发：锁被占用时打印等待日志，再阻塞等待
    if (!lift_mutex_.try_lock()) {
      RCLCPP_WARN(get_logger(),
        "升降互斥: 另一升降脚本执行中，等待其自然完成 (档位 %u，预计 5-6s)...",
        level);
      lift_mutex_.lock();
    }
    std::lock_guard<std::mutex> lock(lift_mutex_, std::adopt_lock);

    // 拿锁后重新判断：等待锁期间其他升降可能已完成并更新 last_lift_level_，
    // 已在该档位则跳过（避免并发场景下多余的重复升降——重复升降会以晃动的
    // 关节反馈为起点重新规划，反而制造抖动）
    if (static_cast<int>(level) == last_lift_level_) {
      RCLCPP_INFO(get_logger(), "升降跳过: 已在档位 %u", level);
      return true;
    }

    auto it = lift_levels_.find(level);
    if (it == lift_levels_.end()) {
      RCLCPP_WARN(get_logger(), "升降档位 %u 未配置，跳过升降", level);
      return true;
    }
    if (lift_script_path_.empty()) {
      RCLCPP_WARN(get_logger(), "未配置升降脚本路径，跳过升降");
      return true;
    }
    const auto & ll = it->second;

    // 执行前检查脚本文件是否存在：路径错误时 python3 会以 exit=2 退出
    // （can't open file），此处提前报出明确原因，避免误导排查。
    if (access(lift_script_path_.c_str(), F_OK) != 0) {
      RCLCPP_ERROR(get_logger(),
        "升降脚本不存在: %s（python3 将报 exit=2），请检查 arm_replay_config.yaml "
        "中 lift.script_path 是否为部署机上的实际路径",
        lift_script_path_.c_str());
      return false;
    }

    RCLCPP_INFO(get_logger(), "执行升降: 档位 %u -> ends=%s duration=%.1fs (电机 %s)",
      level, ll.ends.c_str(), ll.duration, lift_motors_.c_str());

    // 构建完整命令字符串（用于日志，方便手动复现）
    std::string cmd_str = std::string("python3 ") + lift_script_path_ +
      " --motors " + lift_motors_ +
      " --ends=" + ll.ends +
      " --duration " + std::to_string(ll.duration) +
      " --no-confirm";
    RCLCPP_INFO(get_logger(), "升降命令: %s", cmd_str.c_str());

    pid_t pid = fork();
    if (pid < 0) {
      RCLCPP_ERROR(get_logger(), "fork 失败，无法执行升降脚本");
      return false;
    }
    if (pid == 0) {
      // 子进程：输出重定向到日志文件
      std::string log_file = std::string(getenv("HOME") ? getenv("HOME") : ".") +
                             "/logs/guide_robot/lift.log";
      int fd = open(log_file.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
      if (fd >= 0) {
        // 写入时间戳分隔线，区分多次执行
        time_t now = time(nullptr);
        char time_buf[64];
        strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", localtime(&now));
        std::string sep = std::string("\n========== ") + time_buf +
          " | 档位 " + std::to_string(level) +
          " | ends=" + ll.ends +
          " | motors=" + lift_motors_ + " ==========\n";
        ssize_t ws = write(fd, sep.c_str(), sep.size());
        (void)ws;
        dup2(fd, STDOUT_FILENO);
        dup2(fd, STDERR_FILENO);
        close(fd);
      }
      std::string motors_arg = lift_motors_;
      std::string ends_arg   = ll.ends;
      std::string dur_arg    = std::to_string(ll.duration);
      // --ends 的值以 '-' 开头（负角度），必须合并为单个 token（--ends=-5,-10,-5），
      // 否则 argparse 会把负数值误判为选项（expected one argument）。
      std::string ends_full  = "--ends=" + ends_arg;
      char * const argv[] = {
        const_cast<char *>("python3"),
        const_cast<char *>(lift_script_path_.c_str()),
        const_cast<char *>("--motors"), const_cast<char *>(motors_arg.c_str()),
        const_cast<char *>(ends_full.c_str()),
        const_cast<char *>("--duration"), const_cast<char *>(dur_arg.c_str()),
        const_cast<char *>("--no-confirm"),
        nullptr
      };
      execvp("python3", argv);
      // 执行到这里说明 execvp 失败（如 python3 不在 PATH），写日志供 lift.log 排查
      {
        char err_buf[256];
        snprintf(err_buf, sizeof(err_buf), "[execvp失败] python3: %s\n", strerror(errno));
        ssize_t w = write(STDERR_FILENO, err_buf, strlen(err_buf));
        (void)w;
      }
      _exit(127);
    }

    // 父进程：等待子进程退出。
    // 若收到取消请求（如模式切换打断），不 kill 升降脚本，等待其自然走完
    // （约 5-6s），由调用方（execute/executeStandby）在返回后统一处理取消，
    // 避免腰部电机停在半路。
    int status = 0;
    bool cancel_notified = false;
    auto wait_start = std::chrono::steady_clock::now();
    while (true) {
      if (goal_handle->is_canceling() && !cancel_notified) {
        cancel_notified = true;
        RCLCPP_WARN(get_logger(), "升降执行中收到取消请求，等待升降走完 (档位 %u)", level);
      }
      pid_t ret = waitpid(pid, &status, WNOHANG);
      if (ret == pid) break;
      if (ret < 0 && errno != EINTR) {
        RCLCPP_ERROR(get_logger(), "waitpid 失败: %s", strerror(errno));
        return false;
      }
      // 兜底超时：防止脚本异常挂死导致无限等待（正常约 5-6s）
      auto waited = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::steady_clock::now() - wait_start).count();
      if (waited > 30) {
        RCLCPP_WARN(get_logger(), "升降脚本执行超时，强制结束 (档位 %u)", level);
        kill(pid, SIGTERM);
        waitpid(pid, &status, 0);
        return false;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
      auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - wait_start).count();
      RCLCPP_INFO(get_logger(), "升降完成: 档位 %u, 耗时 %.1fs",
        level, elapsed / 1000.0);
      return true;
    }
    int exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - wait_start).count();
    RCLCPP_WARN(get_logger(), "升降脚本异常退出 (档位 %u, exit=%d, 耗时 %.1fs)",
      level, exit_code, elapsed / 1000.0);
    // 输出脚本日志尾部便于定位，常见原因：
    //   exit=2: 脚本路径不存在 / 参数不被脚本识别（argparse 错误）
    //   exit=1: 脚本内部异常（如未收到 /hal/joint_states、ruckig 异常）
    {
      std::string log_file = std::string(getenv("HOME") ? getenv("HOME") : ".") +
                             "/logs/guide_robot/lift.log";
      std::ifstream lf(log_file);
      if (lf.is_open()) {
        std::vector<std::string> lines;
        std::string line;
        while (std::getline(lf, line)) {
          lines.push_back(line);
          if (lines.size() > 8) lines.erase(lines.begin());
        }
        for (const auto & l : lines) {
          RCLCPP_WARN(get_logger(), "  [lift.log] %s", l.c_str());
        }
      }
    }
    return false;
  }

  void execute(std::shared_ptr<GoalHandle> goal_handle)
  {
    uint32_t action_id = goal_handle->get_goal()->action_id;
    uint32_t height_level = goal_handle->get_goal()->height_level;
    auto result = std::make_shared<ArmExecute::Result>();

    // 查找动作映射
    auto it = action_map_.find(action_id);
    if (it == action_map_.end()) {
      result->success = false;
      result->message = "未找到 action_id=" + std::to_string(action_id) + " 的映射";
      goal_handle->abort(result);
      RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
      return;
    }

    const auto & mapping = it->second;

    if (mapping.type == "standby") {
      executeStandby(goal_handle);
      return;
    }

    // 解析有效升降高度：pose.body > action.default_height > goal height_level
    int effective_height = static_cast<int>(height_level);
    if (mapping.default_height > 0) {
      effective_height = mapping.default_height;
    }

    // hold_pose: 姿态 body 字段优先级最高（提前查找姿态）
    const PoseConfig * pose_ptr = nullptr;
    if (mapping.type == "hold_pose") {
      auto pose_it = named_poses_.find(mapping.pose_name);
      if (pose_it == named_poses_.end()) {
        result->success = false;
        result->message = "姿态 '" + mapping.pose_name + "' 未定义";
        goal_handle->abort(result);
        RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
        return;
      }
      pose_ptr = &pose_it->second;
      if (pose_ptr->body > 0) {
        effective_height = pose_ptr->body;
      }
    }

    if (effective_height > 0 && effective_height != mapping.default_height &&
        mapping.default_height > 0) {
      // pose.body 覆盖了 action default_height，记录日志
      RCLCPP_INFO(get_logger(), "升降高度: pose.body=%d 覆盖 default_height=%d",
        effective_height, mapping.default_height);
    }

    if (mapping.type == "bag_replay") {
      // 升降：仅首次或档位变化时执行（动作循环重复执行时不重复升降）
      if (effective_height > 0 && effective_height != last_lift_level_) {
        if (runLiftScript(goal_handle, effective_height)) {
          last_lift_level_ = effective_height;
        } else {
          // 升降失败：档位状态未知，下次重新升降
          last_lift_level_ = -1;
        }
        // 升降期间收到取消：等待升降自然走完后，按取消处理（不再回放动作）
        if (goal_handle->is_canceling()) {
          auto cancel_result = std::make_shared<ArmExecute::Result>();
          cancel_result->success = false;
          cancel_result->message = "升降阶段动作被取消";
          goal_handle->canceled(cancel_result);
          RCLCPP_INFO(get_logger(), "升降阶段取消，动作未执行");
          return;
        }
      }

      std::string resolved_arm  = resolveBagPath(mapping.bag_path);
      std::string resolved_head = resolveBagPath(mapping.head_path);
      executeBagReplay(goal_handle, resolved_arm, resolved_head);
      return;
    }

    if (mapping.type == "hold_pose") {
      // 升降：仅首次或档位变化时执行
      if (effective_height > 0 && effective_height != last_lift_level_) {
        if (runLiftScript(goal_handle, effective_height)) {
          last_lift_level_ = effective_height;
        } else {
          last_lift_level_ = -1;
        }
        if (goal_handle->is_canceling()) {
          auto cancel_result = std::make_shared<ArmExecute::Result>();
          cancel_result->success = false;
          cancel_result->message = "升降阶段动作被取消";
          goal_handle->canceled(cancel_result);
          RCLCPP_INFO(get_logger(), "升降阶段取消，动作未执行");
          return;
        }
      }

      executeHoldPose(goal_handle, pose_ptr->positions, pose_ptr->duration);
      return;
    }

    result->success = false;
    result->message = "未知的动作类型: " + mapping.type;
    goal_handle->abort(result);
    RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
  }

  void executeStandby(std::shared_ptr<GoalHandle> goal_handle)
  {
    RCLCPP_INFO(get_logger(), "执行待机/归位动作 (线性插值)");

    // 插值参数
    const double duration = 3.0;       // 总时长（秒）
    const double rate_hz = 30.0;       // 发布频率
    const int total_steps = static_cast<int>(duration * rate_hz);
    const auto step_period = std::chrono::microseconds(
      static_cast<int64_t>(1e6 / rate_hz));

    // 1. 获取插值起始点：优先使用 bag 回放最后发布的命令位置（与 bag 命令
    //    无缝衔接，避免命令跳变导致关节抖动）；若无则回退到关节反馈位置。
    //    仅当缓存是 3 秒内发布的才使用（与回放 blend 过滤一致），
    //    避免跨点位的过期缓存导致归位瞬间跳变（"抽一下"）。
    std::vector<double> start_positions(STANDBY_JOINT_NAMES.size(), 0.0);
    {
      sensor_msgs::msg::JointState last_cmd;
      bool have_last = false;
      {
        std::lock_guard<std::mutex> lock(last_cmd_mutex_);
        auto now_ = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration<double>(now_ - last_cmd_time_).count();
        if (has_last_published_ && elapsed < 3.0) {
          last_cmd = last_published_cmd_;
          have_last = true;
        }
      }

      if (have_last && last_cmd.name.size() == STANDBY_JOINT_NAMES.size() &&
          last_cmd.position.size() == STANDBY_JOINT_NAMES.size()) {
        // last_published_cmd_ 使用 STANDBY_JOINT_NAMES 顺序，直接复制
        for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
          start_positions[i] = last_cmd.position[i];
        }
        RCLCPP_INFO(get_logger(),
          "使用最后发布的关节指令作为插值起点（无缝衔接，避免抖动）");
      } else {
        // 回退：从 /hal/joint_states 读取当前反馈位置
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        if (has_joint_state_) {
          for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
            for (size_t j = 0; j < latest_joint_state_.name.size(); ++j) {
              if (latest_joint_state_.name[j] == STANDBY_JOINT_NAMES[i] &&
                  j < latest_joint_state_.position.size()) {
                start_positions[i] = latest_joint_state_.position[j];
                break;
              }
            }
          }
          RCLCPP_INFO(get_logger(), "使用当前关节反馈作为插值起点（无历史指令）");
        } else {
          RCLCPP_WARN(get_logger(), "未收到关节状态，使用全零作为插值起点");
        }
      }
    }

    // 打印臂关节起止位置
    RCLCPP_INFO(get_logger(), "归位插值: steps=%d, %zu arm + %zu head joints:",
      total_steps, ARM_JOINT_NAMES.size(), HEAD_JOINT_NAMES.size());
    for (size_t i = 0; i < ARM_JOINT_NAMES.size(); ++i) {
      RCLCPP_INFO(get_logger(),
        "  [%2zu] %-20s: %8.2f -> %8.2f",
        i, ARM_JOINT_NAMES[i].c_str(),
        start_positions[i], ARM_STANDBY_POSITIONS[i]);
    }

    // 构造发布消息
    sensor_msgs::msg::JointState cmd;
    cmd.name = STANDBY_JOINT_NAMES;
    cmd.position.resize(STANDBY_JOINT_NAMES.size());

    auto start_wall = std::chrono::steady_clock::now();
    for (int step = 1; step <= total_steps; ++step) {
      // 检查取消
      if (goal_handle->is_canceling()) {
        auto result = std::make_shared<ArmExecute::Result>();
        result->success = false;
        result->message = "待机动作被取消";
        goal_handle->canceled(result);
        RCLCPP_INFO(get_logger(), "待机插值被取消 (step %d/%d)", step, total_steps);
        return;
      }

      // 计算插值比例 t ∈ [0, 1]
      double t = static_cast<double>(step) / static_cast<double>(total_steps);

      // 线性插值
      for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
        cmd.position[i] = start_positions[i] + t * (STANDBY_POSITIONS[i] - start_positions[i]);
      }
      cmd.header.stamp = now();
      joint_pub_->publish(cmd);

      // 等待下一个周期
      auto expected_time = start_wall + std::chrono::microseconds(
        static_cast<int64_t>(step * 1e6 / rate_hz));
      auto sleep_until = expected_time - std::chrono::steady_clock::now();
      if (sleep_until.count() > 0) {
        std::this_thread::sleep_for(
          std::chrono::duration_cast<std::chrono::microseconds>(sleep_until));
      }
    }

    // 3. 最终精确发布目标位置
    cmd.position = STANDBY_POSITIONS;
    cmd.header.stamp = now();
    joint_pub_->publish(cmd);

    // 4. 降回默认高度（default_level_ 档位）：腰部回默认位，确保下一点位/下次升降从默认开始。
    //    与归位插值串行执行；升降等待期间收到取消时按取消返回。
    if (last_lift_level_ != default_level_) {
      if (runLiftScript(goal_handle, default_level_)) {
        last_lift_level_ = default_level_;
      } else {
        last_lift_level_ = -1;  // 降回默认失败：状态未知
      }
      if (goal_handle->is_canceling()) {
        auto result = std::make_shared<ArmExecute::Result>();
        result->success = false;
        result->message = "待机动作被取消（降回默认高度中）";
        goal_handle->canceled(result);
        RCLCPP_INFO(get_logger(), "待机降回默认高度被取消");
        return;
      }
    }

    auto result = std::make_shared<ArmExecute::Result>();
    result->success = true;
    result->message = "待机动作完成";
    goal_handle->succeed(result);
    RCLCPP_INFO(get_logger(), "待机/归位动作完成 (%d 步插值)", total_steps);
  }

  void executeBagReplay(
    std::shared_ptr<GoalHandle> goal_handle,
    const std::string & arm_path,
    const std::string & head_path)
  {
    RCLCPP_INFO(get_logger(), "开始回放 arm: %s, head: %s",
      arm_path.c_str(), head_path.c_str());

    // 1. 查找 mcap 文件
    std::string arm_mcap  = findMcapFile(arm_path);
    std::string head_mcap = findMcapFile(head_path);
    if (arm_mcap.empty()) {
      auto result = std::make_shared<ArmExecute::Result>();
      result->success = false;
      result->message = "在 " + arm_path + " 中未找到 arm .mcap 文件";
      goal_handle->abort(result);
      RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
      return;
    }
    if (head_mcap.empty()) {
      auto result = std::make_shared<ArmExecute::Result>();
      result->success = false;
      result->message = "在 " + head_path + " 中未找到 head .mcap 文件";
      goal_handle->abort(result);
      RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
      return;
    }
    RCLCPP_INFO(get_logger(), "arm  mcap: %s", arm_mcap.c_str());
    RCLCPP_INFO(get_logger(), "head mcap: %s", head_mcap.c_str());

    // 2. 读取 arm mcap 全部消息
    struct BagMessage {
      int64_t timestamp;
      std::string topic;
      std::shared_ptr<rcutils_uint8_array_t> data;
    };

    std::vector<BagMessage> arm_messages;
    {
      rosbag2_cpp::Reader reader;
      try {
        rosbag2_storage::StorageOptions storage_opts;
        storage_opts.uri = arm_mcap;
        storage_opts.storage_id = "mcap";
        rosbag2_cpp::ConverterOptions converter_opts;
        converter_opts.input_serialization_format = "cdr";
        converter_opts.output_serialization_format = "cdr";
        reader.open(storage_opts, converter_opts);
      } catch (const std::exception & e) {
        auto result = std::make_shared<ArmExecute::Result>();
        result->success = false;
        result->message = "打开 arm mcap 失败: " + std::string(e.what());
        goal_handle->abort(result);
        RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
        return;
      }
      while (reader.has_next()) {
        auto msg = reader.read_next();
        if (TOPIC_REMAP.find(msg->topic_name) != TOPIC_REMAP.end()) {
          BagMessage bm;
#ifdef HAS_SEND_TIMESTAMP
          bm.timestamp = msg->send_timestamp;
#else
          bm.timestamp = msg->time_stamp;
#endif
          bm.topic = msg->topic_name;
          bm.data = msg->serialized_data;
          arm_messages.push_back(std::move(bm));
        }
      }
    }

    if (arm_messages.empty()) {
      auto result = std::make_shared<ArmExecute::Result>();
      result->success = false;
      result->message = "arm mcap 中没有需要回放的消息";
      goal_handle->abort(result);
      RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
      return;
    }

    // 3. 读取 head mcap，仅提取 head 关节帧
    struct HeadFrame {
      double rel_time;  // 相对时间（秒）
      std::array<double, 3> positions;
    };
    std::vector<HeadFrame> head_frames;
    {
      rosbag2_cpp::Reader reader;
      try {
        rosbag2_storage::StorageOptions storage_opts;
        storage_opts.uri = head_mcap;
        storage_opts.storage_id = "mcap";
        rosbag2_cpp::ConverterOptions converter_opts;
        converter_opts.input_serialization_format = "cdr";
        converter_opts.output_serialization_format = "cdr";
        reader.open(storage_opts, converter_opts);
      } catch (const std::exception & e) {
        auto result = std::make_shared<ArmExecute::Result>();
        result->success = false;
        result->message = "打开 head mcap 失败: " + std::string(e.what());
        goal_handle->abort(result);
        RCLCPP_ERROR(get_logger(), "%s", result->message.c_str());
        return;
      }
      int64_t head_first_ts = -1;
      while (reader.has_next()) {
        auto msg = reader.read_next();
        if (msg->topic_name == "/hal/joint_states") {
          auto js = deserialize<sensor_msgs::msg::JointState>(msg->serialized_data);
          HeadFrame hf;
          hf.positions.fill(0.0);
          for (size_t hi = 0; hi < HEAD_JOINT_NAMES.size(); ++hi) {
            for (size_t ji = 0; ji < js.name.size() && ji < js.position.size(); ++ji) {
              if (js.name[ji] == HEAD_JOINT_NAMES[hi]) {
                hf.positions[hi] = js.position[ji];
                break;
              }
            }
          }
#ifdef HAS_SEND_TIMESTAMP
          int64_t ts = msg->send_timestamp;
#else
          int64_t ts = msg->time_stamp;
#endif
          if (head_first_ts < 0) head_first_ts = ts;
          hf.rel_time = static_cast<double>(ts - head_first_ts) / 1e9;
          head_frames.push_back(hf);
        }
      }
    }

    RCLCPP_INFO(get_logger(), "arm=%zu条, head=%zu帧",
      arm_messages.size(), head_frames.size());

    // 4. arm 消息按时间戳排序
    std::sort(arm_messages.begin(), arm_messages.end(),
      [](const BagMessage & a, const BagMessage & b) {
        return a.timestamp < b.timestamp;
      });

    // 5. 计算 arm 相对时间
    int64_t arm_first_ts = arm_messages[0].timestamp;
    std::vector<double> arm_time_diffs;
    arm_time_diffs.reserve(arm_messages.size());
    for (const auto & m : arm_messages) {
      arm_time_diffs.push_back(
        static_cast<double>(m.timestamp - arm_first_ts) / 1e9);
    }

    // 6. 回放（以 arm 消息驱动）
    auto feedback = std::make_shared<ArmExecute::Feedback>();
    auto start_wall = std::chrono::steady_clock::now();
    size_t total = arm_messages.size();
    size_t head_idx = 0;

    // 缓存上次回放最后发布的关节位置，用于循环重播时平滑过渡，避免跳变
    // 仅当上次发布距今 < 3s 时才做 blend（过滤跨点位的过期缓存）
    std::vector<double> last_arm_pos(ARM_JOINT_NAMES.size(), 0.0);
    std::array<double, 3> last_head_pos = {0.0, 0.0, 0.0};
    bool have_last_arm = false;
    bool have_last_head = false;
    {
      std::lock_guard<std::mutex> lock(last_cmd_mutex_);
      auto now = std::chrono::steady_clock::now();
      auto elapsed = std::chrono::duration<double>(now - last_cmd_time_).count();
      if (has_last_published_ && elapsed < 3.0) {
        // 臂关节
        if (last_published_cmd_.position.size() >= ARM_JOINT_NAMES.size()) {
          for (size_t ai = 0; ai < ARM_JOINT_NAMES.size(); ++ai) {
            last_arm_pos[ai] = last_published_cmd_.position[ai];
          }
          have_last_arm = true;
        }
        // 头关节
        size_t h_off = ARM_JOINT_NAMES.size();
        if (last_published_cmd_.position.size() > h_off + 2) {
          for (size_t hi = 0; hi < HEAD_JOINT_NAMES.size(); ++hi) {
            last_head_pos[hi] = last_published_cmd_.position[h_off + hi];
          }
          have_last_head = true;
        }
      }
    }
    const size_t BLEND_FRAMES = 30;  // 前 30 帧（≈1秒）平滑过渡
    size_t js_count = 0;

    for (size_t i = 0; i < total; ++i) {
      if (goal_handle->is_canceling()) {
        auto result = std::make_shared<ArmExecute::Result>();
        result->success = false;
        result->message = "回放被取消 (已发布 " +
          std::to_string(i) + "/" + std::to_string(total) + ")";
        goal_handle->canceled(result);
        RCLCPP_INFO(get_logger(), "%s", result->message.c_str());
        return;
      }

      // 按时间间隔等待
      double expected_elapsed = arm_time_diffs[i];
      auto now = std::chrono::steady_clock::now();
      double actual_elapsed =
        std::chrono::duration<double>(now - start_wall).count();
      double sleep_time = expected_elapsed - actual_elapsed;
      if (sleep_time > 0.001) {
        std::this_thread::sleep_for(
          std::chrono::microseconds(static_cast<int64_t>(sleep_time * 1e6)));
      }

      const auto & bm = arm_messages[i];
      double arm_t = arm_time_diffs[i];

      if (bm.topic == "/hal/joint_states") {
        // 推进 head 指针到时间 ≤ arm_t 的最新帧
        while (head_idx + 1 < head_frames.size() &&
               head_frames[head_idx + 1].rel_time <= arm_t + 1e-6) {
          head_idx++;
        }

        auto arm_js = deserialize<sensor_msgs::msg::JointState>(bm.data);

        // 合并：按 STANDBY_JOINT_NAMES 顺序，臂从 arm mcap，头从 head mcap
        sensor_msgs::msg::JointState merged;
        merged.header = arm_js.header;
        merged.name = STANDBY_JOINT_NAMES;
        merged.position.resize(STANDBY_JOINT_NAMES.size());

        // 臂关节从 arm mcap 提取；开头 BLEND_FRAMES 帧从上次位置平滑过渡
        for (size_t ai = 0; ai < ARM_JOINT_NAMES.size(); ++ai) {
          bool found = false;
          for (size_t ji = 0; ji < arm_js.name.size() && ji < arm_js.position.size(); ++ji) {
            if (arm_js.name[ji] == ARM_JOINT_NAMES[ai]) {
              merged.position[ai] = arm_js.position[ji];
              found = true;
              break;
            }
          }
          if (!found) merged.position[ai] = ARM_STANDBY_POSITIONS[ai];
        }
        if (have_last_arm && js_count < BLEND_FRAMES) {
          double alpha = static_cast<double>(js_count + 1) / BLEND_FRAMES;
          for (size_t ai = 0; ai < ARM_JOINT_NAMES.size(); ++ai) {
            double target = merged.position[ai];
            merged.position[ai] =
              last_arm_pos[ai] + alpha * (target - last_arm_pos[ai]);
          }
        }

        // 头关节从 head mcap 取；开头 BLEND_FRAMES 帧从上次位置平滑过渡，避免循环时回中跳变
        size_t head_offset = ARM_JOINT_NAMES.size();
        if (head_idx < head_frames.size()) {
          for (size_t hi = 0; hi < HEAD_JOINT_NAMES.size(); ++hi) {
            merged.position[head_offset + hi] = head_frames[head_idx].positions[hi];
          }
          if (have_last_head && js_count < BLEND_FRAMES) {
            double alpha = static_cast<double>(js_count + 1) / BLEND_FRAMES;
            for (size_t hi = 0; hi < HEAD_JOINT_NAMES.size(); ++hi) {
              double target = merged.position[head_offset + hi];
              merged.position[head_offset + hi] =
                last_head_pos[hi] + alpha * (target - last_head_pos[hi]);
            }
          }
        } else if (!head_frames.empty()) {
          // head mcap 已播完，保持最后一帧（不跳变到 standby）
          for (size_t hi = 0; hi < HEAD_JOINT_NAMES.size(); ++hi) {
            merged.position[head_offset + hi] = head_frames.back().positions[hi];
          }
        } else {
          for (size_t hi = 0; hi < HEAD_JOINT_NAMES.size(); ++hi) {
            merged.position[head_offset + hi] = HEAD_STANDBY_POSITIONS[hi];
          }
        }
        js_count++;

        joint_pub_->publish(merged);
        // 缓存最后发布的关节指令及时间，供 standby 插值起点 + 回放开头平滑过渡使用
        {
          std::lock_guard<std::mutex> lock(last_cmd_mutex_);
          last_published_cmd_ = merged;
          has_last_published_ = true;
          last_cmd_time_ = std::chrono::steady_clock::now();
        }
      } else if (bm.topic == "/motion_ctl/gripper/left/state") {
        auto msg = deserialize<control_msgs::msg::GripperCommand>(bm.data);
        left_grip_pub_->publish(msg);
      } else if (bm.topic == "/motion_ctl/gripper/right/state") {
        auto msg = deserialize<control_msgs::msg::GripperCommand>(bm.data);
        right_grip_pub_->publish(msg);
      }

      // 进度反馈
      if (i % 100 == 0 || i == total - 1) {
        feedback->status = ArmExecute::Feedback::STATUS_EXECUTING;
        feedback->progress = static_cast<float>(i + 1) / static_cast<float>(total);
        goal_handle->publish_feedback(feedback);
        RCLCPP_INFO(get_logger(), "回放进度: %zu/%zu (%.0f%%)",
          i + 1, total, feedback->progress * 100.0f);
      }
    }

    // 7. 完成
    auto result = std::make_shared<ArmExecute::Result>();
    result->success = true;
    result->message = "回放完成 (共 " + std::to_string(total) + " 条消息)";
    goal_handle->succeed(result);
    RCLCPP_INFO(get_logger(), "bag 回放完成: %zu 条消息", total);
  }

  // ---- 保持姿态（hold_pose）----
  // 平滑插值到目标姿态后返回成功，姿态由最后发布的关节命令保持
  // 直到收到新动作（如 standby 归位）才会改变

  void executeHoldPose(std::shared_ptr<GoalHandle> goal_handle,
                       const std::vector<double> & target_positions,
                       double duration_sec)
  {
    RCLCPP_INFO(get_logger(), "执行保持姿态动作 (线性插值 + 持续保持, duration=%.1fs)", duration_sec);

    // 插值参数
    const double duration = duration_sec;
    const double rate_hz = 30.0;
    const int total_steps = static_cast<int>(duration * rate_hz);

    // 1. 获取插值起始点（同 standby 逻辑：优先 last_published_cmd_，回退 joint feedback）
    std::vector<double> start_positions(STANDBY_JOINT_NAMES.size(), 0.0);
    {
      sensor_msgs::msg::JointState last_cmd;
      bool have_last = false;
      {
        std::lock_guard<std::mutex> lock(last_cmd_mutex_);
        auto now_ = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration<double>(now_ - last_cmd_time_).count();
        if (has_last_published_ && elapsed < 3.0) {
          last_cmd = last_published_cmd_;
          have_last = true;
        }
      }

      if (have_last && last_cmd.name.size() == STANDBY_JOINT_NAMES.size() &&
          last_cmd.position.size() == STANDBY_JOINT_NAMES.size()) {
        for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
          start_positions[i] = last_cmd.position[i];
        }
        RCLCPP_INFO(get_logger(), "保持姿态: 使用最后发布的关节指令作为插值起点");
      } else {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        if (has_joint_state_) {
          for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
            for (size_t j = 0; j < latest_joint_state_.name.size(); ++j) {
              if (latest_joint_state_.name[j] == STANDBY_JOINT_NAMES[i] &&
                  j < latest_joint_state_.position.size()) {
                start_positions[i] = latest_joint_state_.position[j];
                break;
              }
            }
          }
          RCLCPP_INFO(get_logger(), "保持姿态: 使用当前关节反馈作为插值起点");
        } else {
          RCLCPP_WARN(get_logger(), "保持姿态: 未收到关节状态，使用全零作为插值起点");
        }
      }
    }

    // 打印起止位置
    RCLCPP_INFO(get_logger(), "保持姿态插值: steps=%d, %zu joints:",
      total_steps, STANDBY_JOINT_NAMES.size());
    for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
      RCLCPP_INFO(get_logger(), "  [%2zu] %-20s: %8.2f -> %8.2f",
        i, STANDBY_JOINT_NAMES[i].c_str(),
        start_positions[i], target_positions[i]);
    }

    // 2. 平滑插值到目标姿态
    sensor_msgs::msg::JointState cmd;
    cmd.name = STANDBY_JOINT_NAMES;
    cmd.position.resize(STANDBY_JOINT_NAMES.size());

    auto start_wall = std::chrono::steady_clock::now();
    for (int step = 1; step <= total_steps; ++step) {
      if (goal_handle->is_canceling()) {
        auto result = std::make_shared<ArmExecute::Result>();
        result->success = false;
        result->message = "保持姿态插值阶段被取消";
        goal_handle->canceled(result);
        RCLCPP_INFO(get_logger(), "保持姿态插值被取消 (step %d/%d)", step, total_steps);
        return;
      }

      double t = static_cast<double>(step) / static_cast<double>(total_steps);
      for (size_t i = 0; i < STANDBY_JOINT_NAMES.size(); ++i) {
        cmd.position[i] = start_positions[i] + t * (target_positions[i] - start_positions[i]);
      }
      cmd.header.stamp = now();
      joint_pub_->publish(cmd);

      // 缓存发布位置
      {
        std::lock_guard<std::mutex> lock(last_cmd_mutex_);
        last_published_cmd_ = cmd;
        has_last_published_ = true;
        last_cmd_time_ = std::chrono::steady_clock::now();
      }

      auto expected_time = start_wall + std::chrono::microseconds(
        static_cast<int64_t>(step * 1e6 / rate_hz));
      auto sleep_until = expected_time - std::chrono::steady_clock::now();
      if (sleep_until.count() > 0) {
        std::this_thread::sleep_for(
          std::chrono::duration_cast<std::chrono::microseconds>(sleep_until));
      }
    }

    // 3. 精确发布目标位置
    cmd.position = target_positions;
    cmd.header.stamp = now();
    joint_pub_->publish(cmd);
    {
      std::lock_guard<std::mutex> lock(last_cmd_mutex_);
      last_published_cmd_ = cmd;
      has_last_published_ = true;
      last_cmd_time_ = std::chrono::steady_clock::now();
    }

    RCLCPP_INFO(get_logger(), "保持姿态: 已到达目标，动作完成");

    // 3. 返回成功（姿态由最后发布的关节命令保持，直到新动作到来）
    auto result = std::make_shared<ArmExecute::Result>();
    result->success = true;
    result->message = "保持姿态到位完成";
    goal_handle->succeed(result);
    RCLCPP_INFO(get_logger(), "保持姿态动作成功返回");
  }

  // ---- 成员变量 ----
  std::string config_dir_;   // 配置文件所在目录，用于解析相对路径
  rclcpp_action::Server<ArmExecute>::SharedPtr action_server_;
  rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr joint_pub_;
  rclcpp::Publisher<control_msgs::msg::GripperCommand>::SharedPtr left_grip_pub_;
  rclcpp::Publisher<control_msgs::msg::GripperCommand>::SharedPtr right_grip_pub_;
  std::unordered_map<uint32_t, ActionMapping> action_map_;
  std::unordered_map<std::string, PoseConfig> named_poses_;  // 预定义姿态：name -> {positions, duration}

  // 关节状态订阅（用于 standby 插值起始位置）
  rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr joint_state_sub_;
  std::mutex joint_state_mutex_;
  sensor_msgs::msg::JointState latest_joint_state_;
  bool has_joint_state_ = false;

  // 缓存最后发布的关节指令（bag回放时更新），
  // standby 从此位置开始插值，避免命令跳变导致关节抖动
  std::mutex last_cmd_mutex_;
  sensor_msgs::msg::JointState last_published_cmd_;
  bool has_last_published_ = false;
  std::chrono::steady_clock::time_point last_cmd_time_;

  // 升降控制配置与状态
  std::string lift_script_path_;                    // 升降脚本路径（可空=禁用）
  std::string lift_motors_ = "5,6,7";              // 升降电机 ID（腰部）
  std::unordered_map<uint32_t, LiftLevel> lift_levels_;  // 档位 -> 目标配置
  int last_lift_level_ = -1;  // 当前腰部高度档位（-1=未知，>=0=档位）
  int default_level_ = 0;     // 默认档位：归位后腰部停在此档位（由配置 lift.default_level 指定）
  std::mutex lift_mutex_;     // 升降互斥：同一时刻只允许一个升降脚本运行

  // Goal 排队：单线程顺序执行，避免并发发布冲突指令
  std::mutex queue_mutex_;    // 保护 goal 队列
  std::deque<std::shared_ptr<GoalHandle>> goal_queue_;  // 等待执行的 goal 队列
  bool worker_running_{false};  // 工作线程是否在运行
  std::chrono::steady_clock::time_point last_request_time_;  // 上次请求时间（用于计算间隔）
  int action_rest_ms_ = 300;  // 动作执行间休息时间（毫秒），0=不休息
};

// ---- 配置文件路径解析 ----

static std::string resolveConfigPath(const std::string & cli_path)
{
  if (!cli_path.empty()) return cli_path;

  const char * ament_prefix = getenv("AMENT_PREFIX_PATH");
  if (!ament_prefix) return "";

  std::string prefix(ament_prefix);
  const std::string rel = "/share/arm_replay/config/arm_replay_config.yaml";
  std::string search = prefix;

  while (!search.empty()) {
    size_t colon_pos = search.find(':');
    std::string p = (colon_pos != std::string::npos)
                    ? search.substr(0, colon_pos) : search;
    std::string candidate = p + rel;
    if (access(candidate.c_str(), R_OK) == 0) {
      return candidate;
    }
    if (colon_pos == std::string::npos) break;
    search = search.substr(colon_pos + 1);
  }

  // 回退：使用第一个路径
  size_t colon_pos = prefix.find(':');
  std::string first = (colon_pos != std::string::npos)
                      ? prefix.substr(0, colon_pos) : prefix;
  return first + rel;
}

int main(int argc, char ** argv)
{
  // ---- 初始化 spdlog（终端 + 文件双输出）----
  std::string log_dir = std::string(getenv("HOME") ? getenv("HOME") : ".") +
                        "/logs/guide_robot";
  mkdirRecursive(log_dir);
  std::string log_file = log_dir + "/arm_replay.log";

  try {
    auto console_sink = std::make_shared<spdlog::sinks::stdout_color_sink_mt>();
    auto file_sink = std::make_shared<spdlog::sinks::rotating_file_sink_mt>(
      log_file,
      10 * 1024 * 1024,  // 每文件 10MB
      5);                  // 保留 5 个历史文件

    auto dist_sink = std::make_shared<spdlog::sinks::dist_sink_mt>();
    dist_sink->add_sink(console_sink);
    dist_sink->add_sink(file_sink);

    g_logger = std::make_shared<spdlog::logger>("arm_replay", dist_sink);
    g_logger->set_level(spdlog::level::debug);
    g_logger->set_pattern("[%Y-%m-%d %H:%M:%S.%e] [%^%l%$] %v");
    spdlog::register_logger(g_logger);
  } catch (const spdlog::spdlog_ex & e) {
    fprintf(stderr, "spdlog 初始化失败: %s\n", e.what());
    return 1;
  }

  rclcpp::init(argc, argv);

  // 替换默认日志处理器，将所有 RCLCPP 日志路由到 spdlog
  {
    auto rcl_ctx =
      rclcpp::contexts::get_global_default_context()->get_rcl_context();
    rcl_allocator_t allocator = rcl_get_default_allocator();
    rcl_ret_t ret = rcl_logging_configure_with_output_handler(
      &rcl_ctx->global_arguments,
      &allocator,
      spdlogOutputHandler);
    if (ret != RCL_RET_OK) {
      fprintf(stderr, "警告: rcl 日志处理器替换失败 (ret=%d)\n", ret);
    }
  }

  // 解析命令行参数（在 --ros-args 之前截断，避免 getopt 处理 ROS 参数）
  std::string config_path;
  int my_argc = argc;
  for (int i = 1; i < argc; ++i) {
    if (strcmp(argv[i], "--ros-args") == 0) {
      my_argc = i;
      break;
    }
  }

  int opt;
  optind = 1;
  while ((opt = getopt(my_argc, argv, "c:h")) != -1) {
    switch (opt) {
      case 'c':
        config_path = optarg;
        break;
      case 'h':
        printf("用法: %s [选项]\n", argv[0]);
        printf("  -c <path>   配置文件路径 (默认从 AMENT_PREFIX_PATH 查找)\n");
        printf("  -h          显示帮助\n");
        rclcpp::shutdown();
        spdlog::drop_all();
        return 0;
      default:
        break;
    }
  }

  config_path = resolveConfigPath(config_path);
  if (config_path.empty()) {
    fprintf(stderr, "错误: 无法确定配置文件路径，请用 -c 指定\n");
    rclcpp::shutdown();
    spdlog::drop_all();
    return 1;
  }

  RCLCPP_INFO(rclcpp::get_logger("arm_replay"),
    "====================================");
  RCLCPP_INFO(rclcpp::get_logger("arm_replay"),
    "  机械臂回放节点");
  RCLCPP_INFO(rclcpp::get_logger("arm_replay"),
    "  配置文件: %s", config_path.c_str());
  RCLCPP_INFO(rclcpp::get_logger("arm_replay"),
    "  日志文件: %s", log_file.c_str());
  RCLCPP_INFO(rclcpp::get_logger("arm_replay"),
    "====================================");

  try {
    auto node = std::make_shared<ArmReplayNode>(config_path);
    rclcpp::spin(node);
  } catch (const std::exception & e) {
    RCLCPP_ERROR(rclcpp::get_logger("arm_replay"),
      "程序异常: %s", e.what());
  }

  rclcpp::shutdown();
  spdlog::drop_all();
  return 0;
}
