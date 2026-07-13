import unittest
from unittest.mock import patch

from event_logger import configure_events, timed_stage
from poc_repro.pipeline import pipeline_stage


class EventLoggingTest(unittest.TestCase):
    @patch("event_logger.addLog")
    def test_timed_stage_emits_started_and_completed(self, add_log):
        add_log.return_value = True
        configure_events(
            report_id="RPT-TEST",
            agent_job_id=7,
            trace_id="trace-1",
            request_id="request-1",
        )

        with timed_stage("repository_index", payload={"item_count": 2}):
            pass

        self.assertEqual(add_log.call_count, 2)
        started = add_log.call_args_list[0]
        completed = add_log.call_args_list[1]
        self.assertEqual(started.kwargs["event_type"], "fact_check.stage.started")
        self.assertEqual(completed.kwargs["event_type"], "fact_check.stage.completed")
        self.assertEqual(completed.kwargs["payload"]["stage"], "repository_index")
        self.assertGreaterEqual(completed.kwargs["payload"]["duration_ms"], 0)
        self.assertEqual(completed.kwargs["agent_job_id"], 7)

    @patch("event_logger.addLog")
    def test_timed_stage_failure_is_logged_without_swallowing_error(self, add_log):
        add_log.return_value = True
        configure_events(report_id="RPT-TEST")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with timed_stage("docker_run"):
                raise RuntimeError("boom")

        failed = add_log.call_args_list[-1]
        self.assertEqual(failed.kwargs["event_type"], "fact_check.stage.failed")
        self.assertEqual(failed.kwargs["level"], "ERROR")
        self.assertEqual(failed.kwargs["payload"]["error_type"], "RuntimeError")

    def test_poc_pipeline_stage_callback_receives_duration(self):
        events = []

        with pipeline_stage("harness_generation", lambda status, stage, payload: events.append((status, stage, payload))):
            pass

        self.assertEqual(events[0][0:2], ("started", "harness_generation"))
        self.assertEqual(events[1][0:2], ("completed", "harness_generation"))
        self.assertGreaterEqual(events[1][2]["duration_ms"], 0)


if __name__ == "__main__":
    unittest.main()
