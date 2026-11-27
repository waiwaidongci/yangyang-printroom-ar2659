from fastapi import HTTPException
from fastapi.responses import Response

from src.constants.workflow import Decision
from src.services.reviews import review_info
from src.services.runs import export_table, get_run, run_info
from src.utils.csv_files import write_rows


def report_detail(session, run_id, actor):
    run, review = get_run(session, run_id, actor)
    return {**run_info(run, review), 'result': run.result, 'error': run.error,
            'review': review_info(review) if review else None,
            'handover_ready': bool(review and review.decision == Decision.APPROVED)}


def report_csv(session, run_id, key, actor):
    run, review = get_run(session, run_id, actor)
    # 交班清单属于正式交付物：全部判定完成且复核通过后才能导出，未通过一律拦截。
    if review is None or review.decision != Decision.APPROVED:
        raise HTTPException(409, '运行须经复核负责人批准后才能导出交班清单')
    table = export_table(session, run_id, key, actor)
    return Response(write_rows(table['columns'], table['rows']), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="run-{run_id}-{table["key"]}.csv"'})
