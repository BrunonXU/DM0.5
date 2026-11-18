"""Archive agent-written programs, without treating generated code as verified policy.

The host may construct an explicit invocation for the existing PyRUA sandbox.
Native receipts, not a model's summary, establish outcomes.
"""
import ast
import hashlib
import json
from pathlib import Path

from .contracts import HarnessError, digest
from .evidence import EvidenceStore

PREDICATE_BUILTINS = {'abs', 'len', 'min', 'max', 'sum', 'all', 'any', 'bool', 'type',
                      'isinstance', 'dict', 'list', 'tuple', 'int', 'float'}


def validate_candidate(value):
    if not isinstance(value, dict) or set(value) != {'title', 'source', 'claim'}:
        raise ValueError('program_candidate_requires_title_source_claim')
    if any(not isinstance(value[k], str) or not value[k].strip() for k in value):
        raise ValueError('program_candidate_empty_field')
    if len(value['source'].encode()) > 48000 or len(value['claim']) > 2000 or len(value['title']) > 160:
        raise ValueError('program_candidate_too_large')
    tree = ast.parse(value['source'])
    # Loading a candidate must not move anything. Reject decorators, defaults,
    # annotations and top-level statements with definition-time side effects.
    if not tree.body or any(not isinstance(n, ast.FunctionDef) for n in tree.body):
        raise ValueError('program_candidate_definitions_only')
    names = [n.name for n in tree.body]
    if len(set(names)) != len(names) or not {'applicable', 'run_skill'} <= set(names):
        raise ValueError('program_candidate_entrypoints_required')
    if set(names) & PREDICATE_BUILTINS:
        raise ValueError('program_candidate_predicate_builtin_shadowed')
    if 'robo' in names or '_invoke_candidate' in names:
        raise ValueError('program_candidate_reserved_name')
    for node in tree.body:
        args = node.args
        if (node.decorator_list or args.defaults or any(v is not None for v in args.kw_defaults)
                or node.returns or any(a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs)
                or (args.vararg and args.vararg.annotation) or (args.kwarg and args.kwarg.annotation)):
            raise ValueError('program_candidate_definition_side_effect')
        if node.name in ('applicable', 'run_skill') and (len(args.args) != 1 or args.posonlyargs
                or args.kwonlyargs or args.vararg or args.kwarg):
            raise ValueError('program_candidate_entrypoint_signature')
    # Eligibility is a pure expression over supplied numeric state. It cannot
    # invoke a robot, mutate state or grant itself execution authority.
    predicate = next(n for n in tree.body if n.name == 'applicable')
    body = predicate.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        raise ValueError('program_candidate_pure_predicate_required')
    state_name = predicate.args.args[0].arg
    builtins = PREDICATE_BUILTINS
    for node in ast.walk(body[0]):
        if isinstance(node, (ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            raise ValueError('program_candidate_impure_predicate')
        if isinstance(node, ast.Name) and node.id not in builtins | {state_name}:
            raise ValueError('program_candidate_predicate_external_name')
        if isinstance(node, ast.Attribute) and node.attr != 'get':
            raise ValueError('program_candidate_predicate_attribute')
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in builtins
                or isinstance(node.func, ast.Attribute) and node.func.attr == 'get'):
            raise ValueError('program_candidate_predicate_call')
    return dict(value, source_sha256=digest(value['source']))


def invocation_code(candidate):
    """Build code, never evaluate it on the host or inherit earlier cell globals."""
    validated = validate_candidate({key: candidate[key] for key in ('title', 'source', 'claim')})
    if validated != candidate:
        raise ValueError('program_candidate_identity_changed')
    # The worker supplies a fresh ArmToolkit namespace. Do not bypass PyRUA's
    # source inspection or guarded builtins with dynamic execution here.
    return (candidate['source'].rstrip() + '\n\ndef _invoke_candidate(robot):\n'
            '    scene = robot.geometry()\n'
            '    eligible = applicable(scene)\n'
            '    if type(eligible) is not bool:\n'
            '        raise ValueError("program_applicable_must_return_bool")\n'
            '    if not eligible:\n'
            '        return {"applicable": False, "program_invoked": False}\n'
            '    return {"applicable": True, "program_invoked": True,\n'
            '            "result": run_skill(robot)}\n'
            '_invoke_candidate(robo)\n')


def record_candidate(store, value, *, call_id, request_id):
    """Untrusted proposals are supplementary: errors never hide action receipts."""
    try:
        candidate = validate_candidate(value)
    except (ValueError, TypeError, SyntaxError) as exc:
        store.event('program_candidate_rejected', call_id=call_id, reason=str(exc)[:200])
        return dict(accepted=False, reason=str(exc)[:200])
    store.event('program_candidate_proposed', call_id=call_id, request_id=request_id,
                candidate=candidate, origin='astra', verified=False, executable_default=False)
    return dict(accepted=True, source_sha256=candidate['source_sha256'], verified=False)


def archive_run_programs(run, experience_root):
    """Export exact generated source with bound traces, even on partial failure.

    This deliberately does not map a failure code to a predefined solution or
    infer that a proposal was executed simply because the episode succeeded.
    """
    run = Path(run)
    owner = run / 'collected/owner/harness/trace.jsonl'
    if not owner.is_file():
        return []
    owner_bytes = owner.read_bytes()
    owner_rows = [json.loads(line) for line in owner_bytes.splitlines() if line.strip()]
    results = [dict(skill=r.get('skill'), result=r['result']) for r in owner_rows
               if r.get('kind') == 'skill_result']
    calibrations = sorted({r['intent']['calibration_id'] for r in owner_rows
                           if r.get('kind') == 'skill_accepted' and r.get('intent', {}).get('calibration_id')})
    target = EvidenceStore(Path(experience_root) / 'program-candidates')
    owner_sha = target.put_blob(owner_bytes)
    archived = []
    for trace in (run / 'trace.jsonl', run / 'planner/trace.jsonl'):
        if not trace.is_file():
            continue
        trace_bytes = trace.read_bytes()
        trace_sha = target.put_blob(trace_bytes)
        rows = [json.loads(line) for line in trace_bytes.splitlines() if line.strip()]
        for row in rows:
            if row.get('kind') != 'program_candidate_proposed':
                continue
            candidate = validate_candidate({k: row['candidate'][k] for k in ('title', 'source', 'claim')})
            if candidate != row['candidate']:
                raise ValueError('program_candidate_identity_changed')
            item = dict(schema='f1-agent-program-candidate-v1', candidate=candidate,
                        origin='astra', discovery_run=str(run.resolve()), call_id=row['call_id'],
                        owner_trace_sha256=owner_sha, planner_trace_sha256=trace_sha,
                        calibration_ids=calibrations,
                        observed_skill_results=results, verified=False, executable_default=False,
                        execution_of_proposed_source='unverified')
            invocations = [r for r in rows if r.get('kind') == 'program_execution_finished'
                           and r.get('call_id') == row['call_id']
                           and r.get('source_sha256') == candidate['source_sha256']
                           and r.get('origin') == 'sandbox_host']
            if invocations:
                item['execution_bindings'] = invocations
                item['execution_of_proposed_source'] = 'host_bound_invocation'
                for invocation in invocations:
                    refs = [invocation['source_blob_sha256'], invocation['code_sha256']]
                    for call in invocation['backend_calls']:
                        refs.extend(call[k] for k in ('input_sha256','result_sha256','request_chain_sha256') if k in call)
                    for ref in refs:
                        target.put_blob(_read_blob(trace.parent,ref))
            key = digest(item)
            path = target.root / (key + '.json')
            if not path.exists():
                target.write_json(path.name, item)
            elif json.loads(path.read_text()) != item:
                raise ValueError('program_candidate_archive_conflict')
            archived.append(key)
    return sorted(set(archived))


def archive_exploration_cells(run, experience_root):
    """Preserve actual model cells without requiring optional proposal metadata.

    This is an execution transcript, not a manufactured callable, successful
    recipe or binding for program promotion. A future self-contained candidate
    must still use the existing sandbox execution and independent verifier.
    """
    from .memory import observed_task_scope
    run = Path(run)
    owner_path = run/'collected/owner/harness/trace.jsonl'
    planner_path = run/'planner/trace.jsonl'
    if not owner_path.is_file() or not planner_path.is_file():
        return []
    owner_raw, planner_raw = owner_path.read_bytes(), planner_path.read_bytes()
    owner_rows = [json.loads(line) for line in owner_raw.splitlines() if line.strip()]
    planner_rows = [json.loads(line) for line in planner_raw.splitlines() if line.strip()]
    scope = observed_task_scope(owner_rows)
    store = None
    archived = []
    results = {row.get('call_id'): row for row in planner_rows if row.get('kind') == 'planner_tool_result'}
    for call_index, row in enumerate(planner_rows):
        if row.get('kind') != 'planner_tool_call' or row.get('name') != 'python':
            continue
        source = row.get('arguments', {}).get('code')
        if not isinstance(source, str) or not source.strip():
            continue
        if store is None:
            store = EvidenceStore(Path(experience_root)/'exploration')
            owner_sha, planner_sha = store.put_blob(owner_raw), store.put_blob(planner_raw)
        result = results.get(row.get('call_id'), {}).get('result', {})
        calls = []
        for call in result.get('primitive_calls', []):
            if not isinstance(call, dict):continue
            summary = {key: call[key] for key in ('tool', 'skill', 'request_id', 'outcome', 'reason',
                       'ok', 'accepted', 'planned', 'no_motion') if key in call}
            if call.get('tool') in ('execute_skill', 'inspect_plan', 'preview_skill'):
                summary['skill'] = (call.get('kwargs') or {}).get('name')
            calls.append(summary)
        item = dict(schema='f1-exploration-cell-v1', run=str(run.resolve()), scope=scope,
            model_request_id=row.get('request_id'), call_id=row.get('call_id'), call_index=call_index,
            source_sha256=store.put_blob(source.encode()), owner_trace_sha256=owner_sha,
            planner_trace_sha256=planner_sha, observed_calls=calls,
            tool_result_recorded=row.get('call_id') in results,
            exception_recorded=bool(result.get('exception')), reusable_program=False,
            execution_binding='cell_receipt_only_not_self_contained_program',
            verified=False, executable_default=False)
        key = digest(item)
        path = store.root/(key+'.json')
        if not path.exists():
            store.write_json(path.name, item)
        elif json.loads(path.read_text()) != item:
            raise ValueError('exploration_archive_conflict')
        archived.append(key)
    return sorted(set(archived))


def reference_programs(root, calibration_id, *, limit=4):
    """Return a bounded read-only hypothesis snapshot, never an execution choice."""
    entries = []
    directory = Path(root) / 'program-candidates'
    if not directory.is_dir():
        return dict(id=digest([]), entries=[], executable_default=False)
    for path in sorted(directory.glob('*.json')):
        try:
            item = json.loads(path.read_text())
            if path.stem != digest(item) or item['calibration_ids'] != [calibration_id]:
                continue
            candidate = item['candidate']
            if validate_candidate({k:candidate[k] for k in ('title','source','claim')}) != candidate:
                continue
            entries.append(dict(id=path.stem, candidate=candidate,
                                verified=False, execution_of_proposed_source=item['execution_of_proposed_source'],
                                observed_skill_results=item['observed_skill_results'][-12:]))
        except (ValueError, TypeError, KeyError, SyntaxError, OSError):
            continue
    entries = entries[:limit]
    return dict(id=digest(entries), entries=entries, executable_default=False)


AGENT_PROGRAM_VERIFIER = 'f1-agent-python-verifier-v1'
PROGRAM_GOALS = {'placed': 'verify_placed', 'verify_grasp': 'verify_grasp'}

PURE_PROGRAM_BUILTINS = frozenset(('abs','all','any','bool','dict','enumerate','filter','float',
    'int','isinstance','iter','len','list','map','max','min','next','print','range','repr','reversed',
    'round','set','slice','sorted','str','sum','tuple','type','zip',
    'Exception','ValueError','TypeError','RuntimeError','ArithmeticError','ZeroDivisionError',
    'IndexError','KeyError','StopIteration','AssertionError'))
PROGRAM_ESCAPE_NAMES = frozenset(('open','eval','exec','compile','globals','locals','vars',
    'getattr','setattr','delattr','hasattr','dir','__import__','breakpoint','input',
    'os','pathlib','sys','builtins','importlib','inspect'))
PROGRAM_ESCAPE_ATTRIBUTES = frozenset(('backend','ledger','toolkit','primitives','mro',
    'gi_frame','gi_code','ag_frame','ag_code','cr_frame','cr_code','f_globals','f_locals',
    'f_builtins','f_back','f_code','tb_frame','tb_next','func_globals','func_code'))
MATH_PROGRAM_EXPORTS = frozenset(('acos','acosh','asin','asinh','atan','atan2','atanh','ceil',
    'comb','copysign','cos','cosh','degrees','dist','e','erf','erfc','exp','exp2','expm1',
    'fabs','factorial','floor','fmod','frexp','fsum','gamma','gcd','hypot','inf','isclose',
    'isfinite','isinf','isnan','isqrt','lcm','ldexp','lgamma','log','log10','log1p','log2',
    'modf','nan','nextafter','perm','pi','pow','prod','radians','remainder','sin','sinh',
    'sqrt','tan','tanh','tau','trunc','ulp'))


def audit_self_contained_program(program):
    """Admit frozen policy source with no mutable files or prior-cell dependencies.

    Exploration metadata is intentionally broader. This check belongs to
    registration, verification and frozen lookup, never a robot motion gate.
    """
    import symtable
    validate_candidate({k:program[k] for k in ('title','source','claim')})
    tree=ast.parse(program['source'])
    functions={node.name for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node,(ast.Global,ast.ClassDef,ast.AsyncFunctionDef)):
            raise HarnessError('agent_program_self_contained_functions_required')
        names=[]
        if isinstance(node,ast.Name):names=[node.id]
        elif isinstance(node,(ast.FunctionDef,ast.arg)):names=[node.name if isinstance(node,ast.FunctionDef) else node.arg]
        elif isinstance(node,ast.alias):names=[node.asname or node.name]
        if any(name in PROGRAM_ESCAPE_NAMES or name.startswith('__') for name in names):
            raise HarnessError('agent_program_external_or_reflective_dependency')
        if isinstance(node,ast.Attribute) and (node.attr.startswith('_')
                or node.attr in PROGRAM_ESCAPE_ATTRIBUTES or node.attr in PROGRAM_ESCAPE_NAMES
                or isinstance(node.ctx,(ast.Store,ast.Del))):
            raise HarnessError('agent_program_external_or_reflective_dependency')
        if isinstance(node,ast.Constant) and isinstance(node.value,str) and '__' in node.value:
            raise HarnessError('agent_program_reflective_string_dependency')
        if isinstance(node,ast.Import) and any(alias.name!='math' for alias in node.names):
            raise HarnessError('agent_program_math_imports_only')
        if isinstance(node,ast.ImportFrom) and (node.level or node.module!='math'
                or any(alias.name not in MATH_PROGRAM_EXPORTS for alias in node.names)):
            raise HarnessError('agent_program_math_imports_only')
    table=symtable.symtable(program['source'],'<agent-program>','exec')
    pending=list(table.get_children())
    while pending:
        scope=pending.pop();pending.extend(scope.get_children())
        for symbol in scope.get_symbols():
            if (symbol.is_referenced() and symbol.is_global()
                    and symbol.get_name() not in functions|PURE_PROGRAM_BUILTINS):
                raise HarnessError('agent_program_prior_namespace_dependency:'+symbol.get_name())
    return program['source_sha256']


def _read_blob(root, sha):
    import re
    if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
        raise HarnessError('program_evidence_blob_identity_invalid')
    value = (Path(root)/'blobs'/sha).read_bytes()
    if hashlib.sha256(value).hexdigest()!=sha:
        raise HarnessError('program_evidence_blob_changed')
    return value


def run_program_evidence(run):
    """Read sealed workspace host and owner journals; no policy text is executed."""
    run = Path(run).resolve()
    owner = run/'collected/owner/harness/trace.jsonl'
    if not owner.is_file():
        raise HarnessError('agent_program_collected_owner_trace_required')
    raw = owner.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    identities = [r for r in rows if r.get('kind')=='run_identity']
    if len(identities)!=1:
        raise HarnessError('agent_program_owner_identity_required')
    from .program_trials import program_identity
    identity=program_identity(rows,identities[0])
    hosts = []
    for path in (run/'trace.jsonl', run/'planner/trace.jsonl'):
        if path.is_file():
            data=path.read_bytes()
            hosts.append((path.parent,data,[json.loads(x) for x in data.splitlines() if x.strip()]))
    return dict(run=run, owner_bytes=raw, owner_rows=rows, identity=identity, hosts=hosts)


def observed_grasp_segment(evidence):
    """Compile only an observed successful prefix, never invent a strategy.

    Ordinary model cells need not submit optional source metadata. Their owner
    journal can establish a local result even when the later task fails. This
    describes an executed sequence; the generated Python itself has NOT yet
    been executed. Its first source-bound execution is an independent trial.
    """
    from .contracts import MotionParameters
    from .program_verifier import same_scope, successful, SCOPE
    rows=evidence['owner_rows']; mode=evidence['identity'].get('synthetic')
    if type(mode) is not bool:raise HarnessError('observed_program_owner_mode_missing')
    end=next((i for i,r in enumerate(rows) if r.get('kind')=='skill_result'
              and r.get('skill')=='verify_grasp' and successful(r,mode)),None)
    if end is None:return None
    prefix=rows[:end+1]
    accepted=[(i,r['intent']) for i,r in enumerate(prefix) if r.get('kind')=='skill_accepted']
    if not accepted or len(accepted)>16:raise HarnessError('observed_program_prefix_not_bounded')
    first,scope=accepted[0]
    if any(r.get('kind') in ('authority_revoked','web_authorization','task_prepared')
           for r in prefix[first+1:]):raise HarnessError('observed_program_scope_interrupted')
    ids=[];calls=[];publication_count=0
    for ordinal,(start,intent) in enumerate(accepted):
        request=intent.get('request_id');skill=intent.get('skill')
        if not request or request in ids or not same_scope(scope,intent):
            raise HarnessError('observed_program_scope_changed')
        if (skill=='move_tcp' or intent.get('resume_request_id') or intent.get('verification_request_id')
                or intent.get('candidate_start',0)!=0 or intent.get('excluded_candidates',[])):
            raise HarnessError('observed_program_context_dependent_call')
        if sum(r.get('kind')=='skill_accepted' and r.get('intent',{}).get('request_id')==request
               for r in rows)!=1:raise HarnessError('observed_program_request_ambiguous')
        results=[(i,r) for i,r in enumerate(rows) if r.get('kind')=='skill_result'
                 and r.get('result',{}).get('request_id')==request]
        if len(results)!=1:raise HarnessError('observed_program_result_ambiguous')
        finish,result=results[0];reply=result['result']
        if (finish<=start or ordinal+1<len(accepted) and finish>=accepted[ordinal+1][0]
                or result.get('skill')!=skill or reply.get('lifecycle')!='completed'
                or not successful(result,mode) or reply.get('cancel_stage','none')!='none'
                or reply.get('resume_request_id') or reply.get('verification_request_id')):
            raise HarnessError('observed_program_prefix_not_successful')
        published=any(r.get('kind')=='harness_skill_first_publication' and r.get('request_id')==request
                      for r in prefix[start+1:finish])
        if type(reply.get('no_motion')) is not bool or reply['no_motion']==published:
            raise HarnessError('observed_program_publication_inconsistent')
        parameters=MotionParameters.model_validate(intent.get('parameters',{})).for_skill(skill).model_dump(
            mode='json',exclude_none=True,exclude_defaults=True)
        if parameters.get('target_tcp_pose') or parameters.get('via_tcp_poses'):
            raise HarnessError('observed_program_recorded_world_target')
        calls.append(dict(name=skill,parameters=parameters));ids.append(request)
        publication_count+=int(published)
    if (calls[-1]['name']!='verify_grasp' or ids[-1]!=prefix[-1]['result']['request_id']
            or not publication_count):raise HarnessError('observed_program_local_goal_unproven')
    source=['def applicable(scene):',
            '    return scene.get("available") is True or scene.get("object_pose") is not None','',
            'def run_skill(robo):']
    for index,call in enumerate(calls):
        arguments=repr(call['name'])+''.join(', '+key+'='+repr(value) for key,value in sorted(call['parameters'].items()))
        source.append('    result = robo.skill('+arguments+')')
        if index<len(calls)-1:
            source.extend(['    if result.get("outcome") != "success" or result.get("lifecycle") != "completed":',
                           '        return result'])
    source.append('    return result')
    program=validate_candidate(dict(title='Observed local grasp sequence',
        claim='Replay the observed task-relative calls and independently verify grasp; placement and upright manipulation are not claimed.',
        source='\n'.join(source)+'\n'))
    segment=dict(schema='f1-owner-measured-segment-v1',goal='verify_grasp',request_ids=ids,calls=calls,
        scope={key:scope[key] for key in SCOPE},goal_request_id=ids[-1],
        owner_trace_sha256=hashlib.sha256(evidence['owner_bytes']).hexdigest())
    return dict(program=program,segment=segment)


def observed_program_discovery_match(candidate,evidence):
    """Recompute derived-source provenance without claiming sandbox execution."""
    origin=candidate.get('discovery_binding',{})
    if origin.get('kind')!='owner_measured_segment':return None
    try:observed=observed_grasp_segment(evidence)
    except (HarnessError,ValueError,TypeError,KeyError):return None
    if (not observed or observed['program']!=candidate['agent_program']
            or observed['segment']!=origin.get('segment')
            or candidate['program_goal']!=observed['segment']['goal']):return None
    segment=observed['segment']
    return dict(request_ids=segment['request_ids'],goal_request_id=segment['goal_request_id'],
        skills=list(dict.fromkeys(c['name'] for c in segment['calls'])),
        discovery_binding='owner_measured_segment',generated_source_executed_in_discovery=False)


def archive_observed_grasp_program(evidence,root):
    observed=observed_grasp_segment(evidence)
    if observed is None:return None
    target=EvidenceStore(Path(root)/'program-candidates')
    owner_sha=target.put_blob(evidence['owner_bytes'])
    item=dict(schema='f1-agent-program-candidate-v1',candidate=observed['program'],
        origin='owner_measured_segment',discovery_run=str(evidence['run']),
        owner_trace_sha256=owner_sha,observed_segment=observed['segment'],
        verified=False,executable_default=False,
        execution_of_proposed_source='compiled_from_observed_calls_not_executed_as_source')
    key=digest(item);path=target.root/(key+'.json')
    if not path.exists():target.write_json(path.name,item)
    elif json.loads(path.read_text())!=item:raise HarnessError('observed_program_archive_conflict')
    return key


def bound_program_invocations(program, evidence):
    """Match exact host-dispatched code, RPC receipts and immutable blobs."""
    code=invocation_code(program).encode()
    expected_code=hashlib.sha256(code).hexdigest()
    identity_keys=('invocation_id','source_sha256','source_blob_sha256','code_sha256',
                   'call_id','model_request_id','origin')
    found=[]
    for root,raw,rows in evidence['hosts']:
        for finish_index,finish in enumerate(rows):
            if (finish.get('kind')!='program_execution_finished' or finish.get('origin')!='sandbox_host'
                    or finish.get('source_sha256')!=program['source_sha256']
                    or finish.get('code_sha256')!=expected_code):
                continue
            invocation=finish.get('invocation_id')
            starts=[(i,r) for i,r in enumerate(rows) if r.get('kind')=='program_execution_started'
                    and r.get('invocation_id')==invocation]
            if (not invocation or len(starts)!=1 or starts[0][0]>=finish_index
                    or any(starts[0][1].get(k)!=finish.get(k) for k in identity_keys)
                    or sum(r.get('kind')=='program_execution_finished' and r.get('invocation_id')==invocation
                           for r in rows)!=1):
                continue
            try:
                if (_read_blob(root,finish['source_blob_sha256'])!=program['source'].encode()
                        or _read_blob(root,finish['code_sha256'])!=code):
                    continue
                between=rows[starts[0][0]+1:finish_index]
                calls=[]
                for ordinal,call in enumerate(finish['backend_calls']):
                    if call['ordinal']!=ordinal:
                        raise ValueError('backend_call_order_changed')
                    matches=[(i,r) for i,r in enumerate(between) if r.get('invocation_id')==invocation
                             and r.get('ordinal')==ordinal]
                    sent=[(i,r) for i,r in matches if r.get('kind')=='program_backend_call']
                    returned=[(i,r) for i,r in matches if r.get('kind')=='program_backend_result']
                    if (len(sent)!=1 or len(returned)!=1 or sent[0][0]>=returned[0][0]
                            or any(returned[0][1].get(k)!=v for k,v in call.items())
                            or sent[0][1].get('name')!=call['name']
                            or sent[0][1].get('input_sha256')!=call['input_sha256']):
                        raise ValueError('backend_receipt_incomplete')
                    input_value=json.loads(_read_blob(root,call['input_sha256']))
                    result=json.loads(_read_blob(root,call['result_sha256']))
                    if call.get('request_id')!=(result.get('request_id') if isinstance(result,dict) else None):
                        raise ValueError('backend_request_identity_changed')
                    bound = dict(call, arguments=input_value, result=result)
                    if 'request_chain_sha256' in call:
                        bound['request_chain'] = json.loads(_read_blob(root,call['request_chain_sha256']))
                    calls.append(bound)
                if finish['request_ids']!=[c['request_id'] for c in calls if 'request_id' in c]:
                    continue
                found.append(dict(finish, calls=calls, host_trace_sha256=hashlib.sha256(raw).hexdigest()))
            except (OSError,ValueError,KeyError,TypeError,HarnessError):
                continue
    return found


def agent_program_goal_match(candidate, evidence, invocation):
    """A goal must be the independently measured owner result of this invocation."""
    from .program_verifier import same_scope, successful
    if invocation['status']!='cell_returned':return None
    rows=evidence['owner_rows']
    accepted={}; results={}
    for index,row in enumerate(rows):
        if row.get('kind')=='skill_accepted':
            intent=row.get('intent',{}); request=intent.get('request_id')
            if not request or request in accepted:return None
            accepted[request]=(index,intent)
        elif row.get('kind')=='skill_result':
            request=row.get('result',{}).get('request_id')
            if not request or request in results:return None
            results[request]=(index,row)
    calls=[]
    for public in invocation['calls']:
        if public['name']!='execute_skill':continue
        chain=public.get('request_chain')
        if chain is None:
            calls.append(public)
            continue
        if not isinstance(chain,list) or not chain:return None
        # A chain is host-captured, immutable provenance. Each earlier attempt
        # must be a known terminal zero-publication failure, and the last owner
        # receipt must be the same public response. Unrelated owner requests
        # remain rejected by the full ordered journal comparison below.
        for index,attempt in enumerate(chain):
            if not isinstance(attempt,dict):return None
            intent=attempt.get('intent');reply=attempt.get('result')
            if not isinstance(intent,dict) or not isinstance(reply,dict):return None
            request=reply.get('request_id')
            if not request or intent.get('request_id')!=request:return None
            if intent.get('skill')!=public['arguments'].get('name'):return None
            if index<len(chain)-1:
                if (reply.get('no_motion') is not True or reply.get('lifecycle') not in ('completed','rejected')
                        or reply.get('outcome') not in ('failure','pending_review')
                        or reply.get('resume_request_id') or reply.get('verification_request_id')):return None
            else:
                if request!=public.get('request_id'):return None
                for key in ('request_id','lifecycle','outcome','reason','evidence_ids','synthetic','assisted',
                            'no_motion','retryable','cancel_stage','resume_request_id','verification_request_id',
                            'rejected_candidate_variant','candidate_variant'):
                    if reply.get(key)!=public['result'].get(key):return None
            if request in accepted and accepted[request][1]!=intent:return None
            calls.append(dict(public,request_id=request,result=reply))
    ids=[]
    for call in calls:
        request=call.get('request_id')
        if not request or request in ids:return None
        if request not in accepted:
            # RemoteTaskBackend can reject a missing-scene request before owner
            # submission. Keep that host receipt as diagnostic evidence; it is
            # not a physical action and must not prohibit a later valid retry.
            reply=call['result']
            if (reply.get('no_motion') is True and reply.get('lifecycle')=='rejected'
                    and reply.get('outcome') in ('failure','pending_review')
                    and not reply.get('resume_request_id') and not reply.get('verification_request_id')):
                continue
            return None
        if request not in results:return None
        start,intent=accepted[request];end,result=results[request]
        if (start>=end or intent.get('skill')!=call['arguments'].get('name')
                or result.get('skill')!=intent.get('skill')
                or intent.get('calibration_id')!=candidate['calibration_id']):return None
        # A matching skill name alone cannot prove that the bound program's
        # relative choices were the ones submitted to the owner.
        try:
            from .contracts import MotionParameters
            if (MotionParameters.model_validate(intent.get('parameters',{}))
                    !=MotionParameters.model_validate(call['arguments'].get('parameters',{}))):return None
        except (ValueError,TypeError):return None
        for key in ('request_id','lifecycle','outcome','reason','evidence_ids','synthetic','assisted',
                    'no_motion','retryable','cancel_stage','resume_request_id','verification_request_id',
                    'rejected_candidate_variant','candidate_variant'):
            if result['result'].get(key)!=call['result'].get(key):return None
        if (result['result'].get('lifecycle') not in ('completed','rejected')
                or result['result'].get('assisted') is not False
                or result['result'].get('synthetic') is not candidate['synthetic']):return None
        ids.append(request)
    if not ids:return None
    scope=accepted[ids[0]][1]
    if any(not same_scope(scope,accepted[q][1]) for q in ids):return None
    tasks=[r for r in rows if r.get('kind')=='task_prepared' and r.get('task_hash')==scope['task_hash']]
    if (len(tasks)!=1 or tasks[0].get('task',{}).get('shape')!=candidate['shape']
            or tasks[0].get('task',{}).get('operator_instruction','')
               !=candidate.get('operator_instruction','')):return None
    goal=PROGRAM_GOALS[candidate['program_goal']]
    goals=[q for q in ids if results[q][1]['skill']==goal and successful(results[q][1],candidate['synthetic'])]
    if not goals:return None
    verdict=goals[-1]; first=accepted[ids[0]][0]; last=results[ids[-1]][0]
    following=ids[ids.index(verdict)+1:]
    if following and not (candidate['program_goal']=='placed' and len(following)==1
                          and results[following[0]][1]['skill']=='withdraw_reset'
                          and successful(results[following[0]][1],candidate['synthetic'])):
        return None
    # Calls are sequential. An unrelated owner operation or authorization change
    # cannot supply the missing progress of an otherwise inert program.
    owner_ids=[r['intent']['request_id'] for r in rows[first:last+1] if r.get('kind')=='skill_accepted']
    if owner_ids!=ids or any(r.get('kind') in ('authority_revoked','web_authorization','task_prepared')
                            for r in rows[first+1:last+1]):return None
    if not any(r.get('kind')=='harness_skill_first_publication' and r.get('request_id') in ids
               for r in rows[first:results[verdict][0]]):return None
    if (not candidate['synthetic'] and goal=='verify_placed' and not any(
            r.get('kind')=='owner_placement_verified' and r.get('request_id')==verdict for r in rows)):
        return None
    return dict(invocation_id=invocation['invocation_id'], goal_request_id=verdict,
                request_ids=ids, skills=list(dict.fromkeys(accepted[q][1]['skill'] for q in ids)),
                host_trace_sha256=invocation['host_trace_sha256'])


def validated_agent_programs(snapshot, task, *, goal=None, synthetic=False):
    """Query only frozen, promoted Python programs; never select or execute one."""
    import yaml
    if not snapshot or snapshot.get('synthetic') is not synthetic:return []
    selected=[]; goal=goal or task.goal
    if goal not in PROGRAM_GOALS:return []
    for name,text in snapshot.get('files',{}).items():
        try:
            parts=text.split('---',2)
            if len(parts)!=3 or parts[0].strip():continue
            meta=yaml.safe_load(parts[1])
            program=meta['agent_program']
            audit_self_contained_program(program)
            if (meta.get('confidence')!='verified' or meta.get('verifier')!=AGENT_PROGRAM_VERIFIER
                    or not meta.get('review_sha256') or meta.get('program_goal')!=goal
                    or meta.get('calibration_id')!=task.calibration_id or meta.get('shape')!=task.shape
                    or meta.get('operator_instruction','')!=getattr(task,'operator_instruction','')
                    or not meta.get('skills') or set(meta['skills'])-set(task.allowed_skills)
                    or validate_candidate({k:program[k] for k in ('title','source','claim')})!=program
                    or hashlib.sha256(invocation_code(program).encode()).hexdigest()!=meta.get('invocation_code_sha256')):
                continue
            selected.append(dict(id=meta['id'],candidate={k:program[k] for k in ('title','source','claim')},
                goal=goal,source_sha256=program['source_sha256'],review_sha256=meta['review_sha256'],
                skills=meta['skills']))
        except (ValueError,KeyError,TypeError,SyntaxError,yaml.YAMLError,HarnessError):
            continue
    return sorted(selected,key=lambda item:item['id'])


def pending_agent_programs(root, task, *, synthetic=False, limit=1):
    """Freeze successful discovery source for an independent new-run trial.

    This does not promote anything, synthesize a held-out layout, or execute
    source. Recheck its original measured invocation before offering it.
    """
    from .memory import ExperienceStore
    from .program_trials import trial_candidate
    if type(limit) is not int or not 1<=limit<=4:
        raise ValueError('bounded_program_trial_selection_required')
    memory=ExperienceStore(Path(root),synthetic=synthetic)
    files={p.name:p.read_text() for p in sorted((memory.manager.root/'global').glob('*.md'))}
    snapshot=dict(synthetic=synthetic,files=files)
    goals=[task.goal]+(['verify_grasp'] if task.goal=='placed' else [])
    promoted={item['id'] for goal in goals for item in validated_agent_programs(
        snapshot,task,goal=goal,synthetic=synthetic)}
    selected=[]; seen_sources=set()
    for path in sorted(memory.records.root.glob('*.candidate.json'),
                       key=lambda p:(p.stat().st_mtime_ns,p.name),reverse=True):
        key=path.name.removesuffix('.candidate.json')
        if key in promoted:continue
        try:
            candidate=memory._candidate(key)
            program=candidate.get('agent_program')
            if (not program or candidate['program_goal'] not in goals
                    or candidate['calibration_id']!=task.calibration_id or candidate['shape']!=task.shape
                    or candidate.get('operator_instruction','')!=getattr(task,'operator_instruction','')
                    or not candidate['skills'] or set(candidate['skills'])-set(task.allowed_skills)
                    or program['source_sha256'] in seen_sources):continue
            evidence=run_program_evidence(candidate['discovery_run'])
            identity=evidence['identity']
            if (hashlib.sha256(evidence['owner_bytes']).hexdigest()!=candidate['discovery_source_sha256']
                    or identity.get('synthetic') is not synthetic
                    or identity.get('scenario_id')!=candidate['discovery_scenario']
                    or identity.get('scene_manifest_sha256')!=candidate['discovery_scene']):continue
            if candidate.get('discovery_binding',{}).get('kind')=='owner_measured_segment':
                match=observed_program_discovery_match(candidate,evidence)
                matches=[(None,match)] if match else []
            else:
                matches=[(invocation,agent_program_goal_match(candidate,evidence,invocation))
                         for invocation in bound_program_invocations(program,evidence)]
                matches=[pair for pair in matches if pair[1]]
            if not matches:continue
            first=matches[-1][1]['request_ids'][0]
            intent=next(r['intent'] for r in evidence['owner_rows']
                        if r.get('kind')=='skill_accepted' and r['intent']['request_id']==first)
            if any(intent.get(k)!=v for k,v in candidate['discovery_scope'].items()):continue
            selected.append(dict(id=key,candidate={k:program[k] for k in ('title','source','claim')},
                goal=candidate['program_goal'],source_sha256=program['source_sha256'],
                trial_candidate=trial_candidate(candidate,key)))
            seen_sources.add(program['source_sha256'])
            if len(selected)>=limit:break
        except (HarnessError,ValueError,KeyError,TypeError,OSError,StopIteration):
            continue
    return selected


def register_run_agent_programs(run, root):
    """Archive every proposal; register only its measured invocation-end goals."""
    from .memory import ExperienceStore
    archives=archive_run_programs(run,root)
    registered=[]; unresolved=[]
    try:
        evidence=run_program_evidence(run)
    except (HarnessError,ValueError,KeyError,TypeError,OSError) as exc:
        return dict(archive_ids=archives,candidate_ids=[],unresolved=[str(exc)[:160]],promoted=False)
    mode=evidence['identity'].get('synthetic')
    if type(mode) is not bool:
        return dict(archive_ids=archives,candidate_ids=[],unresolved=['owner_mode_missing'])
    memory=ExperienceStore(Path(root),synthetic=mode)
    trial=evidence['identity'].get('program_trial')
    # Ordinary successful cells are useful without optional model metadata.
    # Preserve generated-program provenance separately; validation still uses
    # the existing exact sandbox-source contract on an independent new run.
    derived=[]
    if not archives and (trial is None or trial['mode']=='discovery'):
        try:
            key=archive_observed_grasp_program(evidence,root)
            if key:archives.append(key);derived.append(key)
        except (HarnessError,ValueError,KeyError,TypeError,OSError) as exc:
            unresolved.append(dict(origin='owner_measured_segment',reason=str(exc)[:160]))
    for archive_id in archives:
        path=Path(root)/'program-candidates'/(archive_id+'.json')
        if trial is not None and trial['mode']=='validation':
            archived=json.loads(path.read_text())
            if archived['candidate']['source_sha256']==trial['candidate']['source_sha256']:
                # This run tests the frozen discovery record; it does not turn
                # the same test into another independent discovery candidate.
                continue
        for goal in (('verify_grasp',) if archive_id in derived else PROGRAM_GOALS):
            try:
                key=memory.propose_agent_program(path,goal=goal,require_discovery_goal=True)
                registered.append(key)
            except (HarnessError,ValueError,KeyError,TypeError,OSError) as exc:
                unresolved.append(dict(archive_id=archive_id,goal=goal,reason=str(exc)[:160]))
    return dict(archive_ids=archives,derived_archive_ids=derived,candidate_ids=sorted(set(registered)),unresolved=unresolved,
                promoted=False)


def review_agent_programs(validation_run, root, scenario_id):
    """Host-only independent verification/promotion; never exposed to the model.

    The owner must already identify an intentionally held-out scene. This
    function cannot label a run held-out or manufacture a scene identity.
    """
    from .memory import ExperienceStore
    evidence=run_program_evidence(validation_run)
    identity=evidence['identity']
    if (identity.get('scenario_id')!=scenario_id or identity.get('heldout') is not True
            or not identity.get('scene_manifest_sha256') or type(identity.get('synthetic')) is not bool):
        return dict(promoted=[],reviews=[],reason='heldout_owner_scene_identity_required')
    memory=ExperienceStore(Path(root),synthetic=identity['synthetic'])
    sources={r.get('source_sha256') for _,_,rows in evidence['hosts'] for r in rows
             if r.get('kind')=='program_execution_finished' and r.get('origin')=='sandbox_host'}
    reviews=[]; promoted=[]; snapshot=None
    for path in sorted(memory.records.root.glob('*.candidate.json')):
        key=path.name.removesuffix('.candidate.json')
        try:
            candidate=memory._candidate(key)
            trial=identity.get('program_trial')
            if trial is not None and (trial['mode']!='validation'
                    or trial['candidate']['candidate_id']!=key):continue
            if candidate.get('agent_program',{}).get('source_sha256') not in sources:continue
            receipt=memory.verify(key,validation_run=Path(validation_run),scenario_id=scenario_id)
            reviews.append(receipt)
            if receipt['accepted']:
                snapshot=memory.promote(key)
                promoted.append(key)
        except (HarnessError,ValueError,KeyError,TypeError,OSError) as exc:
            reviews.append(dict(candidate_id=key,accepted=False,reason=str(exc)[:160]))
    result=dict(promoted=promoted,reviews=reviews,snapshot=snapshot)
    memory.records.event('agent_program_validation_review',validation_run=str(Path(validation_run).resolve()),
                         scenario_id=scenario_id,result=result)
    return result


PROGRAM_INSTRUCTIONS = '''
You may define new task-relative Python functions and bounded conditional
programs using the public robot API. Installed helpers are conveniences, not the
complete strategy space. Never redefine an installed helper or replay unknown
motion. Do not assume any prewritten strategy exists for the operator's task.
To retain a strategy you actually developed, attach optional skill_candidate to
a python call: {title, claim, source}. Source contains only function definitions,
including applicable(scene) and run_skill(robo), each with one argument.
applicable returns one pure boolean expression over numeric scene data (dict.get
is supported); run_skill queries current measurements and composes public robot
operations. Use object-relative relationships, not recorded world waypoints.
State exactly what the program claims, including partial skills and known gaps.
The host archives this as an UNVERIFIED candidate, not a successful skill. This
metadata does not execute the source. Explicitly execute intended actions through
the normal python code argument and inspect their receipts. Never invent motion,
contact capabilities or successful outcomes to make a candidate appear complete.
To execute exactly a candidate's source with a host-bound receipt, send the same
skill_candidate with execute_candidate=true and code="". The host loads it in a
fresh namespace, obtains current numeric geometry, checks applicable(scene), and
invokes run_skill(robo) only when applicable returns True. Only the public robot
argument, ordinary Python builtins, and functions defined in source are available;
earlier cell variables/helpers are not inherited. Execution binding is not
independent validation or promotion. Default execute_candidate=false preserves
the metadata-only proposal behavior.
'''
