# Nutcracker Emulator

A private dashboard and browser display for a real Windows virtual machine on an Apple Silicon Mac. **UTM** runs the VM on your Mac; a local companion connects its screen, keyboard, and mouse to the browser using **noVNC**. This project does not contain Windows, a Windows licence, or a replacement for UTM's virtualization engine.

**Current status:** the real VM's UEFI screen has appeared in the browser, and keyboard input reached it. Windows installation, a Windows desktop, Steam login, and Blue Archive gameplay are still pending. The dashboard reads actual UTM status. No purchases have been made; the launcher, browser console, and UTM download require no paid service. Windows licensing is separate.

## Start the private launcher

Requires macOS and Python 3.9 or later. In this downloaded repository folder, install the one browser-console dependency, then start the companion:

```sh
python3 setup_console.py
python3 nutcracker.py
```

The setup helper installs the pinned `websockets==15.0.1` package only inside this checkout's ignored `.runtime/python-deps` folder. It needs an internet connection. The noVNC 1.7.0 browser library is already bundled locally; no Node.js installation or JavaScript build is needed to use it.

The companion opens a dashboard at `http://127.0.0.1:8765`. Keep the Terminal window open; press Control-C to stop the companion. This does not shut down the VM. The browser-display bridge listens separately on `127.0.0.1:8767`; close another Nutcracker session if that port is occupied. The dashboard port can be changed with `python3 nutcracker.py --port 8766`.

To use the double-click launcher after downloading from GitHub, first make it executable:

```sh
chmod +x "Launch Nutcracker.command"
```

Then double-click **Launch Nutcracker.command** in Finder. The dashboard and console work only while the local companion runs. Opening `index.html` or publishing it on GitHub Pages does not start a Windows computer.

## Create the Windows machine

