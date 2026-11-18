"""Recognition of a constant align proposal, never an arrival certificate."""
import numpy as np


def constant_alignment(intent,plan,held16):
    if intent is None or intent.skill!='align' or plan.trajectory is None:
        return False
    try:
        anchor=np.asarray(plan.trajectory['anchor'],float)
        targets=np.asarray(plan.targets,float)
        held=np.asarray(held16,float)
        return bool(anchor.shape==(16,) and held.shape==(16,)
            and targets.ndim==2 and targets.shape[1]==16 and len(targets)>0
            and np.isfinite(targets).all() and np.isfinite(anchor).all()
            and np.array_equal(held,anchor) and np.all(targets==anchor))
    except (KeyError,ValueError,TypeError):
        return False


def check_alignment(verifier,intent,plan,observation,full,measured):
    """Optional optimization failures fall back to the normal motion contract."""
    try:
        result=verifier(intent,plan,observation,len(plan.targets)-1,full,measured)
        return result if isinstance(result,dict) else dict(accepted=False,reason='invalid_alignment_proof')
    except Exception as exc:
        return dict(accepted=False,reason=type(exc).__name__)
