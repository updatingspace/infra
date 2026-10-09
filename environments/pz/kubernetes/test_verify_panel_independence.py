"""Offline tests; Kubernetes and RCON are fixtures, never contacted."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("independence", Path(__file__).with_name("verify-panel-independence.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def owner(kind, name, uid):
    return {"kind": kind, "name": name, "uid": uid, "controller": True}


def pod(app, uid, controller):
    return {
        "metadata": {"name": "zomboid-0" if app == "zomboid" else "panel-" + uid, "uid": uid,
                     "labels": {"app.kubernetes.io/name": app}, "ownerReferences": [controller]},
        "spec": {"nodeName": module.EXPECTED_HOST},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                   "containerStatuses": [{"name": app, "ready": True, "restartCount": 0, "containerID": "containerd://" + app,
                                          "imageID": "sha256:fixture", "state": {"running": {"startedAt": "2026-09-30T16:00:00Z"}}}]},
    }


class Scenario:
    def __init__(self, observe=False, game_change=None, timeout=False, foreign_panel=False, replicas=None):
        self.observe, self.game_change, self.timeout = observe, game_change, timeout
        self.replica_sequence = replicas or [1]
        self.current_replicas = 1
        self.deployment_reads = 0
        self.commands = []
        self.game = pod("zomboid", "game-uid", owner("StatefulSet", "zomboid", "sts-uid"))
        self.old = pod("panel", "old", owner("ReplicaSet", "panel-old", "rs-old"))
        self.new = pod("panel", "new", owner("ReplicaSet", "panel-new", "rs-new"))
        if foreign_panel:
            self.old["metadata"]["ownerReferences"][0]["uid"] = "foreign-rs"
        if timeout:
            self.new["status"]["conditions"][0]["status"] = "False"

    def get(self, resource, name=None):
        if resource == "statefulset":
            return {"metadata": {"uid": "sts-uid"}, "spec": {"replicas": 1}}
        if resource == "pod":
            game = copy.deepcopy(self.game)
            if self.game_change and self.deployment_reads >= 2:
                if self.game_change == "pod_uid":
                    game["metadata"]["uid"] = "replaced-game"
                else:
                    state = game["status"]["containerStatuses"][0]
                    if self.game_change == "container_id": state["containerID"] = "containerd://replaced"
                    if self.game_change == "restarts": state["restartCount"] = 1
                    if self.game_change == "started_at": state["state"]["running"]["startedAt"] = "2026-09-30T16:01:00Z"
            return game
        if resource == "deployment":
            self.deployment_reads += 1
            self.current_replicas = self.replica_sequence[min(self.deployment_reads - 1, len(self.replica_sequence) - 1)]
            return {"metadata": {"uid": "dep-uid", "generation": 2 if self.observe and self.deployment_reads > 1 else 1},
                    "spec": {"replicas": self.current_replicas, "strategy": {"type": "Recreate"}}}
        if resource == "pods":
            if self.current_replicas == 0:
                return {"items": []}
            return {"items": [copy.deepcopy(self.old if self.deployment_reads == 1 else self.new)]}
        if resource == "replicasets":
            return {"items": [{"metadata": {"name": "panel-" + suffix, "uid": "rs-" + suffix,
                                           "ownerReferences": [owner("Deployment", "panel", "dep-uid")]}} for suffix in ("old", "new")]}
        raise AssertionError("Unexpected resource")

    def command(self, label, arguments, timeout=20):
        self.commands.append(arguments)
        if arguments[0] == "exec":
            return '{"players_count":0}'
        if arguments[0] == "delete":
            return "deleted"
        raise AssertionError("Unexpected command")

    def run(self):
        args = SimpleNamespace(kubeconfig="/private/operator.yaml", observe_only=self.observe, wait_seconds=1 if self.timeout else 180)
        with contextlib.ExitStack() as patches:
            patches.enter_context(patch.object(module.os, "geteuid", return_value=0))
            patches.enter_context(patch.object(module.socket, "gethostname", return_value=module.EXPECTED_HOST))
            patches.enter_context(patch.object(module.Path, "is_file", return_value=True))
            patches.enter_context(patch.object(module.time, "sleep"))
            patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if self.timeout:
                patches.enter_context(patch.object(module.time, "monotonic", side_effect=range(100)))
            verifier = module.Verifier(args)
            verifier.get = self.get
            verifier.command = self.command
            verifier.verify()
            return verifier.report


class IndependenceTests(unittest.TestCase):
    def test_default_deletes_only_one_panel_pod_with_normal_grace(self):
        scenario = Scenario()
        report = scenario.run()
        self.assertTrue(report["ok"] and report["game_unchanged"])
        self.assertEqual(report["game_before"], report["game_after"])
        self.assertEqual([command for command in scenario.commands if command[0] == "delete"],
                         [["delete", "pod", "panel-old", "--wait=false"]])

    def test_observer_allows_planned_new_generation_without_delete(self):
        scenario = Scenario(observe=True)
        report = scenario.run()
        self.assertTrue(report["ok"])
        self.assertFalse(report["delete_attempted"])
        self.assertTrue(all(command[0] == "exec" for command in scenario.commands))
        self.assertNotEqual(report["panel_before"]["uid"], report["panel_after"]["uid"])

    def test_observer_waits_through_offline_backup_and_returns_to_one_ready_replica(self):
        scenario = Scenario(observe=True, replicas=[1, 0, 0, 1, 1])
        report = scenario.run()
        self.assertTrue(report["ok"] and report["game_unchanged"])
        self.assertEqual(scenario.deployment_reads, 5)
        self.assertEqual(report["game_before"], report["game_after"])
        self.assertFalse(any(command[0] == "delete" for command in scenario.commands))
        self.assertTrue(report["rcon_before"]["ok"] and report["rcon_after"]["ok"])

    def test_zero_replicas_remains_invalid_at_baseline_final_and_default_restart(self):
        for observe, replicas in ((True, [0]), (True, [1, 1, 0]), (False, [1, 0])):
            with self.subTest(observe=observe, replicas=replicas), self.assertRaisesRegex(module.VerificationError, "one replica"):
                Scenario(observe=observe, replicas=replicas).run()

    def test_observer_rejects_extra_replicas_and_game_restart_during_offline_backup(self):
        with self.assertRaisesRegex(module.VerificationError, "one replica"):
            Scenario(observe=True, replicas=[1, 2]).run()
        with self.assertRaisesRegex(module.VerificationError, "Game identity"):
            Scenario(observe=True, replicas=[1, 0, 1], game_change="restarts").run()

    def test_each_game_identity_change_fails(self):
        for field in ("pod_uid", "container_id", "restarts", "started_at"):
            with self.subTest(field=field), self.assertRaisesRegex(module.VerificationError, "Game changed"):
                Scenario(game_change=field).run()

    def test_unowned_panel_refused_before_mutation(self):
        scenario = Scenario(foreign_panel=True)
        with self.assertRaisesRegex(module.VerificationError, "not owned"):
            scenario.run()
        self.assertFalse(any(command[0] == "delete" for command in scenario.commands))

    def test_replacement_timeout_never_repeats_delete(self):
        scenario = Scenario(timeout=True)
        with self.assertRaisesRegex(module.VerificationError, r"wait budget \(1s\)"):
            scenario.run()
        self.assertEqual(sum(command[0] == "delete" for command in scenario.commands), 1)

    def test_only_read_only_observer_can_extend_wait_to_updater_deadline(self):
        for arguments, expected in (([], 180), (["--observe-only"], 180),
                                    (["--observe-only", "--wait-seconds", "900"], 900),
                                    (["--wait-seconds", "180"], 180)):
            with self.subTest(arguments=arguments), patch("sys.argv", ["verify-panel-independence.py", *arguments]):
                self.assertEqual(module.parse_args().wait_seconds, expected)
        for arguments in (["--wait-seconds", "181"], ["--wait-seconds", "0"],
                          ["--observe-only", "--wait-seconds", "901"], ["--observe-only", "--wait-seconds", "0"]):
            with self.subTest(arguments=arguments), patch("sys.argv", ["verify-panel-independence.py", *arguments]), \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                module.parse_args()
            self.assertEqual(raised.exception.code, 2)

    def test_rcon_wrapper_discards_player_names(self):
        output = io.StringIO()
        command = Mock(return_value="Players connected (2):\n-Alice\n-Bob")
        with patch("runpy.run_path", return_value={"rcon": command}), contextlib.redirect_stdout(output):
            exec(module.RCON_PLAYERS, {})
        command.assert_called_once_with("players")
        self.assertEqual(json.loads(output.getvalue()), {"players_count": 2})
        self.assertNotIn("Alice", output.getvalue())
        self.assertNotIn("Bob", output.getvalue())

    def test_malformed_rcon_count_refused(self):
        args = SimpleNamespace(kubeconfig="/private/operator.yaml", observe_only=True, wait_seconds=180)
        verifier = module.Verifier(args)
        for count in (None, True, -1, "0"):
            with self.subTest(count=count):
                verifier.command = Mock(return_value=json.dumps({"players_count": count}))
                with self.assertRaises(module.VerificationError):
                    verifier.players()


if __name__ == "__main__":
    unittest.main()
