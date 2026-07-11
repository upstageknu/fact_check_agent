"""CLI 진입점. report_id로 워크플로우를 조회해 결정론적 사실 판단을 실행하고 결과를 출력한다.

실행:  python main.py <report_id>
"""

import json
import sys

from orchestrator import fetch_workflow
from runner import run_fact_check


def main(report_id=None):
    if report_id is None:
        if len(sys.argv) > 1:
            report_id = sys.argv[1]
        else:
            raise SystemExit("사용법: python main.py <report_id>")

    report = fetch_workflow(report_id)
    parser_result = (report.get("agent_results") or {}).get("parser") or {}
    raw_report_txt = (report.get("input") or {}).get("raw_report_txt", "")

    result = run_fact_check(parser_result, raw_report_txt=raw_report_txt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
