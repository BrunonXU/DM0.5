"""Check the C++ type support rosbag needs, without opening command publishers."""
import ctypes
from .runtime import ReplayError
from .ros_backend import COMMAND_TYPES


def check_types(profile, backend):
    from ament_index_python.packages import get_package_prefix
    from .cli import select_recording_topics
    topics, diagnostics = select_recording_topics(profile, backend)
    peers = backend.topic_peers(topics)
    command_types = {profile['topics'][key]: value for key, value in COMMAND_TYPES.items()}
    checked = {}
    for topic in topics:
        types = {p['type'] for p in peers.get(topic, {}).get('publishers', [])}
        if topic in command_types:
            types.add(command_types[topic])
        if len(types) != 1:
            raise ReplayError(f'录包类型缺失或冲突：{topic}: {sorted(types)}')
        typename = types.pop()
        try:
            package, category, name = typename.split('/')
            library = get_package_prefix(package) + '/lib/lib' + package + '__rosidl_typesupport_cpp.so'
            lib = ctypes.CDLL(library)
            getattr(lib, f'rosidl_typesupport_cpp__get_message_type_support_handle__{package}__{category}__{name}')
        except Exception as exc:
            raise ReplayError(f'录包消息类型无法加载：{topic} {typename}: {exc}') from exc
        checked[topic] = {'type': typename, 'library': library}
    return {'types': checked, 'diagnostics': diagnostics, 'recorder_started': False}
