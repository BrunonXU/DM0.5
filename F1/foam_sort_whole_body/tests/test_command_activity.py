"""External command interleaving tests; never publishes to a robot."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.ros_backend import RosBackend, COMMAND_TYPES
from f1_replay.runtime import check_command_peers, check_command_activity, ReplayError


class CommandActivityTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'command_ownership_policy':'observed_idle_v1',
                        'topics':{k:'/'+k for k in COMMAND_TYPES}}
        self.backend = RosBackend(self.profile)
        self.backend._monitor_started_ns = time.monotonic_ns()-2_000_000_000
        self.peers = {self.profile['topics'][k]: {
            'publishers':[{'name':'standby','namespace':'/','type':t}],
            'subscribers':[{'name':'controller','namespace':'/','type':t}]
        } for k,t in COMMAND_TYPES.items()}
        self.backend.command_peers = lambda:self.peers

    def test_idle_services_can_coexist(self):
        check_command_peers(self.backend,self.profile)

    def test_external_zero_command_also_blocks(self):
        self.backend._command_observer('base_command')(NS(),NS(publisher_gid=bytes([7]*16)))
        with self.assertRaisesRegex(ReplayError,'实际发送'):
            check_command_peers(self.backend,self.profile)

    def test_only_exact_own_gid_is_ignored(self):
        own = bytes([8]*16)
        self.backend._own_command_gids['arms_command'] = own.hex()
        self.backend._command_observer('arms_command')(NS(),NS(publisher_gid=own))
        check_command_activity(self.backend,self.profile)
        self.backend._command_observer('arms_command')(NS(),NS(publisher_gid=bytes([9]*16)))
        with self.assertRaises(ReplayError):check_command_activity(self.backend,self.profile)

    def test_missing_gid_is_not_treated_as_own(self):
        self.backend._command_observer('base_command')(NS(),NS(publisher_gid=bytes(16)))
        with self.assertRaises(ReplayError):check_command_activity(self.backend,self.profile)

    def test_jazzy_dictionary_message_info(self):
        own=bytes([4]*16)
        self.backend._own_command_gids['base_command']=own.hex()
        self.backend._command_observer('base_command')(NS(),{'publisher_gid':own})
        check_command_activity(self.backend,self.profile)
        self.backend._command_observer('base_command')(NS(),{'publisher_gid':bytes([5]*16)})
        with self.assertRaises(ReplayError):check_command_activity(self.backend,self.profile)

    def test_monitor_subscription_cannot_substitute_for_driver(self):
        for entry in self.peers.values():
            entry['subscribers'][0].update(name=self.backend.node_name,namespace=self.backend.node_namespace)
        with self.assertRaisesRegex(ReplayError,'未发现控制器'):
            check_command_peers(self.backend,self.profile)

    def test_short_observation_and_missing_own_identity_block(self):
        self.backend._monitor_started_ns=time.monotonic_ns()
        with self.assertRaises(ReplayError):check_command_activity(self.backend,self.profile)
        self.backend._monitor_started_ns-=2_000_000_000
        self.backend._commands_enabled=True
        with self.assertRaises(ReplayError):check_command_activity(self.backend,self.profile)

    def test_conflict_latches_and_cannot_be_cleared_by_snapshot_mutation(self):
        self.backend._command_observer('arms_command')(NS(),NS(publisher_gid=bytes([1]*16)))
        self.backend.command_activity()['external_commands'].clear()
        with self.assertRaises(ReplayError):check_command_activity(self.backend,self.profile)

    def test_old_profiles_remain_strict(self):
        del self.profile['command_ownership_policy']
        with self.assertRaisesRegex(ReplayError,'存在其他'):
            check_command_peers(self.backend,self.profile)

if __name__=='__main__':unittest.main()
