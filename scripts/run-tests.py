#!/usr/bin/env python3
"""Run repository script tests and Android JVM tests as separate groups.

Each attempt first replaces prior passes with ``running`` records. A crash or
interruption therefore cannot leave an older pass reusable. Compact deterministic
records are tracked; complete command output stays in ignored evidence files.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import traceback
import unittest
import xml.etree.ElementTree as ET
from typing import Any, cast

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / ".test-results"
GROUP_ROOT = RESULT_ROOT / "groups"
EVIDENCE_ROOT = RESULT_ROOT / "evidence"
TEST_RESULT = RESULT_ROOT / "tests.json"
SCRIPT_RESULT = GROUP_ROOT / "repository-scripts.json"
ANDROID_RESULT = GROUP_ROOT / "android-jvm.json"
WINDOWS_RESULT = GROUP_ROOT / "windows-python.json"
SCRIPT_TEST_ROOT = ROOT / "scripts" / "tests"
ANDROID_REPORT_ROOT = ROOT / "app" / "build" / "test-results" / "testDebugUnitTest"
WINDOWS_TEST_ROOT = ROOT / "windows-app" / "tests"
WINDOWS_SOURCE_ROOT = ROOT / "windows-app" / "src"


def gradle_environment(windows: bool | None = None) -> dict[str, str]:
    """Replace a stale inherited Java home with a valid Windows setting."""

    environment = os.environ.copy()
    is_windows = os.name == "nt" if windows is None else windows

    def valid_home(value: str | None) -> pathlib.Path | None:
        if not value:
            return None
        home = pathlib.Path(os.path.expandvars(value.strip('"')))
        executable = home / "bin" / ("java.exe" if is_windows else "java")
        return home if executable.is_file() else None

    if valid_home(environment.get("JAVA_HOME")) is not None:
        return environment
    environment.pop("JAVA_HOME", None)
    if not is_windows:
        return environment

    winreg: Any = importlib.import_module("winreg")

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for hive, key_name in locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, "JAVA_HOME")
        except OSError:
            continue
        home = valid_home(str(value))
        if home is not None:
            environment["JAVA_HOME"] = str(home)
            break
    return environment


def git_output(*arguments: str) -> str:
    """Return one successful Git query without leaking local paths."""

    completed = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def source_matches_head() -> bool:
    """Return whether only generated result stores differ from ``HEAD``."""

    tracked = subprocess.run(
        [
            "git",
            "diff",
            "--quiet",
            "HEAD",
            "--",
            ".",
            ":(exclude).build/**",
            ":(exclude).test-results/**",
        ],
        cwd=ROOT,
        check=False,
    )
    if tracked.returncode != 0:
        return False
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    return all(
        raw.decode("utf-8", errors="surrogateescape")
        .replace("\\", "/")
        .startswith((".build/", ".test-results/"))
        for raw in untracked.split(b"\0")
        if raw
    )


def source_identity() -> dict[str, Any]:
    """Hash every nonignored source input, excluding generated result stores."""

    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    paths: list[tuple[str, pathlib.Path]] = []
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if relative.startswith((".build/", ".test-results/")):
            continue
        path = ROOT / relative
        if path.is_file():
            paths.append((relative, path))

    digest = hashlib.sha256()
    for relative, path in sorted(paths):
        content = path.read_bytes()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return {
        "contentSha256": digest.hexdigest(),
        "sourceCommit": (
            git_output(
                "log",
                "-1",
                "--format=%H",
                "--",
                ".",
                ":(exclude).build/**",
                ":(exclude).test-results/**",
            )
            or None
        )
        if source_matches_head()
        else None,
    }


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """Atomically replace one deterministic result and remove its temp file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    """Summarize portable test case states."""

    return {
        "total": len(cases),
        **{
            state: sum(case["status"] == state for case in cases)
            for state in ("passed", "failed", "skipped")
        },
    }


