#!/usr/bin/env python3
"""Run repository validation, excluding tests, in the scripts project's uv environment.

SDLC invokes this script with the locked project under scripts.
uv owns dependency synchronization; this entrypoint never installs
dependencies or runs test suites. Failed-check evidence survives for diagnosis
and is removed after success. Test commands belong in SDLC tests operations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULT_FILE = ROOT / ".test-results" / "validation.json"
DEFAULT_EVIDENCE_FILE = ROOT / ".test-results" / "evidence" / "validation.log"

CHECK_DEFINITIONS = [{'id': 'ruff',
  'command': ['{python}',
              '-m',
              'ruff',
              'check',
              '.',
              '--config',
              'scripts/pyproject.toml'],
  'cwd': '.',
  'exclusive': False},
 {'id': 'mypy',
  'command': ['{python}',
              '-m',
              'mypy',
              '--config-file',
              'scripts/pyproject.toml'],
  'cwd': '.',
  'exclusive': False},
 {'id': 'yaml-lint',
  'command': ['{python}',
              '-m',
              'yamllint',
              '.',
              '--config-file',
              'scripts/.yamllint.yml'],
  'cwd': '.',
  'exclusive': False},
 {'id': 'actionlint',
  'command': ['{python}', 'scripts/run-actionlint.py'],
  'cwd': '.',
  'exclusive': False},
 {'id': 'npm-markdown-lint',
  'command': ['{npm}', '--prefix', 'scripts', 'run', 'lint:markdown'],
  'cwd': '.',
  'exclusive': False},
 {'id': 'android-lint',
  'command': ['{gradle}', '--no-daemon', ':app:lintDebug'],
  'cwd': '.',
  'exclusive': False},
 {'id': 'android-build',
  'command': ['{python}', 'scripts/build-android.py'],
  'cwd': '.',
  'exclusive': False}]
COMMAND_NOT_FOUND_EXIT_CODE = 127


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
    """Return one successful Git query without exposing command failures."""

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


def portable_evidence_path(path: pathlib.Path | None) -> str | None:
    """Return a repository-relative evidence path or omit external diagnostics."""

    if path is None:
        return None
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None


def validation_record(
    status: str,
    source: dict[str, Any],
    checks: list[dict[str, Any]],
    evidence_file: pathlib.Path | None,
) -> dict[str, Any]:
    """Build one portable repository-validation result."""

    return {
        "schema": "ceratops-repository-stage-result.v1",
        "stage": "validation",
        "status": status,
        "source": source,
        "checks": checks,
        "evidence": portable_evidence_path(evidence_file),
    }


def command(definition: dict[str, object], temporary_root: pathlib.Path) -> list[str]:
    """Resolve portable executable tokens without shell parsing."""

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    pnpm = "pnpm.cmd" if sys.platform == "win32" else "pnpm"
    pwsh = "pwsh.exe" if sys.platform == "win32" else "pwsh"
    values = {
        "{python}": sys.executable,
        "{npm}": npm,
        "{pnpm}": pnpm,
        "{pwsh}": pwsh,
        "{temp}": str(temporary_root),
        "{gradle}": str(ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")),
    }
    raw_command = definition["command"]
    if not isinstance(raw_command, list):
        raise TypeError("check command must be a list")
    resolved: list[str] = []
    for raw in raw_command:
        value = str(raw)
        for token, replacement in values.items():
            value = value.replace(token, replacement)
        resolved.append(value)
    return resolved


def prepare_temporary_directories(
    argv: list[str], temporary_root: pathlib.Path
) -> None:
    """Create only contract-declared working directories inside the owned root."""

    resolved_root = temporary_root.resolve()
    for index, value in enumerate(argv[:-1]):
        if value != "--temp-root":
            continue
        path = pathlib.Path(argv[index + 1]).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError:
            continue
        path.mkdir(parents=True, exist_ok=True)


def cleanup_evidence(
    evidence_file: pathlib.Path,
    *,
    prune_default_parent: bool,
) -> None:
    """Remove stale failure evidence after success and only its owned directory."""

    evidence_file.unlink(missing_ok=True)
    evidence_file.with_name(f".{evidence_file.name}.tmp").unlink(missing_ok=True)
    if prune_default_parent:
        try:
            evidence_file.parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            if not evidence_file.parent.is_dir() or any(
                evidence_file.parent.iterdir()
            ):
                return
            raise


def child_evidence(argv: list[str], temporary_root: pathlib.Path) -> list[str]:
    """Retain declared child-validator evidence before temporary cleanup."""

    paths: list[pathlib.Path] = []
    for index, value in enumerate(argv):
        if value == "--evidence-file" and index + 1 < len(argv):
            paths.append(pathlib.Path(argv[index + 1]))
        elif value.startswith("--evidence-file="):
            paths.append(pathlib.Path(value.partition("=")[2]))
    retained: list[str] = []
    temporary_root = temporary_root.resolve()
    for path in paths:
        resolved = path.resolve()
        try:
            resolved.relative_to(temporary_root)
        except ValueError:
            continue
        if resolved.is_symlink() or not resolved.is_file():
            continue
        retained.extend(
            (
                f"child_evidence: {resolved.relative_to(temporary_root).as_posix()}",
                resolved.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return retained


def main() -> int:
    """Run contract-selected checks in order and emit one bounded result."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-file", type=pathlib.Path)
    args = parser.parse_args()
    repo_root = ROOT
    evidence_file = (
        args.evidence_file.expanduser().resolve()
        if args.evidence_file
        else DEFAULT_EVIDENCE_FILE
    )
    pending_source: dict[str, Any] = {"contentSha256": None, "sourceCommit": None}
    pending_checks = [
        {"id": str(definition["id"]), "status": "pending"}
        for definition in CHECK_DEFINITIONS
    ]
    write_json(
        RESULT_FILE,
        validation_record("running", pending_source, pending_checks, evidence_file),
    )
    try:
        source = source_identity()
    except (OSError, subprocess.SubprocessError) as exc:
        checks = [{"id": "source-identity", "status": "blocked"}]
        write_json(
            RESULT_FILE,
            validation_record("blocked", pending_source, checks, evidence_file),
        )
        print(f"Could not identify repository source: {exc}", file=sys.stderr)
        return 2

    completed_checks: list[dict[str, Any]] = []
    write_json(
        RESULT_FILE,
        validation_record("running", source, pending_checks, evidence_file),
    )
    with tempfile.TemporaryDirectory(prefix="repository-validation-") as temporary:
        temporary_root = pathlib.Path(temporary)
        child_environment = gradle_environment()
        child_environment["PYTHONPYCACHEPREFIX"] = str(temporary_root / "python-cache")
        for definition in CHECK_DEFINITIONS:
            argv = command(definition, temporary_root)
            prepare_temporary_directories(argv, temporary_root)
            raw_cwd = definition["cwd"]
            if not isinstance(raw_cwd, str):
                raise TypeError("check cwd must be a string")
            cwd = repo_root.joinpath(*pathlib.PurePosixPath(raw_cwd).parts)
            try:
                result = subprocess.run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    env=child_environment,
                )
            except OSError as exc:
                result = subprocess.CompletedProcess(
                    argv,
                    COMMAND_NOT_FOUND_EXIT_CODE,
                    "",
                    f"{type(exc).__name__}: {exc}",
                )
            if result.returncode == 0:
                completed_checks.append(
                    {"id": str(definition["id"]), "status": "passed", "exitCode": 0}
                )
                continue
            evidence_file.parent.mkdir(parents=True, exist_ok=True)
            partial = evidence_file.with_name(f".{evidence_file.name}.tmp")
            retained_child_evidence = child_evidence(argv, temporary_root)
            partial.write_text(
                "\n".join(
                    (
                        f"check: {definition['id']}",
                        f"exit_code: {result.returncode}",
                        f"cwd: {cwd}",
                        "command: " + json.dumps(argv, separators=(",", ":")),
                        "stdout:",
                        result.stdout or "",
                        "stderr:",
                        result.stderr or "",
                        *retained_child_evidence,
                    )
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            partial.replace(evidence_file)
            failed_status = (
                "blocked"
                if result.returncode == COMMAND_NOT_FOUND_EXIT_CODE
                else "failed"
            )
            completed_checks.append(
                {
                    "id": str(definition["id"]),
                    "status": failed_status,
                    "exitCode": result.returncode,
                }
            )
            write_json(
                RESULT_FILE,
                validation_record(
                    failed_status, source, completed_checks, evidence_file
                ),
            )
            print(
                json.dumps(
                    {
                        "schema": "ceratops-repository-stage-result.v1",
                        "stage": "validation",
                        "status": failed_status,
                    },
                    separators=(",", ":"),
                )
            )
            return result.returncode if result.returncode > 0 else 1
    try:
        cleanup_evidence(
            evidence_file,
            prune_default_parent=args.evidence_file is None,
        )
    except OSError as exc:
        completed_checks.append(
            {"id": "evidence-cleanup", "status": "failed", "exitCode": 1}
        )
        write_json(
            RESULT_FILE,
            validation_record("failed", source, completed_checks, evidence_file),
        )
        print(
            json.dumps(
                {
                    "check": "evidence-cleanup",
                    "exit_code": 1,
                    "evidence_file": str(evidence_file),
                    "cleanup_error": f"{type(exc).__name__}: {exc}",
                },
                separators=(",", ":"),
            )
        )
        return 1
    final_record = validation_record(
        "passed", source, completed_checks, None
    )
    write_json(RESULT_FILE, final_record)
    print(json.dumps(final_record, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
