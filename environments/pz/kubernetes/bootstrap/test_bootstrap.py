"""Offline orchestration checks: never opens SSH or starts systemd/k3s."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).with_name("bootstrap.py"))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
PAYLOAD = {"spec": {"version": "v1.36.4+k3s1", "config": "eA==", "installer_sha256": "a" * 64}, "script": "test worker"}
RUNNING = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success", "ExecMainCode": "0", "ExecMainStatus": "0", "MainPID": "42"}
SUCCESS = dict(RUNNING, SubState="exited", ExecMainCode="1", MainPID="0")
MISSING = {"LoadState": "not-found"}


def remote_readiness_function():
    # Execute only the worker's readiness function, never its installation code.
    function = next(node for node in ast.parse(bootstrap.REMOTE).body if isinstance(node, ast.FunctionDef) and node.name == "wait_node_ready")
    namespace = {"subprocess": subprocess, "json": json, "time": time}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<remote-readiness>", "exec"), namespace)
    return namespace["wait_node_ready"]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.node_ready_impl = bootstrap.node_ready
        self.temp = tempfile.TemporaryDirectory()
        self.job = Path(self.temp.name) / "pz-bootstrap" / "job"
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(patch.object(bootstrap, "JOB", self.job))
        self.patches.enter_context(patch.object(bootstrap.os, "geteuid", return_value=0))
        self.patches.enter_context(patch.object(bootstrap.subprocess, "check_output", return_value=bootstrap.EXPECTED_HOST + "\n"))
        self.props = self.patches.enter_context(patch.object(bootstrap, "properties", return_value=RUNNING))
        self.run = self.patches.enter_context(patch.object(bootstrap.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")))
        self.ready = self.patches.enter_context(patch.object(bootstrap, "node_ready", return_value=True))
        self.old_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_umask)
        self.patches.close()
        self.temp.cleanup()

    def initialize(self):
        self.props.side_effect = [MISSING, RUNNING]
        result = bootstrap.manage("launch", PAYLOAD)
        self.props.side_effect = None
        self.props.return_value = RUNNING
        return result

    def test_launch_once_then_reconnect_private_immutable_files(self):
        self.assertEqual(self.initialize()["state"], "running")
        self.assertEqual(bootstrap.manage("status", PAYLOAD)["state"], "running")
        self.assertEqual(bootstrap.manage("launch", PAYLOAD)["state"], "running")
        self.assertEqual(self.run.call_count, 1)
        command = self.run.call_args.args[0]
        self.assertIn("--remain-after-exit", command)
        self.assertIn("--property=Restart=no", command)
        for path in self.job.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.job.stat().st_mode & 0o777, 0o700)

    def test_marker_and_missing_unit_never_relaunch_after_reboot(self):
        self.initialize()
        self.props.return_value = MISSING
        for mode in ("status", "launch"):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                bootstrap.manage(mode, PAYLOAD)
        self.assertEqual(self.run.call_count, 1)

    def test_ambiguous_systemd_launch_is_never_repeated(self):
        self.props.return_value = MISSING
        self.run.side_effect = subprocess.TimeoutExpired("systemd-run", 15)
        with self.assertRaises(subprocess.TimeoutExpired):
            bootstrap.manage("launch", PAYLOAD)
        self.assertTrue((self.job / "launch-attempt").is_file())
        with self.assertRaisesRegex(RuntimeError, "already attempted"):
            bootstrap.manage("launch", PAYLOAD)
        self.assertEqual(self.run.call_count, 1)

    def test_launch_failure_is_never_repeated(self):
        self.props.return_value = MISSING
        self.run.return_value = subprocess.CompletedProcess([], 1, b"", b"private error")
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            bootstrap.manage("launch", PAYLOAD)
        with self.assertRaisesRegex(RuntimeError, "already attempted"):
            bootstrap.manage("launch", PAYLOAD)
        self.assertEqual(self.run.call_count, 1)

    def test_changed_spec_or_script_refused(self):
        self.initialize()
        for payload in (dict(PAYLOAD, script="changed"), dict(PAYLOAD, spec=dict(PAYLOAD["spec"], version="changed"))):
            with self.assertRaises(RuntimeError):
                bootstrap.manage("launch", payload)
        self.assertEqual(self.run.call_count, 1)

    def test_unmanaged_unit_refused(self):
        with self.assertRaisesRegex(RuntimeError, "Unmanaged"):
            bootstrap.manage("launch", PAYLOAD)
        self.run.assert_not_called()

    def test_failed_job_never_restarts(self):
        self.initialize()
        self.props.return_value = dict(RUNNING, ActiveState="failed", Result="exit-code", ExecMainCode="1", ExecMainStatus="1", MainPID="0")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            bootstrap.manage("launch", PAYLOAD)
        self.assertEqual(self.run.call_count, 1)

    def test_success_requires_normal_exit_and_current_node_ready(self):
        current = bootstrap.identity(**PAYLOAD)
        self.assertEqual(bootstrap.classify(SUCCESS, current)["state"], "succeeded")
        self.ready.return_value = False
        self.assertEqual(bootstrap.classify(SUCCESS, current)["state"], "waiting-for-node-ready")
        for changed in ({"ExecMainCode": "2"}, {"ExecMainStatus": "1"}, {"MainPID": "42"}, {"Result": "signal"}):
            with self.assertRaises(RuntimeError):
                bootstrap.classify(dict(SUCCESS, **changed), current)

    def test_local_lost_launch_response_retries_status_only(self):
        replies = [bootstrap.TransportError("lost"), bootstrap.TransportError("lost again"), {"state": "running"}, {"state": "succeeded", "node_ready": True}]
        with patch.object(bootstrap, "request", side_effect=replies) as request, patch.object(bootstrap.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            bootstrap.wait_job(**PAYLOAD)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["launch", "status", "status", "status"])

    def test_local_timeout_does_not_stop_or_restart_remote_job(self):
        with patch.object(bootstrap, "request", side_effect=bootstrap.TransportError("lost")) as request, patch.object(bootstrap.time, "monotonic", side_effect=[0, 1201]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "not stopped"):
                bootstrap.wait_job(**PAYLOAD)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["launch"])

    def test_node_readiness_reads_named_node_and_never_returns_credentials(self):
        response = {"metadata": {"name": bootstrap.EXPECTED_HOST}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        self.assertTrue(self.node_ready_impl())
        response["metadata"]["name"] = "unexpected"
        self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        self.assertFalse(self.node_ready_impl())
        self.assertIn("--request-timeout=10s", self.run.call_args.args[0])

    def test_worker_waits_for_registration_then_readiness(self):
        waiting = {"metadata": {"name": bootstrap.EXPECTED_HOST}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}}
        ready = {"metadata": {"name": bootstrap.EXPECTED_HOST}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "NotFound: private API startup diagnostics"),
            subprocess.CompletedProcess([], 0, json.dumps(waiting), ""),
            subprocess.CompletedProcess([], 0, json.dumps(ready), ""),
        ]
        output = io.StringIO()
        with patch.object(time, "monotonic", return_value=0), patch.object(time, "sleep") as sleep, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            remote_readiness_function()(Path("/private/operator.yaml"), bootstrap.EXPECTED_HOST)
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(all(call.kwargs["capture_output"] and call.kwargs["timeout"] <= 15 for call in self.run.call_args_list))

    def test_worker_readiness_deadline_is_bounded(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "NotFound: private data")
        with patch.object(time, "monotonic", side_effect=[0, 179, 180, 180]), patch.object(time, "sleep"):
            with self.assertRaisesRegex(SystemExit, "within 180 seconds"):
                remote_readiness_function()(Path("/private/operator.yaml"), bootstrap.EXPECTED_HOST)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 1)

    def test_worker_tolerates_transient_transport_timeout(self):
        ready = {"metadata": {"name": bootstrap.EXPECTED_HOST}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.run.side_effect = [subprocess.TimeoutExpired("kubectl", 15), subprocess.CompletedProcess([], 0, json.dumps(ready), "")]
        with patch.object(time, "monotonic", return_value=0), patch.object(time, "sleep"):
            remote_readiness_function()(Path("/private/operator.yaml"), bootstrap.EXPECTED_HOST)
        self.assertEqual(self.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
