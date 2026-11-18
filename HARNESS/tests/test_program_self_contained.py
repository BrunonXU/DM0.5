"""Frozen programs cannot depend on files, reflection or earlier cell variables."""
import hashlib
import json

import pytest
import yaml

from f1_harness.contracts import HarnessError, Task, digest
from f1_harness.memory import ExperienceStore
from f1_harness.program_experience import (
    AGENT_PROGRAM_VERIFIER, audit_self_contained_program, invocation_code,
    validate_candidate, validated_agent_programs,
)


def candidate(body):
    return dict(title='Dependency test',claim='No physical evidence',source=
        'def applicable(scene):\n    return True\n\ndef run_skill(robo):\n'+
        '\n'.join('    '+line for line in body.splitlines())+'\n')


UNBOUND = [
    'return open("previous-route.json").read()',
    'import task_recipe\nreturn task_recipe.run(robo)',
    'from .task_recipe import run\nreturn run(robo)',
    'import os\nreturn os.getcwd()',
    'import pathlib\nreturn pathlib.Path("prior.json").read_text()',
    'import sys\nreturn sys.modules',
    'import builtins\nreturn builtins.open("route")',
    'import io\nreturn io.open("route")',
    'return old_route',
    'return math.sqrt(4)',  # Preset cell globals are not an explicit import.
    'return np.array([1,2,3])',
    'return eval("previous_route")',
    'return exec("pass")',
    'return compile("pass", "route", "exec")',
    'f = globals\nreturn f()',
    'return locals()',
    'return vars(robo)',
    'return getattr(robo,"backend")',
    'return setattr(robo,"backend",None)',
    'return __import__("math")',
    'return robo.__class__',
    'return "{0.__class__}".format(robo)',
    'g=(x for x in [])\nreturn g.gi_frame.f_builtins["open"]("route")',
    'import math\nreturn math.__dict__',
    'from math import __loader__\nreturn __loader__',
    'from math import *\nreturn sqrt(4)',
    'import math\nmath.sqrt=lambda x: 99\nreturn math.sqrt(4)',
    'global old_route\nold_route=[1,2,3]\nreturn old_route',
]


@pytest.mark.parametrize('body',UNBOUND)
def test_registration_and_frozen_lookup_both_reject_unbound_dependencies(tmp_path,body):
    proposed=candidate(body)
    # Proposals can still be archived during exploration. The stricter check
    # controls promotion/automatic reuse, not the model's ordinary Python tool.
    program=validate_candidate(proposed)
    with pytest.raises((HarnessError,SyntaxError)):
        audit_self_contained_program(program)
    archive=dict(schema='f1-agent-program-candidate-v1',origin='astra',candidate=program)
    path=tmp_path/(digest(archive)+'.json');path.write_text(json.dumps(archive))
    with pytest.raises((HarnessError,SyntaxError)):
        ExperienceStore(tmp_path/'memory',synthetic=True).propose_agent_program(path,goal='verify_grasp')
    meta=dict(id='previous-note',confidence='verified',verifier=AGENT_PROGRAM_VERIFIER,
        review_sha256='prior-review',program_goal='placed',calibration_id='cal',shape='square',
        skills=['grasp'],agent_program=program,
        invocation_code_sha256=hashlib.sha256(invocation_code(program).encode()).hexdigest())
    snapshot=dict(synthetic=True,files={'previous.md':'---\n'+yaml.safe_dump(meta)+'---\n'})
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    assert not validated_agent_programs(snapshot,task,synthetic=True)


@pytest.mark.parametrize('body',[
    'import math\npoints=[math.sqrt(x) for x in [1,4,9]]\nreturn sum(points)',
    'from math import sqrt as root\nreturn root(4)',
    'def distance(x):\n    return abs(x)\nreturn distance(-2)',
    'state=robo.geometry()\nreturn state.get("available") is True',
])
def test_explicit_pure_computation_and_lexical_helpers_are_self_contained(body):
    program=validate_candidate(candidate(body))
    assert audit_self_contained_program(program)==program['source_sha256']
