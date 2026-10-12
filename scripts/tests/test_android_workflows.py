"""Behavior tests for the repository-owned Android build and deploy entrypoints."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from typing import Any, cast
from unittest import mock

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def load_script(filename: str, module_name: str) -> types.ModuleType:
    """Load a hyphenated repository script without changing its public filename."""

    specification = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Cannot load {filename}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


BUILD = load_script("build-android.py", "wobblepad_build_android")
DEPLOY = load_script("deploy-android.py", "wobblepad_deploy_android")
RUN_TESTS = load_script("run-tests.py", "wobblepad_run_tests")
VALIDATE = load_script("validate-repository.py", "wobblepad_validate_repository")
ARTIFACT = {
    "type": "android-apk",
    "path": "app-debug.apk",
    "sha256": "0" * 64,
    "size": 1,
}


class BuildAndroidTests(unittest.TestCase):
    def test_gradle_environment_replaces_stale_java_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            java_home = pathlib.Path(temporary)
            executable = java_home / "bin" / "java.exe"
            executable.parent.mkdir()
            executable.write_text("fixture", encoding="utf-8")
            key = mock.MagicMock()
            key.__enter__.return_value = key
            fake_winreg: Any = types.ModuleType("winreg")
            fake_winreg.HKEY_CURRENT_USER = object()
            fake_winreg.HKEY_LOCAL_MACHINE = object()
            fake_winreg.OpenKey = mock.MagicMock(return_value=key)
            fake_winreg.QueryValueEx = mock.MagicMock(
                return_value=(str(java_home), 1)
            )
            with (
                mock.patch.dict(BUILD.os.environ, {"JAVA_HOME": "Z:\\missing"}),
                mock.patch.dict(sys.modules, {"winreg": fake_winreg}),
            ):
                for module in (BUILD, RUN_TESTS, VALIDATE):
                    with self.subTest(module=module.__name__):
                        self.assertEqual(
                            module.gradle_environment(windows=True)["JAVA_HOME"],
                            str(java_home),
                        )

    def test_build_command_lints_before_assembling(self) -> None:
        wrapper = pathlib.Path("gradlew.bat")
        self.assertEqual(
            BUILD.build_command(wrapper),
            [
                str(wrapper),
                "--no-daemon",
                ":app:lintDebug",
                ":app:assembleDebug",
            ],
        )

    def test_build_result_has_structured_identity_without_receipt_path(self) -> None:
        value = BUILD.result("passed", artifact=ARTIFACT)
        self.assertEqual(value["schema"], "ceratops-build-result.v1")
        self.assertEqual(value["status"], "passed")
        self.assertNotIn("receipt", value)

    def test_artifact_identity_binds_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            artifact = root / "app-debug.apk"
            artifact.write_bytes(b"apk")
            identity = BUILD.artifact_identity(artifact, root)
        self.assertEqual(identity["path"], "app-debug.apk")
        self.assertEqual(identity["type"], "android-apk")
        self.assertEqual(identity["size"], 3)
        self.assertEqual(
            identity["sha256"],
            "dd37c2d7274f7ea982cb83390c36918fee9ce8889073c44b68cdc00bdb8c3e04",
        )

    def test_main_keeps_gradle_logs_out_of_structured_stdout(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            wrapper = pathlib.Path(temporary) / "gradlew.bat"
            wrapper.write_text("fixture", encoding="utf-8")
            apk = pathlib.Path(temporary) / "app-debug.apk"
            apk.write_bytes(b"apk")
            completed = BUILD.subprocess.CompletedProcess(
                [str(wrapper)], 0, "gradle output\n", "gradle warning\n"
            )
            with (
                mock.patch.object(BUILD, "gradle_wrapper", return_value=wrapper),
                mock.patch.object(BUILD, "APK", apk),
                mock.patch.object(BUILD.subprocess, "run", return_value=completed),
                mock.patch.object(
                    BUILD,
                    "artifact_identity",
                    return_value=ARTIFACT,
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = BUILD.main([])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "passed")
        self.assertIn("gradle output", stderr.getvalue())
        self.assertIn("gradle warning", stderr.getvalue())


class DeployAndroidTests(unittest.TestCase):
    def test_connected_devices_excludes_offline_and_unauthorized(self) -> None:
        output = """List of devices attached
tablet:37111 device product:gta9 model:SM_X210
phone offline
other unauthorized

