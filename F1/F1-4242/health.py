"""Read-only disk, RAM and selected process state; never scans episode files."""
from pathlib import Path
import shutil,time

def snapshot(data_path):
    path=Path(data_path)
    disk=shutil.disk_usage(path if path.exists() else path.parent)
    memory={}
    for row in Path('/proc/meminfo').read_text().splitlines():
        key,rest=row.split(':',1)
        if key in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):memory[key]=int(rest.split()[0])*1024
    services={key:{'running':False,'pids':[]} for key in ('motor_ctl','kinematics','lxl_teleop','lxl_bridge','pico_pc_service','pico_upper')}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:cmd=(entry/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
        except OSError:continue
        markers={'motor_ctl':'/motor_ctl_node','kinematics':'/kinematics_node','lxl_teleop':'scripts/simulation/teleop_lxl.py','lxl_bridge':'all_remote_lxl.py','pico_pc_service':'./RoboticsServiceProcess','pico_upper':'-m pico_upper.supervisor'}
        for key,marker in markers.items():
            if marker in cmd:
                services[key]['running']=True;services[key]['pids'].append(int(entry.name))
    free=disk.free
    return {'checked_unix':time.time(),'disk':{'path':str(path),'total_bytes':disk.total,'free_bytes':free,'used_bytes':disk.used,'episodes_at_1GB_reserve_100GB':max(0,(free-100_000_000_000)//1_000_000_000)},'memory':memory,'services':services,'note':'进程状态与ROS反馈新鲜度分别检查；容量估算按十进制GB，未扫描或修改录制数据。'}