1. Download **UTM for macOS** from its [official site](https://mac.getutm.app/). The GitHub download is free; the paid App Store edition is optional. Install it in `/Applications` or `~/Applications`.
2. Obtain an official [Windows 11 Arm64 ISO](https://www.microsoft.com/en-us/software-download/windows11arm64). **The download does not include a Windows licence.** Microsoft's [activation instructions](https://support.microsoft.com/en-us/windows/activation/activate-windows) describe choosing **I don't have a product key** during a first installation and obtaining a licence afterward. That normal installer option allows setup to continue; it does not provide a licence or establish permanently free use. This project makes no purchase and does not remove activation or bypass installation requirements.
3. Follow the [official UTM Windows guide](https://docs.getutm.app/guides/windows/). Create a **virtualized ARM64 Windows guest**, not an emulated x86 PC. On a 24 GB Mac, a starting allocation of 8 GB RAM, 4 CPU cores, and 80 GB of virtual storage leaves resources for macOS. These are starting settings, not a game-performance guarantee; plan additional disk space for installers and games.
4. Before starting the VM, configure its browser display as described below. Complete the normal Windows installation and personally review and accept Microsoft's licence terms. Install UTM's official Windows guest drivers. Keep networking at the default shared/NAT setting; avoid bridged networking and port forwarding unless you intend to expose the guest. Disable shared folders and clipboard sharing if you want stronger separation from your Mac.
5. Refresh the dashboard. Choose your VM, then use **Start Windows** and open the live browser console. macOS may ask you to allow Terminal/Python to control UTM: review that prompt yourself. **Shut down Windows** sends a graceful guest shutdown request; it does not force power off. If the guest ignores it, shut down from Windows itself.

### Connect the VM to the browser

The verified configuration uses the free **UTM 4.7.5** download. With the VM stopped, select **virtio-ramfb** without GL as its display device. In UTM's **QEMU** settings, add this additional argument:

```text
-vnc unix:nutcracker-vnc.sock
```

Configure only one VM with that socket name. Keep the relative filename: signed UTM launches QEMU inside its approved shared app-group directory, so the socket resolves to:

```text
~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/nutcracker-vnc.sock
```

The companion checks this one fixed socket; the browser cannot select arbitrary files or servers. UTM's [launch source](https://github.com/utmapp/UTM/blob/v4.7.5/Services/UTMQemuVirtualMachine.swift) sets that working directory. No sandbox, signature, or macOS security setting needs to be disabled.

Use the display without GL because UTM's [QEMU SPICE GL context](https://github.com/utmapp/qemu/blob/v10.0.2-utm/ui/spice-display.c) accepts only its own GL display listener, which excludes VNC. UTM generates `gl=off` for the nongl display. The browser console carries display, keyboard, and mouse; **it provides no browser audio or added 3D acceleration**. UTM's own window remains available. Disconnecting the console leaves the VM running.

The actual setup tested so far is an M4 MacBook Air with 24 GB RAM, UTM 4.7.5, and a VM allocated 8 GB RAM, 4 CPU cores, and 80 GiB of virtual storage. The owner supplied an Arm64 ISO named `Windows11_Client_arm64_en-us_26300_9457.iso`; the filename alone does not verify the installed Windows release. See [VALIDATION.md](VALIDATION.md) for the distinction between completed checks and pending installation/game tests.

## Install Steam in Windows

Use the [official Steam installer](https://store.steampowered.com/about/), or copy `steam-setup.ps1` into your guest and review it. Run it in Windows PowerShell:

```powershell
powershell -NoProfile -File .\steam-setup.ps1
```

If Windows blocks the downloaded script, review its contents and use Windows' normal file-unblocking flow only if you trust it. There is no execution-policy bypass in this project.

The script downloads Valve's installer over HTTPS, checks its Authenticode signature, and opens the normal installation wizard. You accept any terms and sign in directly in Steam. It also writes a local `.runtime/diagnostics.json` report without account details or licence keys. For diagnostics without an installer download, add `-DiagnosticsOnly`.

## Blue Archive: compatibility remains unverified

[Blue Archive on Steam](https://store.steampowered.com/app/3557620/Blue_Archive/) is free to play with optional purchases. Its listing specifies Windows x64, DirectX 11, and **kernel-level Nexon Game Security**. Microsoft notes that [Windows Arm games may fail when anticheat drivers lack Arm support](https://support.microsoft.com/en-us/windows/experience/platform-variants/windows-arm-based-pcs-faq). No official evidence found establishes that Blue Archive works in Windows Arm under UTM.

This configuration uses UTM 4.7.5 without GL. Its [graphics documentation](https://github.com/utmapp/UTM/blob/v4.7.5/Documentation/Graphics.md) does not establish usable Windows guest 3D acceleration. UTM 5 introduced **experimental** Windows graphics acceleration, but that separate beta configuration has not been tested here. [Check the official release notes](https://github.com/utmapp/UTM/releases) for matching host/guest-driver versions and limitations before changing versions. Having Steam installed does not prove a game can run.

Test Windows boot, guest graphics drivers, Steam login, game launch, sound, input, and at least one real gameplay session before calling the setup working. Record actual results in `VALIDATION.md`. Do not bypass anticheat to make a test pass.

Blue Archive's [official Steam Deck support](https://forum.nexon.com/bluearchive/board_view?board=1076&stickyBoard=1&thread=3429786) is evidence for that platform; it does not establish support on macOS or Windows Arm. This project promises neither every Steam game nor perfect performance.

### A licence-free alternative to investigate

[BlueStacks Air](https://www.bluestacks.com/mac) is a free Android emulator for Apple Silicon Macs. Its [official release notes](https://support.bluestacks.com/hc/en-us/articles/32646860057357-Release-Notes-BlueStacks-Air) mention Blue Archive compatibility improvements for the Japanese edition. This may offer a free route for the mobile edition without Windows. It is a different edition from Steam, and gameplay on this Mac has not been tested. Nothing in this project installs it or subscribes to its optional paid services.

## Privacy

- This repository was created as **private**. Keep it private.
- Both the dashboard and WebSocket display bridge bind only to `127.0.0.1`. They check Host and Origin headers; VM actions and console connections require a fresh per-run token. The bridge forwards binary display traffic only to the fixed UTM socket. Its parent directory must belong to your macOS user and deny access to other users; the companion tightens the owned socket to mode `0600` before connecting. This is not protection against software already running as your macOS user.
- No external fonts, analytics, Steam login form, tracking, cloud subscription, or paid backend is included. Official download links lead to their vendors' websites. Steam and Windows themselves still use their own online services.
- Windows disk images, ISO files, diagnostic reports, and secrets are excluded by `.gitignore`. Do not upload VM disks, Steam credentials, or licence keys manually; GitHub's browser uploader does not apply a local `.gitignore` file for you.
- Starting the launcher never buys software, provisions a cloud computer, or installs Windows silently.

## Repository name and hosting

The requested repository is `NutcrackerPro/nutcrackeremulator.github.io`. Its name is not an allocated `nutcrackeremulator.github.io` domain: GitHub user sites require the matching account name. [GitHub Pages is static hosting](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages), so it cannot run Windows or supply a gaming GPU. A private personal source repo does not automatically make a Pages website private. No Pages publishing is configured here.

## Checks

```sh
python3 nutcracker.py --check
python3 -m unittest -v test_nutcracker.py
```

Automated checks cover the companion, console bridge, and local access controls using mocked UTM responses and a test display server. They do not establish Windows boot or game compatibility.

`vendor/novnc/rfb.bundle.js` contains the locally bundled noVNC 1.7.0 runtime. Its [third-party notices](vendor/novnc/NOTICES.txt) retain upstream licences and copyright notices and identify the exact corresponding source. `build-novnc.py` is an optional developer tool for rebuilding that bundle from verified upstream source with esbuild 0.28.2; it is not part of normal startup.
