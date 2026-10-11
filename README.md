# WobblePad

**Turn your balance board into an Android gamepad or Windows arrow controller.**

WobblePad is an experimental Android and Windows bridge for a user-owned BoBo
Wobbly balance board. It connects directly over Bluetooth Low Energy (BLE),
turns the board's continuous tilt stream into calibrated X/Y values, and sends
virtual gamepad or arrow-key input to compatible software.

> [!IMPORTANT]
> This is an independent, unofficial project. It is not affiliated with,
> endorsed by, or sponsored by BO&BO Ltd. “BoBo” and “BoBo Wobbly” are used
> only to identify compatible hardware. Read [DISCLAIMER.md](DISCLAIMER.md)
> before using the app.

## Current status

This repository contains a working prototype, not a production release.

- BLE scan, connection, notification streaming, packet parsing, calibration,
  stall detection, reconnect, battery reading, and CSV export are implemented.
- The direct BLE stream was exercised on a Samsung SM-X210 running Android 16
  for about ten minutes and received 3,204 packets. That test does not prove
  compatibility with every board revision or Android device.
- The virtual controller uses Shizuku to open Android's `/dev/uhid` interface.
  Kernel policy, manufacturer changes, Shizuku state, and the receiving game's
  input support can all affect whether controller output works.
- The Windows app connects through Bleak and emits arrow keys through the
  documented Win32 `SendInput` API. Some games that read only raw hardware
  input may ignore synthesized keys.
- The Windows bundle has passed automated tests and a packaged startup check;
  direct BLE and game-input behavior still require physical qualification.
- There is no signed production APK, Windows installer, or store release yet.
  Build from source while the compatibility surface is still being tested.

## What the Android app does

1. Scans for a device whose name contains `BoBo` or advertises the known custom
   service.
2. Connects with Android BLE/GATT without classic Bluetooth pairing.
3. Enables notifications once on the custom characteristic.
4. Parses each 20-byte notification into nine signed little-endian values.
5. Captures center, left, right, forward, and backward calibration poses.
6. Projects live sensor vectors onto calibrated X/Y axes with smoothing and a
   dead zone.
7. Sends an analog gamepad, repeated arrow-key pulses, or both virtual devices
   together through Android UHID when Shizuku access is available.
8. Offers a **Close BoBo Home** button that uses the same granted Shizuku access
   to stop `com.bobo.home` and release its BLE connection.

Live BLE display, calibration, packet rate, and CSV export work without
Shizuku. Shizuku is needed only for system-visible controller output.

## What the Windows app does

The Windows app continuously scans and connects directly through the operating
system's BLE stack, captures the same five calibration poses, and automatically
emits global arrow-key pulses for as long as a direction remains tilted. It
releases every held key when BLE disconnects or the app closes, and resumes
output after reconnection. Four independent sensitivity sliders control left,
right, forward, and backward movement; the dead-zone and key-repeat interval
have separate sliders. The displayed BoBo battery value is refreshed while
connected and cleared on disconnect. Calibration and settings remain in the
current user's local app-data folder.

## Why Shizuku is needed

Android deliberately isolates ordinary apps from global input injection. A
normal app can receive input and can communicate with BLE peripherals, but it
cannot register an arbitrary system-wide hardware controller for other apps.
WobblePad's small user service asks Shizuku for the Android shell identity and
uses that identity to open `/dev/uhid`, the kernel interface that creates a
virtual HID device.

