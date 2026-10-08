# Verification record

## Launcher

On 8 October 2026, all **34 automated launcher tests passed**, including live loopback HTTP checks, rejection of remote origins/hosts, session-token validation, private-file/symlink rejection, VM-list parsing, UUID validation, missing UTM handling, and graceful shutdown command construction. VM responses in those tests are mocked; they do not establish guest boot or game compatibility.

The actual host check reported macOS on ARM64, 24 GB RAM, and no installed UTM/registered Windows VM. The UI obtains real host status and handles missing UTM/VMs and an offline server. The Windows PowerShell helper has been reviewed but has not been executed in Windows.

## Actual Windows and games

These checks are **pending**, not passed:

- Suitable Windows licence available.
- UTM installed and ARM64 Windows VM created.
- Windows boots to a real desktop.
- Guest graphics and network drivers installed.
- Steam opens and the owner signs in.
- Blue Archive launches without bypassing anticheat.
- Sound, keyboard/mouse/controller, and a real gameplay session verified.

No Windows VM was supplied with this repository. Do not treat a dashboard screenshot, mocked unit test, installer download, or Steam installation as proof of gameplay.

The owner confirmed no Windows licence is currently available and requested no purchases. Windows installation therefore remains a prerequisite; the free launcher and repository do not supply that licence.

When testing, record the date, UTM release, Windows release, guest graphics driver version, game version, and observed result here. Do not include account names, credentials, or Windows product keys.
