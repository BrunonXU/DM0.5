"""Internal FAKE worker: shared numerical models for grasp and transfer drafts."""
from replay_planning_process import PlanningProcess,serve
from prepared_transfer import PreparedNextSkillService,METHODS


class NextSkillProcess(PlanningProcess):
    WORKER_NAME='replay_transfer_prefetch.py'
    METHODS=METHODS


if __name__=='__main__':
    import sys
    from warm_grasp_process import ERROR_CODES
    if len(sys.argv)!=3 or sys.argv[1]!='--worker':raise SystemExit('internal offline next-skill worker only')
    serve(int(sys.argv[2]),service_wrapper=PreparedNextSkillService,methods=METHODS,
          error_codes=ERROR_CODES|{'prepared_grasp_closure_missing','prepared_parent_already_holding'})