WobblePad does not bundle Shizuku, root the device, modify firmware, or bypass a
game's input policy. On a non-rooted Android device, Shizuku normally has to be
started again after reboot through Android's wireless-debugging flow. Review
the [Shizuku documentation](https://shizuku.rikka.app/guide/setup/) before
granting access.

## Android requirements

- Android 12 or later (`minSdk 31`)
- Bluetooth LE hardware
- A compatible BoBo Wobbly board
- [Shizuku](https://shizuku.rikka.app/download/) for virtual controller output
- A game or app that accepts Android gamepad or keyboard input

Only one BLE central can commonly hold a peripheral connection at a time.
Disconnect nRF Connect, the official app, and other BLE clients before testing.

## Windows requirements

- Windows 11
- Bluetooth LE hardware
- A compatible BoBo Wobbly board
- Python 3.11 or later and `uv` when building from source

The packaged Windows build includes its Python runtime and dependencies. It
does not require Shizuku or a custom driver.

## Build

Install JDK 17, Android SDK Platform 36, Python 3.11 or later, and
[uv](https://docs.astral.sh/uv/), then clone the repository. The Gradle wrapper
downloads the declared Gradle version.

On Windows:

```powershell
uv sync --project scripts --locked
uv sync --project windows-app --locked
uv run --project scripts --locked python scripts/validate-repository.py
uv run --project scripts --locked python scripts/run-tests.py
uv run --project scripts --locked python scripts/build-android.py
uv run --project scripts --locked python scripts/deploy-android.py
uv run --project windows-app --locked python scripts/build-windows.py
uv run --project scripts --locked python scripts/deploy-windows.py
```

On Linux or macOS:

```bash
uv sync --project scripts --locked
uv run --project scripts --locked python scripts/validate-repository.py
uv run --project scripts --locked python scripts/run-tests.py
uv run --project scripts --locked python scripts/build-android.py
uv run --project scripts --locked python scripts/deploy-android.py
```

The Windows package must be built and installed on Windows because PyInstaller
does not cross-compile Windows executables.

Android Studio can also open the repository root directly. Debug builds use the
standard per-machine Android debug key; no signing key is stored here.

Check and test helpers keep local records under ignored `.test-results/`, bound
to a source-content digest and the latest commit that changed those source
inputs. Supporting logs stay under the
ignored `.test-results/evidence/` directory. Build and deployment emit bounded
JSON to standard output; platform artifacts remain in ignored build
directories. See [TESTING.md](TESTING.md) for the feature-to-observation map and
the hardware checks that remain manual.

## Use on Android

1. Power on the board and keep it awake.
2. Open WobblePad and allow Nearby devices and notification permissions.
3. WobblePad starts scanning automatically when its screen opens. It keeps
   watching in its foreground service, connects when the board appears, shows a
   short connection message, and starts controller output after live packets
   arrive when calibration and Shizuku access are ready. Tap **Disconnect** or
   stop the notification to end automatic discovery.
4. Capture `CENTER`, `LEFT`, `RIGHT`, `UP`, and `DOWN`. `UP` means away from
   you. Hold each pose steady for three seconds.
5. Finish calibration and verify the live dot follows the board.
6. Start Shizuku and choose analog stick, arrow keys, or both. WobblePad requests
   controller access when necessary and keeps controller output running while
   the board is connected. Changing modes recreates the virtual controller
   automatically.
7. Adjust the displayed sensitivity profile. Analog stick and arrow-key modes
   retain separate directional sensitivity and center-dead-zone settings. Both
   mode lets you switch which profile is displayed while keeping both outputs
   active; the arrow profile also provides a repeat interval.
8. Open a controller-compatible app. Return to WobblePad or use its foreground
   notification to stop the bridge.

Calibration is stored locally and keyed to the board's Bluetooth address. It is
not included in this repository and is not transmitted by the app.

## Use on Windows

1. Run `WobblePad.exe`, power on the board, and close other BLE clients. The app
   watches for the board and connects automatically.
2. Capture center, left, right, up/forward, and down/backward; then finish
   calibration.
3. Adjust any directional sensitivity, the center dead zone, or the key-repeat
   interval.
4. Open an arrow-controlled game. Arrow output starts automatically whenever
   the calibrated board is connected.

## BLE protocol

The prototype uses these observed GATT identifiers:

| Purpose | UUID |
| --- | --- |
| Service | `856b152a-734a-5546-bf2b-ed4898184e12` |
| Stream characteristic | `856b152b-734a-5546-bf2b-ed4898184e12` |
| Client Characteristic Configuration | `00002902-0000-1000-8000-00805f9b34fb` |
| Battery service | `0000180f-0000-1000-8000-00805f9b34fb` |

Notifications are enabled with the standard CCCD value `01 00`. Each accepted
packet starts with `0x41`, ends with `0x42`, and contains nine signed 16-bit
little-endian values between those markers. The mapper uses all nine observed
values and learns physical direction from the five calibration poses instead
of assigning undocumented sensor axes.

The format was independently documented by observing BLE data transmitted by
a user-owned device. The repository contains no BO&BO application code,
firmware, artwork, games, backend access, or paid features.

## Privacy and security

The manifest does not request Internet access. BLE data, calibration, and the
bounded recent-packet CSV buffer remain on the Android device. A CSV leaves the
app only when the user chooses a destination through Android's document picker.
Do not publish Bluetooth addresses or raw captures without sanitizing them.

On Android, Shizuku grants elevated local capability. WobblePad limits its user
service to creating and updating its virtual input devices, releases all input
on stop or failure, and does not expose that service to other apps. See
[SECURITY.md](SECURITY.md) for reporting and trust-boundary details.

## Project layout

```text
app/src/main/       Android application, BLE client, calibration, and UHID bridge
app/src/test/       JVM packet-parser and joystick-mapper tests
windows-app/        Windows BLE, calibration, arrow-input app, and Python tests
scripts/            Validation, tests, platform builds, and deployment entry points
sdlc/sdlc.yml       Ceratops repository and deliverable contract
.test-results/      Latest portable validation and automated-test results
.github/            CI, dependency updates, and contribution templates
```

## Contributing

Contributions are welcome within the project's interoperability and safety
scope. Read [CONTRIBUTING.md](CONTRIBUTING.md), the
[Code of Conduct](CODE_OF_CONDUCT.md), and the
[third-party notices](THIRD_PARTY_NOTICES.md).

## License and names

WobblePad source code is licensed under the [Apache License 2.0](LICENSE).
Third-party components retain their own licenses.

WobblePad is the name of this independent project. BoBo, BoBo Wobbly, and any
related marks belong to their respective owners and are used here only to state
hardware compatibility.
