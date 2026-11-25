"""Evidence-gated RPent corpus. Candidate text never becomes an execution instruction."""
from pathlib import Path
import json
import re

import yaml
from rpent.memory.manager import MemoryManager

from .contracts import HarnessError, SKILLS, digest
from .evidence import EvidenceStore


def observed_task_scope(rows):
    """Describe original owner evidence; never infer a task from failure prose."""
    tasks = [row.get('task') for row in rows if row.get('kind') == 'task_prepared']
    modes = [row.get('synthetic') for row in rows if row.get('kind') == 'run_identity']
    if len(tasks) != 1 or not isinstance(tasks[0], dict) or len(modes) != 1 or type(modes[0]) is not bool:
        return None
    task = tasks[0]
    fields = ('calibration_id', 'shape', 'goal', 'operator_instruction')
    if any(not isinstance(task.get(key), str) for key in fields):
        return None
    return dict(synthetic=modes[0], **{key: task[key] for key in fields})


class ExperienceStore:
    def __init__(self, root: Path, *, synthetic=False):
        self.root = root.resolve()
        self.synthetic = synthetic
        self.records = EvidenceStore(self.root / "review")
        self.manager = MemoryManager(self.root / "corpus", memory_access="read_only")

    def propose(self, *, title, applies_when, skills, discovery_run, discovery_scene, trigger=None,
                motion_parameters=None, calibration_id=None):
        if not skills or set(skills) - set(SKILLS):
            raise HarnessError("experience_only_registered_skills")
        # No arbitrary code or numeric motion parameters in the initial schema.
        value = dict(title=title, applies_when=applies_when, skills=list(skills),
                     discovery_run=discovery_run, discovery_scene=discovery_scene, synthetic=self.synthetic)
        if motion_parameters is not None:
            from .parameter_experience import validated_parameters
            parameters=validated_parameters(motion_parameters)
            if not parameters or not calibration_id or set(parameters)-set(skills):
                raise HarnessError('parameter_experience_scope_required')
            value.update(motion_parameters=parameters,calibration_id=calibration_id,
                         parameter_schema='f1-task-relative-motion-v1')
        if trigger is not None:
            if (set(trigger) != {'skill', 'outcome'} or trigger['skill'] not in SKILLS
                    or trigger['outcome'] not in ('failure', 'pending_review')):
                raise HarnessError('experience_registered_trigger_required')
            value['trigger'] = dict(trigger)
        key = digest(value)
        existing=self.records.root/(key+".candidate.json")
        if existing.exists():
            self._candidate(key)
            return key
        self.records.write_json(key + ".candidate.json", value)
        return key

    def _candidate(self, key):
        if not re.fullmatch("[a-f0-9]{64}", key):
            raise HarnessError("invalid_experience_id")
        value = json.loads((self.records.root / (key + ".candidate.json")).read_text())
        if digest(value) != key or value["synthetic"] != self.synthetic:
            raise HarnessError("experience_integrity_failure")
        if 'program' in value:
            from .recovery_programs import validate_program
            if validate_program(value['program']) != value.get('program_sha256'):
                raise HarnessError('experience_program_identity_changed')
        if 'agent_program' in value:
            from .program_experience import validate_candidate, invocation_code, PROGRAM_GOALS, audit_self_contained_program
            import hashlib
            program=value['agent_program']
            if (validate_candidate({k:program[k] for k in ('title','source','claim')})!=program
                    or value.get('program_goal') not in PROGRAM_GOALS
                    or hashlib.sha256(invocation_code(program).encode()).hexdigest()!=value.get('invocation_code_sha256')):
                raise HarnessError('experience_agent_program_identity_changed')
            audit_self_contained_program(program)
        return value

    def propose_agent_program(self, archive_path, *, goal, require_discovery_goal=False):
        """Register source-bound or measured-segment discovery for a local goal."""
        import hashlib
        from .program_experience import (PROGRAM_GOALS, validate_candidate, invocation_code, audit_self_contained_program,
            run_program_evidence, bound_program_invocations, observed_grasp_segment)
        path=Path(archive_path)
        archived=json.loads(path.read_text())
        if (goal not in PROGRAM_GOALS or path.name!=digest(archived)+'.json'
                or archived.get('schema')!='f1-agent-program-candidate-v1'
                or archived.get('origin') not in ('astra','owner_measured_segment')):
            raise HarnessError('invalid_agent_program_archive')
        program=archived['candidate']
        if validate_candidate({k:program[k] for k in ('title','source','claim')})!=program:
            raise HarnessError('experience_agent_program_identity_changed')
        audit_self_contained_program(program)
        evidence=run_program_evidence(archived['discovery_run'])
        identity=evidence['identity']
        derived=archived['origin']=='owner_measured_segment'
        if (hashlib.sha256(evidence['owner_bytes']).hexdigest()!=archived['owner_trace_sha256']
                or not derived and not any(hashlib.sha256(raw).hexdigest()==archived['planner_trace_sha256']
                                           for _,raw,_ in evidence['hosts'])):
            raise HarnessError('discovery_trace_changed')
        if (identity.get('synthetic') is not self.synthetic or not identity.get('scene_manifest_sha256')
                or not identity.get('scenario_id')):
            raise HarnessError('discovery_identity_or_mode_missing')
        if derived:
            observed=observed_grasp_segment(evidence)
            if (not observed or observed['program']!=program or observed['segment']!=archived.get('observed_segment')
                    or goal!=observed['segment']['goal']):
                raise HarnessError('observed_program_discovery_mismatch')
            used=set(observed['segment']['request_ids']);bound=[]
        else:
            bound=[r for r in bound_program_invocations(program,evidence)
                   if r.get('call_id')==archived['call_id'] and r['request_ids']]
            if not bound:raise HarnessError('agent_program_discovery_execution_unbound')
            used=set(bound[-1]['request_ids'])
        intents=[r['intent'] for r in evidence['owner_rows'] if r.get('kind')=='skill_accepted'
                 and r.get('intent',{}).get('request_id') in used]
        calibrations={i.get('calibration_id') for i in intents}
        task_hashes={i.get('task_hash') for i in intents}
        tasks=[r['task'] for r in evidence['owner_rows'] if r.get('kind')=='task_prepared'
               and r.get('task_hash') in task_hashes]
        if (len(calibrations)!=1 or None in calibrations or len(task_hashes)!=1 or len(tasks)!=1
                or tasks[0].get('shape') not in ('square','circle','triangle')):
            raise HarnessError('agent_program_discovery_scope_missing')
        from .program_verifier import same_scope, SCOPE
        if not all(same_scope(intents[0],intent) for intent in intents):
            raise HarnessError('agent_program_discovery_scope_missing')
        value=dict(title=program['title'], applies_when='applicable(current_numeric_scene)',
            skills=list(dict.fromkeys(i['skill'] for i in intents)), discovery_run=str(evidence['run']),
            discovery_scene=identity['scene_manifest_sha256'], discovery_scenario=identity['scenario_id'],
            discovery_source_sha256=self.records.put_blob(evidence['owner_bytes']),
            synthetic=self.synthetic, agent_program=program, program_goal=goal,
            discovery_scope={field:intents[0][field] for field in SCOPE},
            calibration_id=next(iter(calibrations)), shape=tasks[0]['shape'],
            operator_instruction=tasks[0].get('operator_instruction',''),
            invocation_code_sha256=hashlib.sha256(invocation_code(program).encode()).hexdigest(),
            source_archive_id=path.stem)
        if derived:
            value['discovery_binding']=dict(kind='owner_measured_segment',segment=observed['segment'],
                generated_source_executed_in_discovery=False)
        if identity.get('program_trial') is not None:
            value.update(discovery_scene_content_sha256=identity['scene_content_sha256'],
                         discovery_identity_source='owner_program_trial',generalization_proven=False)
        if require_discovery_goal and not derived:
            from .program_experience import agent_program_goal_match
            if not any(agent_program_goal_match(value,evidence,invocation) for invocation in bound):
                raise HarnessError('agent_program_discovery_goal_unproven')
        key=digest(value)
        if not (self.records.root/(key+'.candidate.json')).exists():
            self.records.write_json(key+'.candidate.json',value)
        else:self._candidate(key)
        return key

    def propose_program(self, lesson_path, *, skill):
        """Bind a quarantined failure lesson to a closed program and original trace."""
        import hashlib
        from .recovery_programs import validate_program
        path = Path(lesson_path)
        lesson = json.loads(path.read_text())
        if (path.name != digest(lesson) + '.candidate.json'
                or lesson.get('schema') != 'f1-program-lesson-candidate-v1'
                or lesson.get('execution_enabled') is not False
                or validate_program(lesson['program']) != lesson.get('program_sha256')
                or skill not in SKILLS or not any(r.get('skill') == skill for r in lesson['incidents'])):
            raise HarnessError('invalid_program_lesson')
        discovery = Path(lesson['source_run']).resolve()
        raw = (discovery / 'collected/owner/harness/trace.jsonl').read_bytes()
        if hashlib.sha256(raw).hexdigest() != lesson['source_sha256']:
            raise HarnessError('discovery_trace_changed')
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        identities = [r for r in rows if r.get('kind') == 'run_identity']
        # The source is diagnosis, not a success assertion, but cannot cross fake/real modes.
        if len(identities) != 1 or identities[0].get('synthetic') is not self.synthetic:
            raise HarnessError('discovery_identity_or_mode_missing')
        program = lesson['program']
        if program['trigger'].get('skill', skill) != skill:
            raise HarnessError('program_trigger_skill_mismatch')
        scene = identities[0].get('scene_manifest_sha256') or digest(
            [r for r in rows if r.get('kind') == 'task_prepared'])
        value = dict(title=lesson['title'], applies_when=program['trigger'],
            skills=program.get('skills', [skill]), discovery_run=str(discovery), discovery_scene=scene,
            synthetic=self.synthetic, program=program, program_sha256=lesson['program_sha256'],
            discovery_source_sha256=self.records.put_blob(raw), lesson_id=digest(lesson))
        key = digest(value)
        if not (self.records.root / (key + '.candidate.json')).exists():
            self.records.write_json(key + '.candidate.json', value)
        else:
            self._candidate(key)
        return key

    def verify(self, key, *, validation_run: Path, scenario_id: str):
        """Verifier entry point; not exposed to the planner or worker tool list."""
        receipt = self._evaluate(key, validation_run=validation_run, scenario_id=scenario_id)
        path = self.records.root / (key + '.review.json')
        from .program_experience import AGENT_PROGRAM_VERIFIER
        if receipt.get('verifier')==AGENT_PROGRAM_VERIFIER:
            # Rejected independent trials remain immutable evidence, not a
            # permanent veto against validating this exact program on a later
            # independent run. The first accepted review remains frozen.
            attempt=key+'.review-'+digest(receipt)+'.json'
            if not (self.records.root/attempt).exists():self.records.write_json(attempt,receipt)
            if receipt['accepted'] and not path.exists():self.records.write_json(path.name,receipt)
            return receipt
        if path.exists():
            if json.loads(path.read_text()) != receipt:
                raise HarnessError('experience_review_already_frozen')
        else:
            self.records.write_json(path.name, receipt)
        return receipt

    def _evaluate(self, key, *, validation_run: Path, scenario_id: str):
        candidate = self._candidate(key)
        if 'agent_program' in candidate:
            return self._evaluate_agent_program(key,candidate,validation_run=validation_run,scenario_id=scenario_id)
        run = validation_run.resolve()
        if str(run) == candidate["discovery_run"] or scenario_id == candidate["discovery_scene"]:
            raise HarnessError("independent_validation_required")
        trace = (run / "trace.jsonl").read_bytes()
        trace_sha = self.records.put_blob(trace)
        if trace_sha == candidate.get('discovery_source_sha256'):
            raise HarnessError('independent_validation_required')
        rows = [json.loads(line) for line in trace.splitlines()]
        identity = [r for r in rows if r["kind"] == "run_identity"]
        outcomes = [r for r in rows if r["kind"] == "skill_result" and r["skill"] == "verify_placed"]
        match = bool(identity and identity[0].get("scenario_id") == scenario_id
                     and identity[0].get("synthetic") == self.synthetic)
        success = bool(match and outcomes and outcomes[-1]["result"]["outcome"] == "success"
                       and outcomes[-1]["result"]["evidence_ids"]
                       and not outcomes[-1]["result"]["assisted"]
                       and outcomes[-1]["result"]["synthetic"] == self.synthetic)
        # Successful placement alone cannot validate an unrelated recovery.
        # Require the proposed sequence, in order, within this run, with no failed
        # or assisted action inside it and no intervening unrelated action.
        actions = [r for r in rows if r.get('kind') == 'skill_result']
        if outcomes:
            actions = actions[:actions.index(outcomes[-1]) + 1]
        matched_requests = []
        for start in range(len(actions) - len(candidate['skills']) + 1):
            sequence = actions[start:start + len(candidate['skills'])]
            trigger = candidate.get('trigger')
            triggered = trigger is None or any(
                r.get('skill') == trigger['skill']
                and r.get('result', {}).get('outcome') == trigger['outcome']
                for r in actions[:start])
            if (triggered and [r.get('skill') for r in sequence] == candidate['skills']
                    and all(r.get('result', {}).get('outcome') == 'success'
                            and not r['result'].get('assisted')
                            and r['result'].get('evidence_ids')
                            and r['result'].get('synthetic') == self.synthetic for r in sequence)):
                matched_requests = [r['result']['request_id'] for r in sequence]
                break
        program_match = None
        if 'program' in candidate:
            from .program_verifier import verify_program
            program_match = verify_program(candidate, rows, outcomes[-1], synthetic=self.synthetic) if outcomes else None
            matched_requests = program_match['request_ids'] if program_match else []
        success = success and bool(matched_requests)
        if 'motion_parameters' in candidate:
            from .parameter_experience import validates_execution
            success=success and validates_execution(candidate,rows,matched_requests)
        # A real experience requires the owner journal's measured placement verdict,
        # not an LLM's finish declaration or a standalone handwritten result.json.
        if not self.synthetic:
            success = (success and identity[0].get('heldout') is True
                       and bool(identity[0].get('scene_manifest_sha256'))
                       and identity[0]['scene_manifest_sha256'] != candidate['discovery_scene']
                       and any(r["kind"] == "owner_placement_verified"
                               and r.get("request_id") == outcomes[-1]["result"]["request_id"] for r in rows))
        receipt = dict(candidate_id=key, validation_run=str(run), scenario_id=scenario_id,
                       trace_sha256=trace_sha, accepted=bool(success),
                       synthetic=self.synthetic, verifier=("f1-parameter-verifier-v1" if 'motion_parameters' in candidate else
                           "f1-program-verifier-v3" if 'program' in candidate else "f1-evidence-verifier-v2"),
                       sequence_request_ids=matched_requests,
                       rejection_reason='' if success else 'placement_or_strategy_evidence_missing')
        if 'program' in candidate:
            receipt.update(program_sha256=candidate['program_sha256'], program_match=program_match)
        return receipt

    def _evaluate_agent_program(self, key, candidate, *, validation_run, scenario_id):
        from .program_experience import (AGENT_PROGRAM_VERIFIER, run_program_evidence,
                                        bound_program_invocations, agent_program_goal_match)
        evidence=run_program_evidence(validation_run)
        if candidate.get('discovery_binding',{}).get('kind')=='owner_measured_segment':
            from .program_experience import observed_program_discovery_match
            if not observed_program_discovery_match(candidate,run_program_evidence(candidate['discovery_run'])):
                raise HarnessError('observed_program_discovery_changed')
        identity=evidence['identity']
        trial=identity.get('program_trial')
        trace_sha=self.records.put_blob(evidence['owner_bytes'])
        if (str(evidence['run'])==candidate['discovery_run']
                or scenario_id==candidate['discovery_scenario']
                or trial is None and identity.get('scene_manifest_sha256')==candidate['discovery_scene']
                or trace_sha==candidate['discovery_source_sha256']):
            raise HarnessError('independent_validation_required')
        original=candidate['discovery_scope']
        if any(r.get('kind')=='skill_accepted' and all(r.get('intent',{}).get(field)==original[field]
               for field in ('owner_instance_id','session_id','epoch')) for r in evidence['owner_rows']):
            raise HarnessError('independent_validation_required')
        matches=[]
        if trial is not None:
            from .program_trials import trial_candidate
            if (trial['mode']!='validation' or trial['candidate']!=trial_candidate(candidate,key)
                    or trial['validation_scope']!='independent_run'):
                raise HarnessError('program_trial_candidate_mismatch')
        if (identity.get('synthetic') is self.synthetic and identity.get('scenario_id')==scenario_id
                and identity.get('scene_manifest_sha256')
                and (self.synthetic or identity.get('heldout') is True)):
            for invocation in bound_program_invocations(candidate['agent_program'],evidence):
                match=agent_program_goal_match(candidate,evidence,invocation)
                if match:matches.append(match)
        matched=matches[-1] if matches else None
        # Seal both journals. Later promotion re-evaluates the same owner and
        # host facts, including code and RPC blobs, rather than trusting a flag.
        for _,raw,_ in evidence['hosts']:self.records.put_blob(raw)
        receipt=dict(candidate_id=key,validation_run=str(evidence['run']),scenario_id=scenario_id,
            trace_sha256=trace_sha,accepted=bool(matched),synthetic=self.synthetic,
            verifier=AGENT_PROGRAM_VERIFIER,sequence_request_ids=matched['request_ids'] if matched else [],
            program_match=matched,program_source_sha256=candidate['agent_program']['source_sha256'],
            program_goal=candidate['program_goal'],
            rejection_reason='' if matched else 'agent_program_bound_goal_evidence_missing')
        if trial is not None:
            receipt.update(validation_scope='independent_run',generalization_proven=False,
                program_trial_sha256=trial['manifest_sha256'],
                initial_observation_sha256=trial['observation_sha256'],
                scene_content_sha256=trial['scene_content_sha256'],
                discovery_scene_content_sha256=candidate.get('discovery_scene_content_sha256'))
        return receipt

    def promote(self, key):
        candidate = self._candidate(key)
        review_path=self.records.root/(key+'.review.json')
        if not review_path.is_file():raise HarnessError('experience_not_verified')
        receipt = json.loads(review_path.read_text())
        if receipt["candidate_id"] != key or not receipt["accepted"] or receipt["synthetic"] != self.synthetic:
            raise HarnessError("experience_not_verified")
        from .program_experience import AGENT_PROGRAM_VERIFIER
        expected = (AGENT_PROGRAM_VERIFIER if 'agent_program' in candidate else
                    'f1-parameter-verifier-v1' if 'motion_parameters' in candidate else
                    'f1-program-verifier-v3' if 'program' in candidate else 'f1-evidence-verifier-v2')
        if receipt.get('verifier') != expected or not receipt.get('sequence_request_ids'):
            raise HarnessError('experience_requires_sequence_revalidation')
        current_path=Path(receipt['validation_run'])/('collected/owner/harness/trace.jsonl'
                     if 'agent_program' in candidate else 'trace.jsonl')
        current = current_path.read_bytes()
        if self.records.put_blob(current) != receipt["trace_sha256"]:
            raise HarnessError("validation_trace_changed")
        if ('program' in candidate or 'motion_parameters' in candidate or 'agent_program' in candidate) and self._evaluate(key, validation_run=Path(receipt['validation_run']),
                scenario_id=receipt['scenario_id']) != receipt:
            raise HarnessError('program_validation_changed')
        inbox = self.manager.root / "_internal/inbox" / key
        inbox.mkdir(parents=True, exist_ok=True)
        metadata = dict(id=key, scope="global", kind="strategy", title=candidate["title"],
                        applies_when=candidate["applies_when"], confidence="verified", skills=candidate["skills"],
                        evidence={"cells": [candidate["discovery_run"], receipt["validation_run"]]})
        if 'program' in candidate:
            metadata.update(program=candidate['program'], program_sha256=candidate['program_sha256'],
                            verifier=expected, review_sha256=digest(receipt))
        if 'motion_parameters' in candidate:
            metadata.update(motion_parameters=candidate['motion_parameters'],
                            calibration_id=candidate['calibration_id'],parameter_schema=candidate['parameter_schema'],
                            verifier=expected,review_sha256=digest(receipt))
        if 'agent_program' in candidate:
            metadata.update(agent_program=candidate['agent_program'],program_goal=candidate['program_goal'],
                calibration_id=candidate['calibration_id'],shape=candidate['shape'],
                operator_instruction=candidate.get('operator_instruction',''),
                invocation_code_sha256=candidate['invocation_code_sha256'],
                skills=receipt['program_match']['skills'],verifier=expected,review_sha256=digest(receipt))
            if candidate.get('discovery_binding'):
                metadata['source_origin']=candidate['discovery_binding']['kind']
            if receipt.get('validation_scope')=='independent_run':
                metadata.update(validation_scope='independent_run',generalization_proven=False,
                                program_trial_sha256=receipt['program_trial_sha256'])
        body = "\nValidated skill sequence: " + " → ".join(candidate["skills"]) + "\n"
        (inbox / (key + ".md")).write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n" + body)
        merged = self.manager.merge_memory(cell_tag=key, run_state_dir=Path(receipt["validation_run"]), solved=True)
        self.records.event("experience_promoted", candidate_id=key, review=digest(receipt), merge=merged)
        return self.snapshot()

    def snapshot(self):
        # Only canonical published notes enter the fixed snapshot.
        files = {p.name: p.read_text() for p in sorted((self.manager.root / "global").glob("*.md"))}
        value = dict(schema="f1-experience-snapshot-v1", synthetic=self.synthetic, files=files)
        identity = digest(value)
        path = self.root / (identity + ".snapshot.json")
        if not path.exists():
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        return dict(snapshot_id=identity, path=str(path), count=len(files))

    def advisory_context(self, task, *, limit=3, max_bytes=6000):
        """Recall related observed facts and code, separately from verified policy.

        Reuse the existing content-addressed evidence stores. RPent publication
        and independent program verification remain the only route to execution
        preferences; recurrence and model-written code never become authority.
        """
        import hashlib
        if not 1 <= limit <= 4 or not 1000 <= max_bytes <= 12000:
            raise ValueError('bounded_advisory_context_required')
        scope = dict(synthetic=self.synthetic, **{key: getattr(task, key)
                     for key in ('calibration_id', 'shape', 'goal', 'operator_instruction')})
        match_scope = {key: value for key, value in scope.items() if key != 'operator_instruction'}

        def matching_summary(source):
            if not isinstance(source, dict) or not isinstance(source.get('operator_instruction'), str):
                return None
            if {key: value for key, value in source.items() if key != 'operator_instruction'} != match_scope:
                return None
            return dict(match_scope, operator_instruction_sha256=hashlib.sha256(
                source['operator_instruction'].encode()).hexdigest())

        result = dict(schema='f1-experience-advisory-v1', scope=matching_summary(scope), failures=[], explorations=[],
            executable=False, cause_status='reported_codes_only_not_physical_root_cause')

        def append_bounded(key, value):
            result[key].append(value)
            if len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes:
                result[key].pop()
                return False
            return True

        folder = self.root/'failures'
        seen = set()
        for path in sorted(folder.glob('*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True):
            try:
                record = json.loads(path.read_text())
                source = record['source_sha256']
                if (path.stem != digest(record) or record.get('schema') != 'f1-failure-facts-v1'
                        or not re.fullmatch('[a-f0-9]{64}', source) or source in seen):
                    continue
                raw = (folder/'blobs'/source).read_bytes()
                if hashlib.sha256(raw).hexdigest() != source:
                    continue
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                summary_scope = matching_summary(observed_task_scope(rows))
                if summary_scope is None:
                    continue
                seen.add(source)
                facts = [{key: item.get(key) for key in ('skill', 'outcome', 'reason', 'request_id', 'count')}
                         for item in record['incidents'] if item.get('skill') in task.allowed_skills][:6]
                if not facts:
                    continue
                for fact in facts:
                    fact['reason'] = str(fact['reason'] or '')[:300]
                    fact['publication_recorded'] = any(row.get('kind') == 'harness_skill_first_publication'
                        and row.get('request_id') == fact['request_id'] for row in rows)
                entry = dict(id=path.stem, source_run=Path(record['run']).name,
                             source_trace_sha256=source, scope=summary_scope, facts=facts)
                if append_bounded('failures', entry) and len(result['failures']) >= limit:
                    break
            except (OSError, ValueError, TypeError, KeyError):
                continue
        folder = self.root/'exploration'
        explored = []
        for path in sorted(folder.glob('*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True):
            try:
                record = json.loads(path.read_text())
                summary_scope = matching_summary(record.get('scope'))
                if (path.stem != digest(record) or record.get('schema') != 'f1-exploration-cell-v1'
                        or summary_scope is None):
                    continue
                source = record['source_sha256']
                if not re.fullmatch('[a-f0-9]{64}', source):
                    continue
                raw = (folder/'blobs'/source).read_bytes()
                if hashlib.sha256(raw).hexdigest() != source:
                    continue
                entry = dict(id=path.stem, source_run=Path(record['run']).name,
                    source_sha256=source, owner_trace_sha256=record['owner_trace_sha256'],
                    call_id=record['call_id'], scope=summary_scope, observed_calls=record['observed_calls'][:8],
                    reusable_program=False, execution_binding='cell_receipt_only_not_self_contained_program')
                if len(raw) <= 2000:
                    entry['historical_code'] = raw.decode()
                else:
                    entry['code_omitted'] = 'Exact source retained locally; not a reusable program.'
                explored.append(((path.stat().st_mtime_ns, record.get('call_index', -1)), entry))
                if len(explored)>=32:break
            except (OSError, ValueError, TypeError, KeyError):
                continue
        for _, entry in sorted(explored, key=lambda pair: pair[0], reverse=True):
            if append_bounded('explorations', entry):
                break
        return result


def read_snapshot(path: Path, *, synthetic=False):
    value = json.loads(path.read_text())
    if (not isinstance(value,dict) or path.name != digest(value) + ".snapshot.json"
            or value.get("synthetic") is not synthetic or not isinstance(value.get('files'),dict)
            or any(not isinstance(k,str) or not isinstance(v,str) for k,v in value['files'].items())):
        raise HarnessError("snapshot_identity_or_mode_mismatch")
    return value
