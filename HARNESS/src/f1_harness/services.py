"""Explicit local service lifecycle; no service grants START authority."""
from pathlib import Path
import json
import threading
import time


def serve(kind,config_path:Path,store, *, numerical_pool=None):
    from .ipc import UnixServer
    config=json.loads(config_path.read_text())
    server=observer=mask_client=lease=None
    try:
        if kind=='numerics':
            from .numerical_pool import NUMERICAL_RPC_TIMEOUT_S
            if numerical_pool is not None:
                from .numerical_pool import acquire
                try:
                    lease,receipt=acquire(numerical_pool,config_path,
                        bundle=Path(__file__).resolve().parents[2])
                except (OSError,ValueError,RuntimeError) as exc:
                    # Numerical-only acquisition is optional. No robot command
                    # has been dispatched; never retry a failed leased solve.
                    receipt=dict(reused=False,reason=type(exc).__name__)
                store.event('numerical_initialization_route',**receipt)
            from .numerical_service import NumericalDispatcher,NUMERICAL_CLIENTS
            if lease is None:
                from .mesh_cache import register_mesh_cache
                register_mesh_cache(config_path.parent,store)
                numerics=NumericalDispatcher(config['spec'])
            else:numerics=lease
            server=UnixServer(Path(config['socket']),numerics.dispatch,
                             max_clients=NUMERICAL_CLIENTS,timeout=NUMERICAL_RPC_TIMEOUT_S).start()
        elif kind in ('plan','verify'):
            from .contracts import Task
            from .skill_services import PlanService,VerifyService
            task=Task.model_validate(config['task'])
            if kind=='plan':
                service=PlanService(task,config['fixture'],Path(config['numerical_socket']))
            else:
                gateway_path=config_path.with_name('gateway.local.json')
                gateway=json.loads(gateway_path.read_text()) if gateway_path.is_file() else {}
                geometry=gateway.get('scene',{}).get('geometry',{})
                service=VerifyService(task,fixture=config.get('fixture'),
                                      independent_grasp=bool(geometry.get('measured_gripper_contact')))
            server=UnixServer(Path(config['socket']),service.dispatch,timeout=92 if kind=='plan' else 12,
                             scene_evidence=True).start()
        elif kind=='masks':
            from .mask_service import mask_server
            images=mask_server(config)
        elif kind=='gateway':
            from .gateway import Gateway,GatewayServer,blob_server
            from .remote import OwnerProxy
            from .ros_observer import RosObserver
            from .evidence import FrameCache
            observer=RosObserver(config['observation'],FrameCache(config.get('frame_cache_bytes',256*1024*1024)))
            threading.Thread(target=observer.spin,daemon=True).start()
            if config.get('scene'):
                from .mask_service import scene_mask_provider
                from .live_scene import LiveSceneObserver
                from .aperture_view import planner_view_geometry
                plan_path = config_path.with_name('plan.local.json')
                if plan_path.is_file():
                    plan_doc = json.loads(plan_path.read_text())
                    config['scene']['aperture_view_geometry'] = planner_view_geometry(
                        plan_doc, config['scene'])
                    bucket_obs = [
                        o for o in (plan_doc.get('fixture') or {}).get('obstacles', ())
                        if o.get('role') == 'bucket' and 'min' in o and 'max' in o
                    ]
                    if bucket_obs and (plan_doc.get('fixture') or {}).get('fixture_reference_hole') == config['scene'].get('geometry', {}).get('base_hole'):
                        import numpy as np
                        lo = np.min([o['min'] for o in bucket_obs], axis=0).tolist()
                        hi = np.max([o['max'] for o in bucket_obs], axis=0).tolist()
                        config['scene']['geometry']['fixture_exclusion_box'] = dict(min=lo, max=hi)
                mask_client=scene_mask_provider(config['scene'],config_path.parent)
                observer=LiveSceneObserver(observer,config['scene'],mask_client,store)
            server=GatewayServer(Gateway(OwnerProxy(Path(config['owner_socket'])),observer,store),port=config['control_port']).start()
            images=blob_server(store if config.get('scene') else observer.store,port=config['image_port'])
        else:raise ValueError('unregistered service')
        store.event('service_ready',service=kind,command_publishers=0)
        print(json.dumps(dict(ready=True,service=kind,command_publishers=0)),flush=True)
        while True:time.sleep(.2)
    except KeyboardInterrupt:
        return dict(stopped=True,service=kind)
    finally:
        if 'images' in locals():images.shutdown();images.server_close()
        if server:server.close()
        if lease:
            try:lease.close()
            except (OSError,ValueError,RuntimeError) as exc:
                store.event('numerical_lease_cleanup_unknown',error_type=type(exc).__name__)
        if observer:observer.close()
        if mask_client:mask_client.close()
