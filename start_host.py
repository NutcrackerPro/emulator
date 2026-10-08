#!/usr/bin/env python3
"""Restart this Mac's saved emulator connection and authenticated launcher."""
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

import nutcracker


ROOT = Path(__file__).resolve().parent
SOCKET = Path('/tmp/nutcracker-tailscale-' + str(os.getuid()) + '.sock')


def wait_until_running(status, daemon=None, attempts=60, interval=0.5):
    """Allow the saved connector to finish starting before checking its login."""
    state = None
    for _ in range(attempts):
        if daemon is not None and daemon.poll() is not None:
            return None
        state = status()
        if state and state.get('BackendState') == 'Running':
            return state
        if state and state.get('BackendState') in {'NeedsLogin', 'NeedsMachineAuth'}:
            return state
        time.sleep(interval)
    return state


def watch_owned_daemon(daemon, stopping, failed, lock):
    """Only an owned connector can request a restart of this supervisor."""
    daemon.wait()
    with lock:
        if stopping.is_set():
            return
        failed.set()
        os.kill(os.getpid(), signal.SIGTERM)


def stop_owned_process(process, timeout):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main():
    origin = nutcracker.remote_settings(ROOT)
    daemon = None
    awake = None
    log = None
    stopping = threading.Event()
    failed = threading.Event()
    monitor_lock = threading.Lock()
    previous_term = signal.getsignal(signal.SIGTERM)

    def stop_host(_signal, _frame):
        if not stopping.is_set():
            raise SystemExit(1 if failed.is_set() else 0)

    signal.signal(signal.SIGTERM, stop_host)
    try:
        if origin:
            runtime = ROOT / '.runtime' / 'tailscale'
            cli = runtime / 'tailscale'
            service = runtime / 'tailscaled'
            if runtime.is_symlink() or cli.is_symlink() or service.is_symlink() or not cli.is_file() or not service.is_file():
                print('The saved internet connector is missing. Restore it before starting remote access.', file=sys.stderr)
                return 1
            if SOCKET.exists():
                info = SOCKET.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    print('The private connector socket is unavailable.', file=sys.stderr)
                    return 1
                SOCKET.chmod(0o600)
            def status():
                result = subprocess.run([str(cli), '--socket=' + str(SOCKET), 'status', '--json'],
                                        capture_output=True, text=True, timeout=5, check=False)
                if result.returncode:
                    return None
                return json.loads(result.stdout)
            state = status()
            if state is None:
                log = (runtime / 'daemon.log').open('a', encoding='utf-8')
                os.chmod(runtime / 'daemon.log', 0o600)
                daemon = subprocess.Popen([str(service), '--tun=userspace-networking',
                                           '--socket=' + str(SOCKET), '--statedir=' + str(runtime / 'state'),
                                           '--no-logs-no-support'], stdout=log, stderr=log)
            if not state or state.get('BackendState') != 'Running':
                state = wait_until_running(status, daemon)
            if not state or state.get('BackendState') != 'Running':
                print('Sign in to the internet connector on this Mac before opening Windows from another device.', file=sys.stderr)
                return 1
            SOCKET.chmod(0o600)
            hostname = (state.get('Self') or {}).get('DNSName', '').rstrip('.')
            if origin != 'https://' + hostname:
                print('The internet address changed. Update the saved address and website link on this Mac.', file=sys.stderr)
                return 1
            result = subprocess.run([str(cli), '--socket=' + str(SOCKET), 'funnel', '--bg', '8768'],
                                    capture_output=True, text=True, timeout=10, check=False)
            if result.returncode:
                print('The internet connector needs approval on this Mac. Finish its Funnel setup, then reopen the launcher.', file=sys.stderr)
                return 1
            # Keep the Mac awake while hosting; exiting restores normal idle sleep.
            if Path('/usr/bin/caffeinate').is_file():
                awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if daemon is not None:
                threading.Thread(target=watch_owned_daemon,
                                 args=(daemon, stopping, failed, monitor_lock),
                                 name='nutcracker-connector-watch', daemon=True).start()
        return nutcracker.main(sys.argv[1:])
    except (OSError, ValueError, subprocess.TimeoutExpired, nutcracker.CompanionError):
        print('The emulator connection could not restart. Reopen the launcher on this Mac.', file=sys.stderr)
        return 1
    finally:
        with monitor_lock:
            stopping.set()
        try:
            stop_owned_process(awake, 3)
        finally:
            try:
                stop_owned_process(daemon, 5)
            finally:
                try:
                    if log:
                        log.close()
                finally:
                    signal.signal(signal.SIGTERM, previous_term)


if __name__ == '__main__':
    sys.exit(main())
