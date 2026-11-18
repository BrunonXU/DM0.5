"""Summarize original owner evidence; proposals remain quarantined until validation."""
import json
from pathlib import Path
from .contracts import digest
from .evidence import EvidenceStore
from .memory import ExperienceStore


def summarize_run(run, experience_root):
    run=Path(run);source=run/'collected/owner/harness/trace.jsonl'
    if not source.is_file():return None
    from .program_experience import (register_run_agent_programs, review_agent_programs,
                                    run_program_evidence, archive_exploration_cells)
    def agent_review():
        registered=register_run_agent_programs(run,experience_root)
        registered['exploration_ids']=archive_exploration_cells(run,experience_root)
        from .contracts import HarnessError
        try:identity=run_program_evidence(run)['identity']
        except (HarnessError,ValueError,KeyError,TypeError,OSError) as exc:
            registered['independent_validation']=dict(promoted=[],reason=str(exc)[:160])
            return registered
        if (identity.get('heldout') is True and identity.get('scenario_id')
                and identity.get('scene_manifest_sha256')):
            registered['independent_validation']=review_agent_programs(
                run,experience_root,identity['scenario_id'])
        return registered
    from .failure_corpus import record_failures
    from .learning_review import review_failures,learning_snapshot,register_candidates
    from .timing_report import timing_report
    from .tracking_diagnostics import record_tracking_diagnostics
    from .planning_lessons import review_candidate_orders
    from .parameter_experience import review_run_parameters
    parameter_rows=[json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    review_run_parameters(run,experience_root,parameter_rows)
    # Separate content-addressed supplemental facts; do not rewrite an old
    # experience review or change its already-registered program identities.
    tracking = record_tracking_diagnostics(run, experience_root)
    timing_path=run/'timing-report.json'
    if not timing_path.exists():
        EvidenceStore(run).write_json(timing_path.name,timing_report(run))
    existing=run/'experience-review.json'
    if existing.is_file():
        import hashlib
        value=json.loads(existing.read_text())
        if value['source_sha256']!=hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError('reviewed_owner_trace_changed')
        review_candidate_orders(run, experience_root)
        facts=record_failures(run,experience_root)
        register_candidates(review_failures(facts,experience_root),experience_root)
        learning_snapshot(experience_root)
        return dict(value,agent_programs=agent_review())
    planning = review_candidate_orders(run, experience_root)
    failure_record=record_failures(run,experience_root)
    learning=review_failures(failure_record,experience_root)
    if learning:
        learning["diagnostic_snapshot"]=learning_snapshot(experience_root)
        learning["verifier_registration"]=register_candidates(learning,experience_root)
    rows=[json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    outcomes=[r for r in rows if r.get('kind')=='skill_result']
    checkpoints=[r for r in rows if r.get('kind')=='harness_motion_checkpoint']
    facts=[dict(skill=r.get('skill'),request_id=r.get('result',{}).get('request_id'),
                outcome=r.get('result',{}).get('outcome'),reason=r.get('result',{}).get('reason')) for r in outcomes]
    placements=[r for r in outcomes if r.get('skill')=='verify_placed']
    success=bool(placements and placements[-1]['result'].get('outcome')=='success'
                 and placements[-1]['result'].get('evidence_ids')
                 and not placements[-1]['result'].get('synthetic')
                 and not placements[-1]['result'].get('assisted')
                 and any(r.get('kind')=='owner_placement_verified' and r.get('request_id')==placements[-1]['result'].get('request_id') for r in rows))
    scene=digest([r for r in rows if r.get('kind')=='task_prepared'])
    candidates=[];memory=ExperienceStore(Path(experience_root))
    # A failed run is still useful diagnostic evidence, but it does not establish
    # that a recovery sequence works. Only observed successful recovery is proposed.
    if success and any(r.get('skill')=='insert' and r.get('outcome')=='failure' for r in facts):
        candidates.append(memory.propose(title='插入失败后重新观察并对齐',
            applies_when='insert returned failure; retreat and measured scene remain available',
            skills=['retreat','reobserve','align','insert'],discovery_run=str(run.resolve()),discovery_scene=scene,
            trigger={'skill':'insert','outcome':'failure'}))
    value=dict(schema='f1-harness-run-review-v1',run=str(run.resolve()),source=str(source),
               source_sha256=EvidenceStore(run).put_blob(source.read_bytes()),
               measured_placement_success=success,skill_results=facts,motion_checkpoints=checkpoints,
               candidate_ids=candidates,promoted=False,failure_record=failure_record,learning_review=learning,
               tracking_diagnostics=tracking,
               planning_order_review=planning,
               limits=['No automatic control-code or numeric-parameter edits',
                       'Failure reasons are evidence, not inferred physical causes',
                       'Independent held-out evidence is required before promotion'])
    EvidenceStore(run).event('experience_run_review',candidate_ids=candidates,promoted=False,
                             measured_placement_success=success)
    # Seal our host trace append before program verification hashes that trace.
    # The independent review writes only to the shared experience store.
    value['agent_programs']=agent_review()
    EvidenceStore(run).write_json('experience-review.json',value)
    return value
