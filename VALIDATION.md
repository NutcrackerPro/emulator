# Verification record

## Companion and browser console

On 8 October 2026, all **51 automated tests passed**. Coverage includes loopback HTTP/WebSocket access controls, rejection of remote origins/hosts, session tokens, private-file/symlink rejection, VM-list parsing, UUID validation, missing UTM handling, graceful shutdown command construction, and browser-console forwarding. UTM responses and the display server in those tests are mocked; these results do not establish Windows boot or game compatibility.

A browser test with a mock RFB display server confirmed that the noVNC viewer displays pixels and forwards keyboard and mouse input. This is a client/bridge check, not proof of a Windows desktop.

The implemented browser path is local noVNC 1.7.0 → token- and Origin-checked WebSocket bridge on `127.0.0.1:8767` → one fixed UTM Unix socket. The dashboard listens on `127.0.0.1:8765`. The bridge uses `websockets==15.0.1`, installed by `setup_console.py` inside the checkout's ignored runtime directory. The noVNC runtime and notices are bundled; rebuilding with `build-novnc.py` is optional.

## Actual host and VM

The actual host is an **M4 MacBook Air with 24 GB RAM**. **UTM 4.7.5** is installed, and a real ARM64 VM has been created with **8 GB RAM, 4 CPU cores, and an 80 GiB virtual disk**. It uses `virtio-ramfb` without GL and the additional QEMU argument `-vnc unix:nutcracker-vnc.sock`.

Signed UTM resolves the relative socket inside its shared app-group directory:

```text
~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/nutcracker-vnc.sock
```

A connection to that actual socket returned a valid RFB greeting. The real VM's **UEFI screen and Windows 11 installer appeared in the local browser**. Browser keyboard input selected the installation disc; mouse input completed the language, keyboard, edition, and empty-disk choices. This verifies live Windows installer display and input transport. The console has no browser audio or added 3D acceleration.

The owner supplied an ISO named `Windows11_Client_arm64_en-us_26300_9457.iso`. Its name alone is not verification of the Windows release, channel, or installed build. The actual Windows version must be recorded after installation.

## Actual Windows and games

The owner explicitly approved acceptance of Microsoft's displayed installation licence terms on 8 October 2026. Setup selected Windows 11 Home, used the normal **I don't have a product key** option, and installed onto the new empty 80 GiB virtual disk. Installation reached 83% and restarted; completion and desktop access still need verification.

These checks are **pending**, not passed:

- Windows installation and first-run setup complete.
- Windows boots to a real desktop.
- Guest graphics and network drivers installed.
- Steam opens and the owner signs in.
- Blue Archive launches without bypassing anticheat.
- Sound, keyboard/mouse/controller, and a real gameplay session verified.

The Windows PowerShell helper has been reviewed but has not been executed in Windows. No Windows VM, ISO, licence, or account credentials are supplied in this repository. Do not treat a dashboard screenshot, mocked unit test, UEFI display, installer download, or Steam installation as proof of gameplay.

The owner confirmed that no Windows licence is currently available and requested no purchases. Microsoft's [normal installation instructions](https://support.microsoft.com/en-us/windows/activation/activate-windows) allow a first installation to proceed with **I don't have a product key** and describe obtaining a licence afterward. That option does not supply a licence or establish permanently free Windows use. No activation bypass or purchase has been performed. Licence availability and activation remain unresolved separately from installation progress.

When testing, record the date, UTM release, Windows release, guest graphics driver version, game version, and observed result here. Do not include account names, credentials, or Windows product keys.
