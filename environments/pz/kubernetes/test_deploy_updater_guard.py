"""Offline tests for the Terraform/updater exclusion gate; never access a VM."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("deploy_guard", Path(__file__).with_name("deploy.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def idle(**changes):
    return {"status": "ok", "suspended": True, "active_jobs": 0,
            "active_pods": 0, "journal": "terminal", **changes}


class RemoteGuard(unittest.TestCase):
    def setUp(self):
        self.namespace = {}
        exec(deploy.UPDATER_GUARD, self.namespace)

    def snapshot(self, cron=None, jobs=None, pods=None):
        values = [cron, {"items": jobs or []}, {"items": pods or []}]
        with mock.patch.dict(self.namespace, updater_kubectl=mock.Mock(side_effect=values),
                             updater_journal_state=lambda: "absent"):
            return self.namespace["updater_snapshot"]()

    def test_initial_absent_cron_and_completed_history_are_idle(self):
        result = self.snapshot(jobs=[{"status": {"conditions": [{"type": "Complete", "status": "True"}]}}],
                               pods=[{"status": {"phase": "Succeeded"}}])
        self.assertEqual(result, idle(journal="absent"))

    def test_pending_job_and_terminating_completed_pod_are_active(self):
        result = self.snapshot(cron={"spec": {"suspend": True}}, jobs=[{}],
                               pods=[{"metadata": {"deletionTimestamp": "fixture"}, "status": {"phase": "Succeeded"}}])
        self.assertEqual((result["active_jobs"], result["active_pods"]), (1, 1))

    def test_apply_gate_rejects_unpaused_active_or_incomplete_state(self):
        for state in (idle(suspended=False), idle(active_jobs=1), idle(active_pods=1),
                      idle(journal="incomplete"), idle(journal="invalid")):
            with self.subTest(state=state), mock.patch.dict(self.namespace, updater_snapshot=lambda: state):
                with self.assertRaises(RuntimeError):
                    self.namespace["require_updater_idle"]()
        for phase in ("absent", "terminal"):
            with mock.patch.dict(self.namespace, updater_snapshot=lambda: idle(journal=phase)):
                self.namespace["require_updater_idle"]()

    def test_journal_accepts_only_absent_or_known_terminal_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            with mock.patch.dict(self.namespace, Path=lambda _: path):
                self.assertEqual(self.namespace["updater_journal_state"](), "absent")
                for phase in ("committed", "rolled_back", "starting_candidate", "restoring_data", "unexpected"):
                    path.write_text(json.dumps({"phase": phase}))
                    expected = "terminal" if phase in {"committed", "rolled_back"} else "incomplete"
                    self.assertEqual(self.namespace["updater_journal_state"](), expected)
                path.write_text("incomplete-json")
                self.assertEqual(self.namespace["updater_journal_state"](), "invalid")
                path.unlink()
                path.symlink_to(Path(directory) / "missing")
                self.assertEqual(self.namespace["updater_journal_state"](), "invalid")


class LocalObserver(unittest.TestCase):
    def run_pause(self, replies, timeout=10):
        now = [0]
        def advance(seconds):
            now[0] += seconds
        with mock.patch.object(deploy, "UPDATER_PAUSE_TIMEOUT", timeout), \
             mock.patch.object(deploy, "updater_request", side_effect=replies) as request, \
             mock.patch.object(deploy.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(deploy.time, "sleep", side_effect=advance), redirect_stdout(io.StringIO()):
            deploy.pause_updater_for_plan()
            return [call.args[0] for call in request.call_args_list]

    def test_lost_pause_ack_reconnects_without_repeating_mutation(self):
        self.assertEqual(self.run_pause([subprocess.CalledProcessError(255, "ssh"), idle()]), ["pause", "status"])

    def test_waits_for_active_job_and_pod_before_reading_terminal_journal(self):
        self.assertEqual(self.run_pause([idle(), idle(active_jobs=1, active_pods=1, journal="incomplete"), idle()]),
                         ["pause", "status", "status"])

    def test_incomplete_idle_journal_and_failed_pause_do_not_plan(self):
        for state in (idle(journal="incomplete"), idle(suspended=False)):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                self.run_pause([idle(), state])

    def test_unavailable_or_active_updater_has_bounded_deadline(self):
        for state in ({"status": "unavailable"}, idle(active_jobs=1)):
            with self.subTest(state=state), self.assertRaises(TimeoutError):
                self.run_pause([idle(), state, state])

    def test_workloads_plan_runs_guard_before_any_terraform_action(self):
        events = []
        with mock.patch.object(deploy, "pause_updater_for_plan", side_effect=lambda: events.append("guard")), \
             mock.patch.object(deploy, "ssh", side_effect=lambda command, **_: events.append(command)), \
             mock.patch("sys.argv", ["deploy.py", "plan", "workloads"]):
            deploy.main()
        self.assertEqual(events[0], "guard")
        self.assertIn(" init ", events[1])
        self.assertIn(" plan ", events[2])


class SavedApplyGate(unittest.TestCase):
    def test_new_launch_is_refused_before_marker_if_updater_is_not_paused(self):
        commands = []
        def run(command, **_):
            commands.append(command)
            if command[0] == "systemctl":
                output = "LoadState=not-found\n"
            elif command[0] == "k3s":
                output = json.dumps({"spec": {"suspend": False}} if "cronjob" in command else {"items": []})
            else:
                self.fail("An apply process unexpectedly launched")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        with tempfile.TemporaryDirectory() as directory:
            remote = deploy.APPLY_REMOTE.replace('Path("/opt/pz-infrastructure")', "Path(" + repr(directory) + ")")
            with mock.patch.object(subprocess, "run", side_effect=run), \
                 mock.patch("sys.argv", ["-", "launch", "workloads", "a" * 64]), \
                 redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
                exec(remote, {})
            self.assertIn("not suspended", json.loads(output.getvalue())["error"])
            self.assertFalse(list(Path(directory).rglob("launch-attempt")))
            self.assertFalse(any(command[0] == "systemd-run" for command in commands))

    def test_existing_apply_unit_status_is_not_blocked_or_relaunched(self):
        def run(command, **_):
            self.assertEqual(command[0], "systemctl")
            return subprocess.CompletedProcess(command, 0, stdout="LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainCode=1\nExecMainStatus=0\n", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            remote = deploy.APPLY_REMOTE.replace('Path("/opt/pz-infrastructure")', "Path(" + repr(directory) + ")")
            with mock.patch.object(subprocess, "run", side_effect=run), \
                 mock.patch("sys.argv", ["-", "launch", "workloads", "a" * 64]), redirect_stdout(io.StringIO()) as output:
                exec(remote, {})
            self.assertEqual(json.loads(output.getvalue())["Result"], "success")


if __name__ == "__main__":
    unittest.main()
