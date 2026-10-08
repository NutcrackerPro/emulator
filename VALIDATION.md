# Verification record

## Companion and browser console

On 8 October 2026, all **83 automated tests passed**. Coverage includes loopback HTTP/WebSocket access controls, rejection of remote origins/hosts, password hash storage, authenticated sessions, sign-in throttling, logout and session-expiry closure of live WebSockets, safe public-page navigation, session tokens, private-file/symlink rejection, VM-list parsing, UUID validation, missing UTM handling, graceful shutdown command construction, and browser-console forwarding. UTM responses and the display server in those tests are mocked; these results do not establish Windows boot or game compatibility.

A browser test with a mock RFB display server confirmed that the noVNC viewer displays pixels and forwards keyboard and mouse input. This is a client/bridge check, not proof of a Windows desktop.

The implemented browser path is local noVNC 1.7.0 → token- and Origin-checked WebSocket bridge on `127.0.0.1:8767` → one fixed UTM Unix socket. The dashboard listens on `127.0.0.1:8765`. The bridge uses `websockets==15.0.1`, installed by `setup_console.py` inside the checkout's ignored runtime directory. The noVNC runtime and notices are bundled; rebuilding with `build-novnc.py` is optional.

## Actual host and VM

The actual host is an **M4 MacBook Air with 24 GB RAM**. **UTM 4.7.5** is installed, and a real ARM64 VM has been created with **8 GB RAM, 4 CPU cores, and an 80 GiB virtual disk**. It uses `virtio-ramfb` without GL and the additional QEMU argument `-vnc unix:nutcracker-vnc.sock`.

Signed UTM resolves the relative socket inside its shared app-group directory:

```text
~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/nutcracker-vnc.sock
```

A connection to that actual socket returned a valid RFB greeting. The real VM's **UEFI screen and Windows 11 installer appeared in the local browser**. Browser keyboard input selected the installation disc; mouse input completed the language, keyboard, edition, and empty-disk choices. This verifies live Windows installer display and input transport. The console has no browser audio or added 3D acceleration.

The owner supplied an ISO named `Windows11_Client_arm64_en-us_26300_9457.iso`. The installed Windows version was checked inside the guest rather than inferred from that filename.

## Actual Windows and games

The owner explicitly approved acceptance of Microsoft's displayed installation licence terms on 8 October 2026. Setup selected Windows 11 Home, used the normal **I don't have a product key** option, and installed onto the new empty 80 GiB virtual disk. The owner completed first-run account setup, and Windows reached its real desktop in the local browser console. Internet access, browser fullscreen, and keyboard/mouse input work. The display connection remained live through installation restarts.

Guest diagnostics report **Microsoft Windows 11 Home, version 10.0.26300, build 26300**. The **Red Hat VirtIO GPU DOD controller** reports driver **22.7.38.43**. These observations do not establish a Windows release channel or usable 3D acceleration.

The official Steam installer was downloaded to `C:\Windows\Temp\NutcrackerSteamSetup.exe`. Authenticode reported **Valid**, with publisher **Valve Corp**. The normal interactive wizard completed installation in `C:\Program Files (x86)\Steam`. Steam then downloaded its client update and opened to its real sign-in screen in the browser. No account credentials were entered. Installer signature and client startup do not establish game compatibility.

The browser's **Run app** shortcut opened Windows Run, and keyboard input launched the installer. The additional controls wrap at smaller viewport sizes. The current Fullscreen control targets only the live framebuffer. Its accessibility tree contains only the Windows display, with no website toolbar/statusbar, and the visible fullscreen view confirms that layout. Keyboard capture is requested where supported; host/browser-reserved combinations are not promised.

These checks are **pending**, not passed:

- Owner Steam login.
- Blue Archive launches without bypassing anticheat.
- Guest audio, controller support, usable 3D acceleration, and a real gameplay session.
- Windows licence availability and activation.

The Windows PowerShell helper has been reviewed but has not been executed in Windows. No Windows VM, ISO, licence, or account credentials are supplied in this repository. Do not treat a dashboard screenshot, mocked unit test, UEFI display, installer download, or Steam installation as proof of gameplay.

The owner confirmed that no Windows licence is currently available and requested no purchases. Microsoft's [normal installation instructions](https://support.microsoft.com/en-us/windows/activation/activate-windows) allow a first installation to proceed with **I don't have a product key** and describe obtaining a licence afterward. That option does not supply a licence or establish permanently free Windows use. No activation bypass or purchase has been performed. Licence availability and activation remain unresolved separately from installation progress.

When testing, record the date, UTM release, Windows release, guest graphics driver version, game version, and observed result here. Do not include account names, credentials, or Windows product keys.

## Boot and cleanup

The virtual installation-disc drives were removed after installation, preserving the existing NVMe Windows disk. The VM restarted from that disk and reached the installed Windows desktop. Closing and reopening the browser console reconnects to the existing VM.

Known temporary bundle-build downloads/tools, duplicate unbundled source, obsolete setup screenshots, regenerable Python caches, verified installer copies inside Windows, diagnostic probes, and obsolete configuration backups were removed. Owner-downloaded files and installed apps were preserved. Windows ReTrim completed successfully against already unused sectors. The active disk was measured at approximately **46.32 GiB physically allocated** after that operation; its guest volume contained approximately **51.51 GiB of used space**. The ReTrim report is not a claim that all reported trimmed bytes became new free host space.

The supplied local account opened the real, already running Windows desktop after server restart. Signing out returned to the sign-in page; reopening the console while signed out required authentication again. The account record contains only salted PBKDF2-SHA256 hash data, with 600,000 iterations, in a mode-0600 file beneath the mode-0700 ignored runtime directory. No plaintext password is published.

The public GitHub Pages launch page contains static HTML/CSS and an ordinary link to the localhost console. Local account authentication, token, Host, Origin, and frame restrictions remain in place; the VM is not publicly exposed.


## Internet access and display clarity — 8 October 2026

All **111 automated checks passed** with the patched aiohttp 3.14.4 gateway on Python 3.12.14. They cover exact remote Host/Origin checks, Secure remote session cookies, cross-site restrictions, fixed upstreams, duplicate headers, request bounds, WebSocket authentication and binary forwarding, session revocation, and gateway shutdown. The public launch page now points to the Mac’s assigned HTTPS address instead of localhost.

The actual Windows framebuffer measured 1470 × 923 pixels. Native mode rendered it at 1470 × 923 CSS pixels with sharp interpolation; the narrow browser’s prior Fit view reduced it to 327 × 205 pixels (22%), explaining the blurry text. Display quality is set to the highest supported noVNC level. Native/Fit switching was checked against the live VM. Internet verification is recorded separately after the tunnel is active.

The assigned public HTTPS endpoint was verified with valid TLS. Opening its protected console redirected to sign-in; the configured account then connected through the public secure WebSocket to the actual running Windows framebuffer at 1470 × 923. No game performance claim follows from this remote display test.

The optional user login service adds 27 passing focused checks for private file handling, exact service scope, startup waiting, owned-connector recovery, and orderly shutdown.

Final verification: all **138 automated checks passed**. The background job is running from macOS Application Support, with the internet connector owned by its supervisor. Remote sign-in reconnected to the live Windows screen after that host restart. The remote fullscreen accessibility view contained only the Windows display and framebuffer image; controls remained outside it. Browser automation could not reliably exercise reserved fullscreen exit keys, so their cross-browser behavior remains a manual check.
