"""User-selected demo IK: 5mm / 0.05deg, native predicted H20 at 30Hz."""
import argparse
from f1_inference.ik_ood_adapter import ood_solver, decode_response as decode_ood
from f1_inference.native_adapter import create_app

RECIPE = 'f1_ikfk_demo_pos5mm_rot005deg_v1'
OPTIONS = {'position_tolerance_mm': 5.0, 'rotation_tolerance_deg': 0.05}
UPSTREAMS = {'5k': 7892, '20k': 7894, '25k': 7896, '70k': 7901}
STEPS = {'5k': 5000, '20k': 20000, '25k': 25000, '70k': 70000}


def demo_solver():
    return ood_solver(options=OPTIONS)


def decode_response(response, solver):
    return decode_ood(response, solver, recipe=RECIPE, options=OPTIONS)


def demo_app():
    return create_app(demo_solver(), response_decoder=decode_response,
                      upstreams=UPSTREAMS, checkpoint_steps=STEPS, health_fields={
                          'execution_mode': 'sync_raw_h20_v1', 'execute_steps': 20,
                          'ik_recipe_version': RECIPE, 'ik_options': OPTIONS,
                          'adapter': 'demo_5mm_0.05deg; original_model_output_and_joint_limits',
                      })


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=7900)
    args = parser.parse_args()
    demo_app().run(host='127.0.0.1', port=args.port, threaded=True, use_reloader=False)
