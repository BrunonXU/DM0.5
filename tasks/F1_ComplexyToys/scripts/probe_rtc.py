"""Real 5K model probe using previously captured rollout inputs; zero robot IO."""
import json
from pathlib import Path
import time
import numpy as np
from PIL import Image
import torch
from f1_toys.inference import Policy, batched_observation
from f1_toys.rtc import sample_actions

root = Path('/workspace/tasks/F1_ComplexyToys')
inputs = root/'artifacts/20261019-prerejection-ab'
row = json.loads((inputs/'inputs.jsonl').read_text().splitlines()[0])
policy = Policy(root/'artifacts/20261016-v1-formal-training/bundle-5000',
    root/'artifacts/h20-staging/f1-toys-20261016-v1-formal-r3/RLinf')
state = row['request']['prediction_replay']['state16']
held = row['request']['prediction_replay']['last_submitted16']
images = {key:Image.open(inputs/name).convert('RGB') for key,name in row['images'].items()}
data = policy.processor.observation(images, np.asarray(state))
from f1_toys.processing import process_image
prepared={key:Image.fromarray(process_image(image,key,policy.contract)) for key,image in images.items()}
prepared_data=policy.observation_from_pixels(prepared,state)
for key,value in data.items():
    if isinstance(value,dict):
        for name,array in value.items():np.testing.assert_array_equal(array,prepared_data[key][name])
    else:np.testing.assert_array_equal(value,prepared_data[key])
print(json.dumps(dict(prepared_model_input_bitwise_match=True)),flush=True)
obs = batched_observation(data, policy.observation_type, policy.device)
noise = torch.randn((1,50,32), device='cuda', generator=torch.Generator(device='cuda').manual_seed(42))
with torch.no_grad(), torch.autocast('cuda', dtype=torch.bfloat16):
    old = policy.model.sample_actions(obs, num_steps=5, noise=noise.clone())
    new = sample_actions(policy.model, obs, policy.preprocess_observation, noise=noise.clone())
torch.testing.assert_close(old, new, rtol=0, atol=0)
context = dict(bootstrap=True, delay_steps=15, shift_steps=20, coverage_steps=37,
    plan_origin_ns=0, base_plan_id=0, committed_actions16=[], previous_absolute_eef10=[])
result, _, _ = policy.predict_rtc(images, state, held, context, seed=42)
print(json.dumps(dict(bootstrap=result['success'], timing=result['timing_ms'], sync_bitwise_match=True)), flush=True)
if result['success']:
    context.update(bootstrap=False, base_plan_id=1, previous_absolute_eef10=result['absolute_eef10'][20:],
                   committed_actions16=result['actions'][20:35])
else:
    context.update(bootstrap=False, base_plan_id=1, previous_absolute_eef10=result['absolute_eef10'][20:],
                   committed_actions16=[held]*15)
for index in range(4):
    result, _, _ = policy.predict_rtc(images, state, held, context, seed=42)
    print(json.dumps(dict(index=index, guided=result['success'], timing=result['timing_ms'])), flush=True)
