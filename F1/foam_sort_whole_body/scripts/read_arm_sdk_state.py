"""Read ONLY the existing motor_ctl SDK status buffer; no attach or control calls.

For the audited 4F-F1 binary only. /proc/<pid>/mem is opened O_RDONLY.
Never loads the SDK, connects to the controller, clears errors or stops a process.
"""
import hashlib,json,math,os,struct,time
from pathlib import Path

SDK=Path('/home/user/workspace/f1/install/tj_sdk/lib/libMarvinSDK.so')
SDK_SHA='8580298c967ba2ca9166a7175a289a59c3d5aa3a9ae09c17373fde36c4092346'
EXE='/home/user/workspace/f1/install/motor_ctl/lib/motor_ctl/motor_ctl_node'
MACHINE='f8181edeb110424bbd1963cf717dfeee'

def decode(data):
    if len(data)!=1428:raise ValueError('SDK status size mismatch')
    result={}
    for side,index in [('left',0),('right',1)]:
        inp=24+index*368;out=760+index*312
        mode,cmd,error=struct.unpack_from('<iii',data,index*12)
        floats=lambda offset:list(struct.unpack_from('<7f',data,offset))
        result[side]=dict(current_state=mode,command_state=cmd,error_code=error,
            rt_input_enabled=struct.unpack_from('<i',data,inp)[0],
            input_frame=struct.unpack_from('<i',data,inp+8)[0],
            missed_frames=struct.unpack_from('<h',data,inp+12)[0],
            drag_type=struct.unpack_from('<i',data,inp+176)[0],
            velocity_percent=struct.unpack_from('<h',data,inp+116)[0],
            acceleration_percent=struct.unpack_from('<h',data,inp+118)[0],
            output_frame=struct.unpack_from('<i',data,out)[0],
            joint_position_deg=floats(out+4),joint_velocity=floats(out+32),
            feedback_command_deg=floats(out+88),sdk_input_target_deg=floats(inp+88),
            current=floats(out+116),torque=floats(out+144),temperature=floats(out+172))
        if any(not math.isfinite(v) for k in ['joint_position_deg','feedback_command_deg','sdk_input_target_deg'] for v in result[side][k]):
            raise ValueError('Invalid SDK status values')
    return result

def main():
    if Path('/etc/machine-id').read_text().strip()!=MACHINE:raise RuntimeError('Wrong robot')
    if hashlib.sha256(SDK.read_bytes()).hexdigest()!=SDK_SHA:raise RuntimeError('SDK version changed; offsets not valid')
    pids=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            if os.readlink(proc/'exe')==EXE:pids.append(proc)
        except OSError:pass
    if len(pids)!=1:raise RuntimeError(f'Expected one motor_ctl, found {len(pids)}')
    proc=pids[0];maps=[];base=None
    for line in (proc/'maps').read_text().splitlines():
        fields=line.split();lo,hi=(int(x,16) for x in fields[0].split('-'))
        maps.append((lo,hi,fields[1]))
        if len(fields)>=6 and fields[5]==str(SDK) and fields[2]=='00000000':base=lo
    if base is None:raise RuntimeError('Audited SDK not mapped')
    fd=os.open(proc/'mem',os.O_RDONLY)
    def read(addr,size):
        if not any(lo<=addr and addr+size<=hi and 'r' in perms for lo,hi,perms in maps):
            raise RuntimeError('SDK address outside readable mapping')
        value=os.pread(fd,size,addr)
        if len(value)!=size:raise RuntimeError('Short SDK read')
        return value
    try:
        # Audited AArch64 CRobot::OnGetBuf reads *(base+0x408a8)+0x530,
        # copying precisely 0x594 bytes. Only the cached DCSS is read.
        pointer=struct.unpack('<Q',read(base+0x408a8,8))[0]
        if not pointer:raise RuntimeError('SDK object not initialized')
        samples=[]
        for _ in range(6):
            samples.append({'monotonic_ns':time.monotonic_ns(),**decode(read(pointer+0x530,1428))})
            time.sleep(.1)
    finally:os.close(fd)
    report=dict(pid=int(proc.name),sdk_sha256=SDK_SHA,hardware_published=False,
                sdk_calls_made=False,process_stopped=False,snapshot_atomic=False,
                frame_changed={s:len({x[s]['output_frame'] for x in samples})>1 for s in ['left','right']},
                samples=samples)
    print(json.dumps(report,indent=2,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':f'{type(exc).__name__}: {exc}','hardware_published':False},ensure_ascii=False))
        raise SystemExit(2)
