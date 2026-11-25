"""Numeric-first model context; raw evidence stays in the local capture store."""
from copy import deepcopy
import json

POLICY = '''Default to numeric observations and execute decidable steps locally.
No initial or failure images are automatically supplied. Only when numeric evidence
cannot resolve a specific question, set image_reason on the python tool call,
explaining the missing evidence, and explicitly use robo.show for the relevant view.
The left wrist is an additional camera at its operator-set pose. The current motion tools
control the right arm. For a hidden aperture, use robo.aperture_observation() for candidates in
raw image pixels; a 2D shape alone is not a world-frame pose. If ambiguity persists
request robo.show("left") with image_reason. For localization, read search_entities
in robo.geometry() or failed inspect_plan geometry. Match the ID to its listed camera
before requesting pixels; boxes belong only to the displayed frame. A partial scene
can still support specific skills. Read available_skills and missing_entities;
inspect_plan checks the requested skill against the current scene and owner.
Aim for zero image requests. Respect the task's visual consultation budget and
resolve subsequent visual questions with fresh local numeric evidence when it is spent.
Images are delivered once. Their source frame references remain available locally;
never invent boxes on an unseen image. Missing geometry is unknown, not a detection.
Old tool outputs are summarized; persistent Python variables and original receipts
remain available. Inspect needed details explicitly instead of printing whole ledgers.
'''


def retire_images(inputs):
    """Replace only consumed pixels; keep source metadata and call/output pairing."""
    result=deepcopy(inputs)
    for message in result:
        if isinstance(message.get('content'),list):
            message['content']=[dict(type='input_text',text='Previously supplied image omitted; use its recorded source frame, or explicitly request a new view if needed.')
                if part.get('type')=='input_image' else part for part in message['content']]
    return result


def compact_history(inputs):
    """Summarize past receipts, preserving every call ID and latest full feedback.

    Decision context in the latest feedback retains unresolved blockers. Never
    derive success from a model assertion or erase unknown execution/checkpoints.
    """
    result=deepcopy(inputs)
    outputs=[m for m in result if m.get('type')=='function_call_output']
    for message in outputs[:-1]:
        try: value=json.loads(message['output'])
        except (TypeError,ValueError): continue
        if not isinstance(value,dict) or value.get('historical_summary'):continue
        value = expand_references(value)
        keys=('tool','kwargs','request_id','outcome','reason','no_motion','cancel_stage',
              'resume_request_id','verification_request_id','planned','current_scope','accepted')
        summary=dict(historical_summary=True,original_call_id=message['call_id'],
            primitive_calls=[{k:c[k] for k in keys if k in c} for c in value.get('primitive_calls',[])],
            details='Original complete result is in local trace and persistent Python state; query needed details.',
            exception=value.get('exception'),decision_context=value.get('decision_context'))
        # Old printed geometry/calculations are historical, not a current scene.
        # Keep their content identity in the model projection; the complete
        # original remains in the host trace and variables remain in Python.
        import hashlib
        summary['archived_output_fields']={}
        for key in ('stdout','printed_result','value'):
            if key not in value:continue
            raw=json.dumps(value[key],ensure_ascii=False,sort_keys=True).encode()
            summary['archived_output_fields'][key]=dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        message['output']=json.dumps(summary)
    return result


def deduplicate_feedback(value):
    """Reference exact repeated JSON structures; never trim novel numeric facts."""
    import ast
    value=deepcopy(value)
    stdout=value.get('stdout')
    if isinstance(stdout,str):
        try:
            parsed=ast.literal_eval(stdout.strip())
            if isinstance(parsed,(dict,list)):
                # Round-trip ensures no Python-only values are silently changed.
                if json.loads(json.dumps(parsed))==parsed:
                    value.pop('stdout');value['printed_result']=parsed
        except (ValueError,SyntaxError,TypeError,RecursionError):pass
    seen={}
    def visit(item,path):
        if not isinstance(item,(dict,list)):return item
        key=json.dumps(item,sort_keys=True,ensure_ascii=False)
        if len(key)>512:
            if key in seen:return {'same_as_json_pointer':seen[key]}
            seen[key]=path
        if isinstance(item,list):return [visit(v,path+'/'+str(i)) for i,v in enumerate(item)]
        return {k:visit(v,path+'/'+str(k).replace('~','~0').replace('/','~1')) for k,v in item.items()}
    return visit(value,'')


def expand_references(value):
    """Resolve our within-result references before projecting a historical receipt."""
    def visit(item, depth=0):
        if depth > 64: raise ValueError('feedback_reference_cycle')
        if isinstance(item, dict):
            if set(item)=={'same_as_json_pointer'}:
                target=value
                for part in item['same_as_json_pointer'].split('/')[1:]:
                    part=part.replace('~1','/').replace('~0','~')
                    target=target[int(part)] if isinstance(target,list) else target[part]
                return visit(target,depth+1)
            return {k:visit(v,depth+1) for k,v in item.items()}
        if isinstance(item,list):return [visit(v,depth+1) for v in item]
        return item
    return visit(value)
