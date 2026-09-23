import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import yaml

from pico_f1.collection import load_settings, effective_settings, bind_route, start, stop, control
from pico_f1.collection_control import handle, expire_leases, manual_view, RECEIVER, DASHBOARD
from pico_f1.transfer import atomic_json, read, merge, enqueue, outbox_index
from pico_f1.lerobot_v3 import LeRobotV3Dataset
import test_lerobot_v3 as fixtures


class CollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.project = self.home / "pico"
        (self.project / "config").mkdir(parents=True)
        self.base = self.home / "storage/pico/datasets"
        self.base.mkdir(parents=True)
        self.cfg = dict(scene="mm", task="synthetic", local_base="mocap_data", central=dict(
            dataset_base=str(self.base), host="100.71.200.77", port=22222, user="xx_100872445",
            identity_file="config/private/key", known_hosts_file="config/private/hosts", dashboard=True))
        self.write_config()

    def write_config(self):
        (self.project / "config/collection.yaml").write_text(yaml.safe_dump(self.cfg))

    def request(self, robot="F1_D070", token=None, operation="start", scene="mm"):
        return dict(operation=operation, robot=robot, token=token or str(uuid.uuid4()),
                    destination=str(self.base / scene / "mocap_data"), dashboard=True)

    def test_yaml_and_frozen_session(self):
        settings = load_settings(self.project)
        self.assertEqual(settings["source"], str(self.project / "mocap_data/mm/mocap_data"))
        atomic_json(self.project / "run/collection_session.json", dict(settings=settings))
        self.cfg["scene"] = "pick"
        self.write_config()
        self.assertEqual(effective_settings(self.project)["scene"], "mm")
        self.assertEqual(load_settings(self.project)["scene"], "pick")

    def test_invalid_scene_and_escape_rejected(self):
        for field, value in (("scene", "../oops"), ("local_base", "../data"), ("scene", "x; rm")):
            old = self.cfg[field]
            self.cfg[field] = value
            self.write_config()
            with self.assertRaises(ValueError):
                load_settings(self.project)
            self.cfg[field] = old

    def test_routes_preserve_old_scene(self):
        bind_route(self.project, load_settings(self.project))
        self.cfg["scene"] = "pick"
        self.write_config()
        bind_route(self.project, load_settings(self.project))
        routes = read(self.project / "transfer_state/routes.json")
        self.assertEqual({r["scene"] for r in routes.values()}, {"mm", "pick"})
        self.cfg["task"] = "another task"
        self.write_config()
        with self.assertRaisesRegex(ValueError, "不能改"):
            bind_route(self.project, load_settings(self.project))

    def test_user_tailnet_proxy_and_no_fallback(self):
        self.cfg['central']['use_user_tailscale'] = True
        self.write_config()
        private = self.project/'config/private'
        private.mkdir()
        (private/'key').touch()
        (private/'hosts').touch()
        session = dict(robot='F1_D070_01', token=str(uuid.uuid4()), settings=load_settings(self.project))
        with patch('pico_f1.collection.Path.home', return_value=self.home):
            with self.assertRaisesRegex(ValueError, '不回退'):
                control(self.project, session, 'start')
            proxy = self.home/'.local/bin/tailscale-user'
            proxy.parent.mkdir(parents=True)
            proxy.touch()
            with patch('pico_f1.collection.subprocess.run') as run:
                run.return_value.returncode = 0
                run.return_value.stdout = '{"ok": true}'
                self.assertTrue(control(self.project, session, 'start')['ok'])
                command = run.call_args.args[0]
                self.assertTrue(any('ProxyCommand=env XDG_RUNTIME_DIR=' in x and 'tailscale-user' in x for x in command))
                self.assertIn('StrictHostKeyChecking=yes', command)
        self.cfg['central']['use_user_tailscale'] = 'true'
        self.write_config()
        with self.assertRaises(ValueError):
            load_settings(self.project)

    def test_last_robot_stops_both_services_only(self):
        events = []
        service = lambda action, units: events.append((action, units))
        a, b = self.request(), self.request("F1_D072", scene="pick")
        with patch("pico_f1.collection_control.DEFAULT_BASE", self.base):
            handle(self.project, a["robot"], a, service, now=100)
            handle(self.project, b["robot"], b, service, now=101)
            events.clear()
            a["operation"] = "stop"
            result = handle(self.project, a["robot"], a, service, now=102)
            self.assertEqual(result["active_robots"], ["F1_D072"])
            self.assertNotIn(("stop", [RECEIVER, DASHBOARD]), events)
            b["operation"] = "stop"
            result = handle(self.project, b["robot"], b, service, now=103)
            self.assertEqual(result["active_robots"], [])
            self.assertEqual(events[-1], ("stop", [RECEIVER, DASHBOARD]))

    def test_stale_stop_and_wrong_robot_rejected(self):
        request = self.request()
        with patch("pico_f1.collection_control.DEFAULT_BASE", self.base):
            handle(self.project, "F1_D070", request, lambda *a: None, now=100)
            with self.assertRaises(ValueError):
                handle(self.project, "F1_D072", request, lambda *a: None, now=101)
            old = dict(request, operation="stop", token=str(uuid.uuid4()))
            with self.assertRaisesRegex(ValueError, "stale"):
                handle(self.project, "F1_D070", old, lambda *a: None, now=101)

    def test_retry_same_start_and_renew(self):
        request = self.request()
        with patch("pico_f1.collection_control.DEFAULT_BASE", self.base):
            handle(self.project, "F1_D070", request, lambda *a: None, now=100)
            handle(self.project, "F1_D070", request, lambda *a: None, now=101)
            request["operation"] = "renew"
            handle(self.project, "F1_D070", request, lambda *a: None, now=102)
        leases = read(self.project.parent / "transfer_6000d/collection_leases.json")
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases["F1_D070"]["expires"], 192)

    def test_watchdog_only_stops_after_last_lease_expires(self):
        path = self.project.parent / "transfer_6000d/collection_leases.json"
        atomic_json(path, {"F1_D070": {"expires": 99}, "F1_D072": {"expires": 110}})
        with patch("pico_f1.collection_control.time.time", return_value=100), \
             patch("pico_f1.collection_control.subprocess.run") as stop_service:
            expire_leases(self.project)
            stop_service.assert_not_called()
        self.assertEqual(list(read(path)), ["F1_D072"])
        with patch("pico_f1.collection_control.time.time", return_value=111), \
             patch("pico_f1.collection_control.subprocess.run") as stop_service:
            expire_leases(self.project)
            self.assertIn(RECEIVER, stop_service.call_args.args[0])
            self.assertIn(DASHBOARD, stop_service.call_args.args[0])
        self.assertEqual(read(path), {})

    def test_failed_stop_retains_snapshot_for_retry(self):
        session = dict(robot="F1_D070", token=str(uuid.uuid4()), settings=load_settings(self.project))
        marker = self.project / "run/collection_session.json"
        atomic_json(marker, session)
        with patch("pico_f1.collection.stop_agent") as local_stop, patch("pico_f1.collection.control", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                stop(self.project)
            local_stop.assert_called_once()
        self.assertTrue(marker.exists())
        self.cfg["scene"] = "changed"
        self.write_config()
        with patch("pico_f1.collection.stop_agent"), patch("pico_f1.collection.control", return_value={"active_robots": []}) as remote:
            stop(self.project)
            self.assertEqual(remote.call_args.args[1]["settings"]["scene"], "mm")
        self.assertFalse(marker.exists())

    def test_manual_dashboard_survives_robot_stop_and_expiry(self):
        events = []
        service = lambda action, units: events.append((action, units))
        target = self.base/'mm/mocap_data'
        target.mkdir(parents=True)
        with patch('pico_f1.collection_control.DEFAULT_BASE', self.base):
            manual_view(self.project, 'start', target, service, now=100)
            self.assertTrue(all(RECEIVER not in units for _, units in events))
            request = self.request('F1_D070_01', scene='pick')
            handle(self.project, request['robot'], request, service, now=101)
            self.assertEqual(read(self.home/'transfer_6000d/dashboard_selection.json')['root'], str(target))
            events.clear()
            handle(self.project, request['robot'], dict(request, operation='stop'), service, now=102)
            self.assertNotIn(('stop', [DASHBOARD]), events)
            with patch('pico_f1.collection_control.subprocess.run') as ctl:
                expire_leases(self.project)
                self.assertNotIn(DASHBOARD, ctl.call_args.args[0])
            events.clear()
            result = manual_view(self.project, 'stop', service=service, now=103)
            self.assertFalse(result['dashboard_needed'])
            self.assertEqual(events, [('stop', [DASHBOARD])])

    def test_manual_release_restores_active_scene_without_stopping_receiver(self):
        events = []
        service = lambda action, units: events.append((action, units))
        target = self.base/'review/mocap_data'
        target.mkdir(parents=True)
        with patch('pico_f1.collection_control.DEFAULT_BASE', self.base):
            request = self.request('F1_D072')
            handle(self.project, request['robot'], request, service, now=100)
            manual_view(self.project, 'start', target, service, now=101)
            events.clear()
            result = manual_view(self.project, 'stop', service=service, now=102)
            self.assertTrue(result['dashboard_needed'])
            self.assertEqual(events, [('restart', [DASHBOARD])])
            self.assertEqual(read(self.home/'transfer_6000d/dashboard_selection.json')['root'], request['destination'])

    def test_manual_missing_root_rejected_without_marker(self):
        with self.assertRaisesRegex(ValueError, '不存在'):
            manual_view(self.project, 'start', self.base/'missing', lambda *a: None)
        self.assertFalse((self.home/'transfer_6000d/dashboard_manual.json').exists())

    def test_restart_same_scene_continues_indices_and_other_scene_separate(self):
        cfg = load_settings(self.project)
        source = Path(cfg["source"])
        bind_route(self.project, cfg)
        writer = LeRobotV3Dataset(source, 30, "synthetic")
        fixture = fixtures.LeRobotV3WriterTest()
        for offset in (0, 10):
            staging, arrays, cameras = fixture._episode(source, offset)
            writer.commit(staging, arrays, cameras)
            # Each new simulated session reloads the same YAML/registry.
            bind_route(self.project, load_settings(self.project))
            enqueue(self.project, source, "F1_D070", Path(cfg["destination"]))
            for row in outbox_index(self.project):
                bundle = self.project / "transfer_state/outbox" / row["key"]
                merge(bundle / "payload", read(bundle / "manifest.json"), self.base)
        self.assertEqual(read(Path(cfg["destination"]) / "meta/info.json")["total_episodes"], 2)
        self.cfg["scene"] = "pick"
        self.write_config()
        cfg2 = load_settings(self.project)
        bind_route(self.project, cfg2)
        source2 = Path(cfg2["source"])
        staging, arrays, cameras = fixture._episode(source2, 0)
        LeRobotV3Dataset(source2, 30, "synthetic").commit(staging, arrays, cameras)
        keys = enqueue(self.project, source2, "F1_D070", Path(cfg2["destination"]))
        bundle = self.project / "transfer_state/outbox" / keys[0]
        record = merge(bundle / "payload", read(bundle / "manifest.json"), self.base)
        self.assertEqual(record["global_episode"], 0)
        self.assertEqual(read(Path(cfg["destination"]) / "meta/info.json")["total_episodes"], 2)


if __name__ == "__main__":
    unittest.main()