"""
        self.assertEqual(DEPLOY.connected_devices(output), ["tablet:37111"])

    def test_single_connected_device_is_selected(self) -> None:
        self.assertEqual(DEPLOY.select_device(["tablet:37111"], None), "tablet:37111")

    def test_explicit_connected_device_is_selected(self) -> None:
        self.assertEqual(
            DEPLOY.select_device(["usb", "tablet:37111"], "tablet:37111"),
            "tablet:37111",
        )

    def test_ambiguous_device_inventory_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            DEPLOY.select_device(["usb", "tablet:37111"], None)

    def test_missing_requested_device_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not connected"):
            DEPLOY.select_device(["usb"], "tablet:37111")

    def test_deploy_result_identifies_target_and_artifact(self) -> None:
        value = DEPLOY.result(
            "passed",
            serial="tablet:37111",
            artifact=ARTIFACT,
        )
        self.assertEqual(value["schema"], "ceratops-deployment-result.v1")
        self.assertEqual(value["target"], "tablet:37111")
        self.assertEqual(value["artifact"], ARTIFACT)


class ResultRecordTests(unittest.TestCase):
    def test_android_contract_accepts_the_combined_build_record(self) -> None:
        contract = yaml.safe_load(
            (SCRIPTS.parent / "sdlc" / "sdlc.yml").read_text(encoding="utf-8")
        )
        required = contract["deliverables"]["apps"]["wobblepad-android"][
            "actions"
        ]["validate"]["validation-capabilities"]
        self.assertEqual(set(required), {"android-lint", "android-build"})
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            record = root / "result.json"
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                mock.patch.object(VALIDATE, "RESULT_FILE", record),
                mock.patch.object(VALIDATE, "DEFAULT_EVIDENCE_FILE", root / "failure.log"),
                mock.patch.object(
                    VALIDATE,
                    "source_identity",
                    return_value={"contentSha256": "source", "sourceCommit": "commit"},
                ),
                mock.patch.object(VALIDATE.subprocess, "run", return_value=completed) as run,
                mock.patch.object(sys, "argv", ["validate-repository.py"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for _ in range(2):
                    run.reset_mock()
                    self.assertEqual(VALIDATE.main(), 0)
                    result = json.loads(record.read_text(encoding="utf-8"))
                    passed = {
                        check["id"] for check in result["checks"]
                        if check["status"] == "passed"
                    }
                    self.assertEqual(result["status"], "passed")
                    self.assertTrue(set(required).issubset(passed))
                    android_calls = [
                        call.args[0] for call in run.call_args_list
                        if "scripts/build-android.py" in call.args[0]
                    ]
                    self.assertEqual(len(android_calls), 1)

    def test_combined_android_failure_never_reports_a_pass(self) -> None:
        definition = VALIDATE.CHECK_DEFINITIONS[-1]
        for exit_code, status in ((1, "failed"), (127, "blocked")):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as raw:
                root = pathlib.Path(raw)
                record = root / "result.json"
                with (
                    mock.patch.object(VALIDATE, "CHECK_DEFINITIONS", [definition]),
                    mock.patch.object(VALIDATE, "RESULT_FILE", record),
                    mock.patch.object(VALIDATE, "DEFAULT_EVIDENCE_FILE", root / "failure.log"),
                    mock.patch.object(VALIDATE, "source_identity", return_value={}),
                    mock.patch.object(VALIDATE.subprocess, "run", return_value=
                                      subprocess.CompletedProcess([], exit_code, "", "failure")),
                    mock.patch.object(sys, "argv", ["validate-repository.py"]),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    for _ in range(2):
                        self.assertEqual(VALIDATE.main(), exit_code)
                        payload = json.loads(record.read_text(encoding="utf-8"))
                        self.assertEqual(payload["status"], status)
                        self.assertEqual(payload["checks"], [
                            {"id": name, "status": status, "exitCode": exit_code}
                            for name in ("android-lint", "android-build")
                        ])

    def test_combined_android_interruption_and_fresh_retry(self) -> None:
        boundaries = [("record", number, after)
                      for number in (1, 2, 3) for after in (False, True)]
        boundaries += [("command", 1, after) for after in (False, True)]
        definition = VALIDATE.CHECK_DEFINITIONS[-1]
        real_write = VALIDATE.write_json
        for boundary, number, after in boundaries:
            with self.subTest(boundary=boundary, number=number, after=after):
                with tempfile.TemporaryDirectory() as raw:
                    root = pathlib.Path(raw)
                    record = root / "result.json"
                    counts = {"record": 0, "command": 0}
                    armed = True

                    def interrupt(
                        kind: str, completed: bool,
                        counters: dict[str, int] = counts,
                        selected: tuple[str, int, bool] = (boundary, number, after),
                    ) -> None:
                        nonlocal armed
                        if armed and (kind, counters[kind], completed) == selected:
                            armed = False
                            raise KeyboardInterrupt

                    def write(
                        path: pathlib.Path, payload: dict[str, Any],
                        counters: dict[str, int] = counts,
                    ) -> None:
                        counters["record"] += 1
                        interrupt("record", False)
                        real_write(path, payload)
                        interrupt("record", True)

                    def run(
                        *args: Any, counters: dict[str, int] = counts, **kwargs: Any
                    ) -> subprocess.CompletedProcess[str]:
                        counters["command"] += 1
                        interrupt("command", False)
                        result = subprocess.CompletedProcess([], 0, "", "")
                        interrupt("command", True)
                        return result

                    with (
                        mock.patch.object(VALIDATE, "CHECK_DEFINITIONS", [definition]),
                        mock.patch.object(VALIDATE, "RESULT_FILE", record),
                        mock.patch.object(VALIDATE, "DEFAULT_EVIDENCE_FILE", root / "failure.log"),
                        mock.patch.object(VALIDATE, "source_identity", return_value={}),
                        mock.patch.object(VALIDATE, "write_json", side_effect=write),
                        mock.patch.object(VALIDATE.subprocess, "run", side_effect=run),
                        mock.patch.object(sys, "argv", ["validate-repository.py"]),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            VALIDATE.main()
                        if record.exists():
                            payload = json.loads(record.read_text(encoding="utf-8"))
                            expected = "passed" if (boundary, number, after) == ("record", 3, True) else "running"
                            self.assertEqual(payload["status"], expected)
                        for _ in range(2):
                            self.assertEqual(VALIDATE.main(), 0)
                            payload = json.loads(record.read_text(encoding="utf-8"))
                            self.assertEqual(payload["checks"], [
                                {"id": name, "status": "passed", "exitCode": 0}
                                for name in ("android-lint", "android-build")
                            ])

    def test_validation_record_is_portable_and_separate_from_build(self) -> None:
        value = VALIDATE.validation_record(
            "passed",
            {"contentSha256": "abc", "sourceCommit": "def"},
            [{"id": "android-lint", "status": "passed", "exitCode": 0}],
            None,
        )
        self.assertEqual(value["schema"], "ceratops-repository-stage-result.v1")
        self.assertEqual(value["stage"], "validation")
        self.assertIsNone(value["evidence"])
        self.assertNotIn("artifact", value)

    def test_generated_check_records_are_ignored_by_git(self) -> None:
        paths = [
            ".test-results/validation.json",
            ".test-results/tests.json",
            ".test-results/groups/repository-scripts.json",
        ]
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--verbose", "--", *paths],
            cwd=SCRIPTS.parent,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(len(ignored.stdout.splitlines()), len(paths))
        for line in ignored.stdout.splitlines():
            self.assertTrue(line.startswith(".gitignore:"), line)
        tracked = subprocess.run(
            ["git", "ls-files", "--", ".test-results"],
            cwd=SCRIPTS.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tracked.stdout.strip(), "")

    def test_atomic_result_writer_replaces_previous_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "result.json"
            RUN_TESTS.write_json(path, {"schema": "test", "status": "running"})
            RUN_TESTS.write_json(path, {"schema": "test", "status": "failed"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            temporary_path = path.with_name(f".{path.name}.tmp")
            self.assertFalse(temporary_path.exists())
        self.assertEqual(payload["status"], "failed")

    def test_source_commit_is_used_only_when_source_matches_head(self) -> None:
        for module in (RUN_TESTS, VALIDATE):
            with self.subTest(module=module.__name__):
                with (
                    mock.patch.object(
                        module.subprocess,
                        "run",
                        return_value=module.subprocess.CompletedProcess([], 0),
                    ),
                    mock.patch.object(
                        module.subprocess,
                        "check_output",
                        return_value=b".test-results/tests.json\0",
                    ),
                ):
                    self.assertTrue(module.source_matches_head())
                with mock.patch.object(
                    module.subprocess,
                    "run",
                    return_value=module.subprocess.CompletedProcess([], 1),
                ):
                    self.assertFalse(module.source_matches_head())

    def test_unittest_errors_remain_errors_and_get_portable_case_evidence(self) -> None:
        class BrokenTest(unittest.TestCase):
            def runTest(self) -> None:
                raise RuntimeError("broken")

        result: Any = unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=cast(Any, RUN_TESTS.RecordingResult)
        ).run(unittest.TestSuite([BrokenTest()]))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.failures), 0)
        self.assertEqual(result.cases[0]["status"], "failed")
        self.assertIn("RuntimeError: broken", result.cases[0]["difference"])


if __name__ == "__main__":
    unittest.main()
