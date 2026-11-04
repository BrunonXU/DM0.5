"""Local placement lifetime, independent of physical episode and controller."""
from dataclasses import dataclass, asdict

OPERATOR_PROTOCOL = 'f1_rlt_local_v4'
OBJECTIVE = 'approach_place_release_v1'


@dataclass
class LocalPlacement:
    episode_id: str | None = None
    segment_id: str | None = None
    status: str = 'unused'
    started_ns: int | None = None
    ended_ns: int | None = None
    assisted: bool = False
    actor_version: str | None = None

    def begin(self, episode_id, now, actor_version):
        if self.status != 'unused':
            raise ValueError('Only one local placement per episode')
        self.episode_id = episode_id
        self.segment_id = episode_id + ':placement:1'
        self.status, self.started_ns = 'active', now
        self.actor_version = actor_version
        return self.metadata()

    def finish(self, now, outcome, reason, terminal_observation):
        if self.status != 'active':
            return None
        if outcome not in ('success', 'failure', 'pending_review', 'interrupted'):
            raise ValueError('Invalid local outcome')
        self.status, self.ended_ns = outcome, now
        return dict(self.metadata(), termination_reason=reason,
                    reward=1.0 if outcome == 'success' else 0.0 if outcome == 'failure' else None,
                    result_schema='f1_local_result_v2',
                    reward_known=outcome in ('success', 'failure'),
                    terminated=outcome in ('success', 'failure'), truncated=outcome == 'interrupted',
                    terminal_observation=terminal_observation, physical_reset=False)

    def metadata(self):
        return dict(asdict(self), objective=OBJECTIVE)