def group_record(
    group: str,
    status: str,
    source: dict[str, Any],
    command: list[str],
    evidence: pathlib.Path,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create one test-group record under the shared stage schema."""

    return {
        "schema": "ceratops-repository-stage-result.v1",
        "stage": "tests",
        "group": group,
        "status": status,
        "source": source,
        "command": command,
        "summary": counts(cases),
        "cases": cases,
        "evidence": evidence.relative_to(ROOT).as_posix(),
    }


def aggregate_record(
    status: str,
    source: dict[str, Any],
    groups: list[tuple[str, pathlib.Path, dict[str, Any]]],
) -> dict[str, Any]:
    """Bind the aggregate to exact group-record bytes."""

    references = []
    for group, path, record in groups:
        references.append(
            {
                "id": group,
                "status": record["status"],
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "schema": "ceratops-repository-stage-result.v1",
        "stage": "tests",
        "status": status,
        "source": source,
        "groups": references,
    }


class RecordingResult(unittest.TextTestResult):
    """Capture unittest outcomes without parsing human-formatted output."""

    cases: list[dict[str, Any]]

    def startTestRun(self) -> None:
        self.cases = []
        super().startTestRun()

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        self.cases.append({"id": test.id(), "status": "passed"})
        super().addSuccess(test)

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        self.cases.append({"id": test.id(), "status": "skipped", "reason": reason})
        super().addSkip(test, reason)

    def addFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        self.cases.append(
            {
                "id": test.id(),
                "status": "failed",
                "difference": "".join(traceback.format_exception(*err))[-2000:],
            }
        )
        super().addFailure(test, err)

    def addError(self, test: unittest.case.TestCase, err: Any) -> None:
        self.cases.append(
            {
                "id": test.id(),
                "status": "failed",
                "difference": "".join(traceback.format_exception(*err))[-2000:],
            }
        )
        super().addError(test, err)


def run_script_tests(source: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run repository script tests through unittest's structured API."""

    evidence = EVIDENCE_ROOT / "repository-scripts.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    command = ["python", "-m", "unittest", "discover", "-s", "scripts/tests"]
    with evidence.open("w", encoding="utf-8", newline="\n") as stream:
        suite = unittest.defaultTestLoader.discover(str(SCRIPT_TEST_ROOT))
        result = unittest.TextTestRunner(
            stream=stream, verbosity=2, resultclass=cast(Any, RecordingResult)
        ).run(suite)
    recording = cast(RecordingResult, result)
    cases = recording.cases
    exit_code = 0 if recording.wasSuccessful() and cases else 1
    status = "passed" if exit_code == 0 else "failed"
    return exit_code, group_record(
        "repository-scripts", status, source, command, evidence, cases
    )


def parse_android_results() -> list[dict[str, Any]]:
    """Read Gradle JUnit XML without treating console text as test evidence."""

    cases: list[dict[str, Any]] = []
    for report in sorted(ANDROID_REPORT_ROOT.glob("*.xml")):
        document = ET.parse(report).getroot()
        suites = [document] if document.tag == "testsuite" else list(document.findall("testsuite"))
        for suite in suites:
            for case in suite.findall("testcase"):
                failure = case.find("failure")
                if failure is None:
                    failure = case.find("error")
                skipped = case.find("skipped")
                status = "failed" if failure is not None else "skipped" if skipped is not None else "passed"
                item: dict[str, Any] = {
                    "id": f"{case.get('classname', '')}.{case.get('name', '')}".strip("."),
                    "status": status,
                }
                if failure is not None:
                    details = "\n".join(
                        part.strip()
                        for part in (failure.get("message", ""), failure.text or "")
                        if part.strip()
                    )
                    item["difference"] = details[:2000]
                cases.append(item)
    return sorted(cases, key=lambda item: str(item["id"]))


def run_android_tests(source: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run the Kotlin/JVM suite and retain its exact JUnit outcomes."""

    wrapper = ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")
    evidence = EVIDENCE_ROOT / "android-jvm.log"
    command = [wrapper.name, "--no-daemon", "--rerun-tasks", ":app:testDebugUnitTest"]
    if not wrapper.is_file():
        return 2, group_record(
            "android-jvm", "blocked", source, command, evidence, []
        )

    shutil.rmtree(ANDROID_REPORT_ROOT, ignore_errors=True)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with evidence.open("w", encoding="utf-8", newline="\n") as stream:
        completed = subprocess.run(
            [str(wrapper), *command[1:]],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=gradle_environment(),
        )
    cases = parse_android_results()
    exit_code = completed.returncode
    if exit_code == 0 and not cases:
        exit_code = 1
    status = "passed" if exit_code == 0 else "failed"
    return exit_code, group_record(
        "android-jvm", status, source, command, evidence, cases
    )


def run_windows_tests(source: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run the Windows app's hardware-free model and input-transition tests."""

    evidence = EVIDENCE_ROOT / "windows-python.log"
    command = windows_test_command()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(WINDOWS_SOURCE_ROOT))
    try:
        with evidence.open("w", encoding="utf-8", newline="\n") as stream:
            suite = unittest.defaultTestLoader.discover(str(WINDOWS_TEST_ROOT))
            result = unittest.TextTestRunner(
                stream=stream, verbosity=2, resultclass=cast(Any, RecordingResult)
            ).run(suite)
    finally:
        sys.path.remove(str(WINDOWS_SOURCE_ROOT))
    recording = cast(RecordingResult, result)
    cases = recording.cases
    exit_code = 0 if recording.wasSuccessful() and cases else 1
    status = "passed" if exit_code == 0 else "failed"
    return exit_code, group_record(
        "windows-python", status, source, command, evidence, cases
    )


def windows_test_command() -> list[str]:
    """Describe the in-process Windows test run without checkout-specific paths."""

    return ["python", "-m", "unittest", "discover", "-s", "windows-app/tests"]


def main() -> int:
    pending_source: dict[str, Any] = {"contentSha256": None, "sourceCommit": None}
    script_evidence = EVIDENCE_ROOT / "repository-scripts.log"
    android_evidence = EVIDENCE_ROOT / "android-jvm.log"
    windows_evidence = EVIDENCE_ROOT / "windows-python.log"
    initial_script = group_record(
        "repository-scripts", "running", pending_source, [], script_evidence, []
    )
    initial_android = group_record(
        "android-jvm", "running", pending_source, [], android_evidence, []
    )
    initial_windows = group_record(
        "windows-python", "running", pending_source, [], windows_evidence, []
    )
    write_json(SCRIPT_RESULT, initial_script)
    write_json(ANDROID_RESULT, initial_android)
    write_json(WINDOWS_RESULT, initial_windows)
    write_json(
        TEST_RESULT,
        aggregate_record(
            "running",
            pending_source,
            [
                ("repository-scripts", SCRIPT_RESULT, initial_script),
                ("android-jvm", ANDROID_RESULT, initial_android),
                ("windows-python", WINDOWS_RESULT, initial_windows),
            ],
        ),
    )

    try:
        source = source_identity()
    except (OSError, subprocess.SubprocessError) as exc:
        blocked_script = group_record(
            "repository-scripts", "blocked", pending_source, [], script_evidence, []
        )
        blocked_android = group_record(
            "android-jvm", "blocked", pending_source, [], android_evidence, []
        )
        blocked_windows = group_record(
            "windows-python", "blocked", pending_source, [], windows_evidence, []
        )
        write_json(SCRIPT_RESULT, blocked_script)
        write_json(ANDROID_RESULT, blocked_android)
        write_json(WINDOWS_RESULT, blocked_windows)
        write_json(
            TEST_RESULT,
            aggregate_record(
                "blocked",
                pending_source,
                [
                    ("repository-scripts", SCRIPT_RESULT, blocked_script),
                    ("android-jvm", ANDROID_RESULT, blocked_android),
                    ("windows-python", WINDOWS_RESULT, blocked_windows),
                ],
            ),
        )
        print(f"Could not identify repository source: {exc}", file=sys.stderr)
        return 2

    initial_script = group_record(
        "repository-scripts", "running", source, [], script_evidence, []
    )
    initial_android = group_record(
        "android-jvm", "running", source, [], android_evidence, []
    )
    initial_windows = group_record(
        "windows-python", "running", source, [], windows_evidence, []
    )
    write_json(SCRIPT_RESULT, initial_script)
    write_json(ANDROID_RESULT, initial_android)
    write_json(WINDOWS_RESULT, initial_windows)
    write_json(
        TEST_RESULT,
        aggregate_record(
            "running",
            source,
            [
                ("repository-scripts", SCRIPT_RESULT, initial_script),
                ("android-jvm", ANDROID_RESULT, initial_android),
                ("windows-python", WINDOWS_RESULT, initial_windows),
            ],
        ),
    )

    script_code, script_record = run_script_tests(source)
    write_json(SCRIPT_RESULT, script_record)
    android_code, android_record = run_android_tests(source)
    write_json(ANDROID_RESULT, android_record)
    windows_code, windows_record = run_windows_tests(source)
    write_json(WINDOWS_RESULT, windows_record)
    status = "passed" if script_code == 0 and android_code == 0 and windows_code == 0 else "failed"
    aggregate = aggregate_record(
        status,
        source,
        [
            ("repository-scripts", SCRIPT_RESULT, script_record),
            ("android-jvm", ANDROID_RESULT, android_record),
            ("windows-python", WINDOWS_RESULT, windows_record),
        ],
    )
    write_json(TEST_RESULT, aggregate)
    print(json.dumps(aggregate, separators=(",", ":")))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
