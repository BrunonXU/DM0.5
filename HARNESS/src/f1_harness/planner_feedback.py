"""Remove only exact printed copies of structured tool receipts from model input.

The raw worker result stays in trace/capture. New calculations, geometry,
exceptions and incomplete/truncated output remain untouched.
"""
import ast


def compact_feedback(result):
    feedback=dict(result)
    stdout=result.get('stdout')
    calls=result.get('primitive_calls', [])
    if not isinstance(stdout,str) or len(stdout)>65536 or not calls:
        return feedback

    def echoed(value):
        if not isinstance(value,dict) or not value:
            return False
        # A coincidentally equal arbitrary calculation is not a tool receipt.
        if not ({'planned','current_scope'} <= value.keys() or
                {'request_id','outcome'} <= value.keys()):
            return False
        return any(all(key in call and call[key]==item for key,item in value.items()) for call in calls)

    def reference_diagnostics(value,depth=0):
        """Keep new printed fields while referencing exact large ledger values."""
        if depth>8:return value,0
        if isinstance(value,list):
            items=[reference_diagnostics(item,depth+1) for item in value]
            return [item for item,_ in items],sum(count for _,count in items)
        if not isinstance(value,dict):return value,0
        output={};count=0
        for key,item in value.items():
            match=None
            # Generated programs often rename these fields when printing.
            # Alias alone is insufficient: require exact full-value equality
            # with the canonical receipt, retaining the caller's printed key.
            canonical={'diagnostics':'planning_diagnostic','placement':'placement_advisory'}.get(key,key)
            if canonical in ('planning_diagnostic','placement_advisory','recovery_context') and isinstance(item,dict) and len(repr(item))>160:
                match=next((i for i,call in enumerate(calls) if call.get(canonical)==item),None)
            if match is not None:
                output[key]={'see_primitive_call_index':match,'field':canonical};count+=1
            else:
                output[key],n=reference_diagnostics(item,depth+1);count+=n
        return output,count

    lines=[];removed=0
    for line in stdout.splitlines(keepends=True):
        try:value=ast.literal_eval(line.strip())
        except (ValueError,SyntaxError,TypeError,RecursionError,MemoryError):
            lines.append(line);continue
        if echoed(value):
            removed+=1;continue
        if (isinstance(value,dict) and set(value)=={'stage','diagnostics'}
                and isinstance(value['stage'],str) and isinstance(value['diagnostics'],list)
                and value['diagnostics'] and all(echoed(v) for v in value['diagnostics'])):
            lines.append(repr(dict(stage=value['stage'],diagnostics='see primitive_calls'))+'\n')
            removed+=1;continue
        compact,count=reference_diagnostics(value)
        if count:
            lines.append(repr(compact)+'\n');removed+=count
        else:lines.append(line)
    if removed:
        feedback['stdout']=''.join(lines)
        feedback['stdout_receipts_deduplicated']=removed
    return feedback
