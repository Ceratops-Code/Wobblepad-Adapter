# Testing

WobblePad separates repository validation, automated tests, and delivery. The
SDLC contract owns the commands. `scripts/validate-repository.py` runs repository
checks and Android lint, while `scripts/run-tests.py` runs repository-script,
Kotlin/JVM, and Windows Python test groups. Each retained result identifies the
exact source content it exercised.

## Feature-to-observation map

| Supported behavior | Test or observation | Current coverage |
| --- | --- | --- |
| Accept only 20-byte `0x41 … 0x42` packets and decode nine signed little-endian fields | `PacketParserTest` checks the observed frame and malformed boundaries | Automated JVM test |
| Derive independent X/Y axes from five calibration poses and reject unsafe calibration data | `JoystickMapperTest` checks cardinal projection plus missing, invalid, and collinear captures | Automated JVM test |
| Apply radial dead zone, time-based smoothing, full-scale clamping, and digital-key hysteresis | `JoystickMapperTest` supplies controlled sensor values and nanosecond timestamps | Automated JVM test |
| Apply independent left, right, up, and down sensitivity without changing the center dead zone | Android `JoystickMapperTest` and Windows `MapperTests` exercise the same cardinal vectors | Automated JVM and Python tests |
| Discover the board, subscribe once, receive notifications, refresh the BoBo battery level, clear stale battery state, and recover a stalled stream | Observe a physical supported board, battery display, and packet counter on an Android device | Hardware qualification required after BLE changes |
| Persist calibration only for the selected Bluetooth address | Calibrate, restart the app, reconnect the same board, then try a different address | Hardware qualification required after storage changes |
| Move the on-screen dot in the physical tilt direction | Capture all five poses and compare left, right, forward, and backward movement | Hardware qualification required after mapper or UI changes |
| Create, automatically restart, and release virtual gamepad, arrow-key, or simultaneous input through Shizuku | Verify both input devices, independent profiles, automatic permission handling, mode switching, repeated arrow pulses, disconnect, and service failure | Hardware qualification required after controller-output changes |
| Emit only Windows arrow transitions and release held keys on stop | `ArrowKeyEmitterTests` records press/release calls without injecting global input | Automated Python test |
| Automatically discover, connect, subscribe, refresh battery state, and reconnect through Windows BLE | Exercise a physical board with the packaged Windows application | Windows hardware qualification required after BLE changes |
| Keep sensor data local unless the user explicitly exports CSV | Inspect the merged manifest and exercise Android's document picker | Repository validation plus manual Android observation |

Historical prototype captures live under the ignored `Prototype/` directory.
They do not qualify a newer commit and must not be copied into current results.

## Commands and retained results

Run the locked workflow from the repository root:

```text
uv sync --project scripts --locked
uv run --project scripts --locked python scripts/validate-repository.py
uv run --project scripts --locked python scripts/run-tests.py
```

Validation replaces `.test-results/validation.json`. Tests replace the aggregate
`.test-results/tests.json` and the repository-script, Android JVM, and Windows
Python group records under `.test-results/groups/`.
These records stay local and are ignored by Git, so running the checks leaves
source clean.
The SDLC contract declares the canonical Ceratops result schema for validation,
tests, Android and Windows builds, and platform deployment; the lifecycle runner
validates each successful command's complete JSON output before accepting it.
The test runner rejects a zero-test JVM run and retains each case outcome and a
digest of every nonignored source input. Aggregate group references include the
SHA-256 digest of the exact group record. `sourceCommit` names the latest commit
that changed non-result source, so a later commit containing only refreshed
records does not make the next run rewrite those records.

Each runner writes `running` before executing work, then atomically replaces it
with `passed`, `failed`, or `blocked`. An interrupted attempt therefore cannot
leave an older passing status. Diagnostic logs under
`.test-results/evidence/` are local and ignored.

APK assembly and device installation are delivery operations:

```text
uv run --project scripts --locked python scripts/build-android.py
uv run --project scripts --locked python scripts/deploy-android.py [--serial DEVICE]
```

The build command emits bounded JSON on standard output and leaves the APK in
Gradle's standard ignored output directory. Deployment requires that APK and
never builds an implicit replacement.

Windows packaging and current-user installation are separate delivery
operations with the same prerequisite rule:

```text
uv run --project windows-app --locked python scripts/build-windows.py
uv run --project scripts --locked python scripts/deploy-windows.py
```

The Windows build produces `.build/windows/WobblePad-Windows.zip`; installation
replaces only `%LOCALAPPDATA%\Programs\WobblePad` and leaves calibration under
the separate `%LOCALAPPDATA%\WobblePad` settings directory.
