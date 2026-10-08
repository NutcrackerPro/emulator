# Nutcracker Emulator

A private, local launcher for a real Windows virtual machine on an Apple Silicon Mac. Built around **UTM**, an existing free, open-source virtual machine app. This project supplies the launcher and setup tools; it does not contain Windows, a Windows licence, or a custom replacement for the UTM hypervisor.

**Current status:** launcher implemented; Windows installation, Steam login, and Blue Archive gameplay have not been verified. The launcher displays actual UTM status and never simulates a running desktop. No paid services are required by the launcher and no purchases have been made.

## Start the private launcher

Requires macOS and Python 3.9 or later. In this downloaded repository folder, run:

```sh
python3 nutcracker.py
```

It opens a dashboard at `http://127.0.0.1:8765`. Keep the Terminal window open; press Control-C to stop. If the port is already used, run `python3 nutcracker.py --port 8766`.

To use the double-click launcher after downloading from GitHub, first make it executable:

```sh
chmod +x "Launch Nutcracker.command"
```

Then double-click **Launch Nutcracker.command** in Finder. The dashboard works only while the local server runs. Opening `index.html` or putting it on GitHub Pages does not start a Windows computer.

## Create the Windows machine

1. Download **UTM for macOS** from its [official site](https://mac.getutm.app/). The GitHub download is free; the paid App Store edition is optional. Install it in `/Applications` or `~/Applications`.
2. Obtain an official [Windows 11 Arm64 ISO](https://www.microsoft.com/en-us/software-download/windows11arm64). **The download does not include a Windows licence.** Use a valid licence you already own. This project does not buy a licence, remove activation, or bypass installation requirements. If you do not have a suitable licence, the completely free Windows route remains blocked.
3. Follow the [official UTM Windows guide](https://docs.getutm.app/guides/windows/). Create a **virtualized ARM64 Windows guest**, not an emulated x86 PC. On a 24 GB Mac, a starting allocation of 8 GB RAM, 4 CPU cores, and 80 GB of virtual storage leaves resources for macOS. These are starting settings, not a game-performance guarantee; plan additional disk space for installers and games.
4. Complete Windows setup yourself, including Microsoft licence terms. Install UTM's official Windows guest drivers. Keep networking at the default shared/NAT setting; avoid bridged networking and port forwarding unless you intend to expose the guest. Disable shared folders and clipboard sharing if you want stronger separation from your Mac.
5. Refresh the dashboard. Choose your VM, then use **Start Windows**. macOS may ask you to allow Terminal/Python to control UTM: review that prompt yourself. **Shut down Windows** sends a graceful guest shutdown request; it does not force power off. If the guest ignores it, shut down from Windows itself.

Your Windows desktop opens in the **UTM application**, not inside the webpage. This project has no browser remote-desktop service.

## Install Steam in Windows

Use the [official Steam installer](https://store.steampowered.com/about/), or copy `steam-setup.ps1` into your guest and review it. Run it in Windows PowerShell:

```powershell
powershell -NoProfile -File .\steam-setup.ps1
```

If Windows blocks the downloaded script, review its contents and use Windows' normal file-unblocking flow only if you trust it. There is no execution-policy bypass in this project.

The script downloads Valve's installer over HTTPS, checks its Authenticode signature, and opens the normal installation wizard. You accept any terms and sign in directly in Steam. It also writes a local `.runtime/diagnostics.json` report without account details or licence keys. For diagnostics without an installer download, add `-DiagnosticsOnly`.

## Blue Archive: compatibility remains unverified

[Blue Archive on Steam](https://store.steampowered.com/app/3557620/Blue_Archive/) is free to play with optional purchases. Its listing specifies Windows x64, DirectX 11, and **kernel-level Nexon Game Security**. Microsoft notes that [Windows Arm games may fail when anticheat drivers lack Arm support](https://support.microsoft.com/en-us/windows/experience/platform-variants/windows-arm-based-pcs-faq). No official evidence found establishes that Blue Archive works in Windows Arm under UTM.

UTM 5 introduced **experimental** Windows graphics acceleration. [Check the official release notes](https://github.com/utmapp/UTM/releases) for the matching host/guest-driver versions and limitations before using a beta; stable and beta versions do not have identical graphics capabilities. Having Steam installed does not prove a game can run.

Test Windows boot, guest graphics drivers, Steam login, game launch, sound, input, and at least one real gameplay session before calling the setup working. Record actual results in `VALIDATION.md`. Do not bypass anticheat to make a test pass.

Blue Archive's [official Steam Deck support](https://forum.nexon.com/bluearchive/board_view?board=1076&stickyBoard=1&thread=3429786) is evidence for that platform; it does not establish support on macOS or Windows Arm. This project promises neither every Steam game nor perfect performance.

### A licence-free alternative to investigate

[BlueStacks Air](https://www.bluestacks.com/mac) is a free Android emulator for Apple Silicon Macs. Its [official release notes](https://support.bluestacks.com/hc/en-us/articles/32646860057357-Release-Notes-BlueStacks-Air) mention Blue Archive compatibility improvements for the Japanese edition. This may offer a free route for the mobile edition without Windows. It is a different edition from Steam, and gameplay on this Mac has not been tested. Nothing in this project installs it or subscribes to its optional paid services.

## Privacy

- This repository was created as **private**. Keep it private.
- The server binds only to `127.0.0.1`. It checks the Host and Origin headers and requires a fresh per-run token for VM actions. It is not a remotely accessible game server or protection against other software already running as your macOS user.
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

Automated checks cover the launcher and local access controls using mocked UTM responses. They are not Windows boot or game tests. The companion code uses Python's standard library and local HTML/CSS/JavaScript; no package installation is required.
