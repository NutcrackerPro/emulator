#!/usr/bin/env python3
"""Prepare or explicitly install this Mac user's emulator login service."""
import argparse
import errno
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
LABEL = 'com.nutcracker.emulator.host'
HOST_FILES = (
    'start_host.py', 'nutcracker.py', 'console_bridge.py', 'remote_gateway.py',
    'index.html', 'console.html', 'login.html', 'app.js', 'console.js', 'login.js',
    'styles.css', 'steam-setup.ps1',
)
VENDOR_FILES = (
    'rfb.bundle.js', 'LICENSE.txt', 'README.md', 'AUTHORS', 'ATTRIBUTION.md',
    'NOTICES.txt', 'docs/LICENSE.BSD-3-Clause', 'docs/LICENSE.OFL-1.1',
    'docs/LICENSE.BSD-2-Clause', 'docs/LICENSE.MPL-2.0',
)


class ServiceError(Exception):
    pass


def private_directory(path, tighten=True):
    if path.is_symlink():
        raise ServiceError('A service directory cannot be a symbolic link.')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ServiceError('A service directory must belong to the current Mac user.')
    if tighten:
        path.chmod(0o700)
    elif info.st_mode & 0o022:
        raise ServiceError('The login service directory cannot be writable by other users.')


def private_file(path, content=None):
    """Never follow a link or truncate an existing log."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    if content is None:
        flags |= os.O_APPEND
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ServiceError('A service file must be a regular file owned by this user.')
        os.fchmod(descriptor, 0o600)
        if content is not None:
            os.ftruncate(descriptor, 0)
            with os.fdopen(os.dup(descriptor), 'wb') as stream:
                stream.write(content)
    finally:
        os.close(descriptor)


def trusted_file(path):
    if path.is_symlink():
        raise ServiceError('A host bundle source file cannot be a symbolic link.')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ServiceError('Host bundle files must belong to this user and be protected from other users.')
    return info


def trusted_directory(path):
    if path.is_symlink() or not path.is_dir():
        raise ServiceError('A host bundle source directory cannot be a symbolic link.')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ServiceError('Host bundle source directories must belong to this user and be protected from other users.')


def background_path(home=None):
    home = (Path(home) if home is not None else Path.home()).resolve(strict=True)
    library = home / 'Library'
    if library.is_symlink() or not library.is_dir() or library.stat().st_uid != os.getuid():
        raise ServiceError('The current user Library folder is unavailable.')
    return library / 'Application Support' / 'Nutcracker' / 'host'


def copy_bundle_file(source, destination, mutable=False):
    """Share immutable files by inode; preserve the installed host's private state."""
    info = trusted_file(source)
    if destination.is_symlink():
        raise ServiceError('An installed host file cannot be a symbolic link.')
    if destination.exists():
        trusted_file(destination)
        if mutable:
            destination.chmod(0o600)
            return
        if source.samefile(destination):
            return
    private_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.host-copy-', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if not mutable:
            temporary.unlink()
            try:
                os.link(source, temporary, follow_symlinks=False)
            except OSError as error:
                if error.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                    raise
                shutil.copyfile(source, temporary, follow_symlinks=False)
                temporary.chmod(0o700 if info.st_mode & 0o111 else 0o600)
        else:
            shutil.copyfile(source, temporary, follow_symlinks=False)
            temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def copy_bundle_tree(source, destination, mutable=False):
    trusted_directory(source)
    private_directory(destination)
    for child in source.iterdir():
        if child.name == '__pycache__' or child.suffix in {'.pyc', '.pyo'}:
            continue
        if child.is_symlink():
            raise ServiceError('A host bundle dependency or state file cannot be a symbolic link.')
        target = destination / child.name
        if child.is_dir():
            copy_bundle_tree(child, target, mutable=mutable)
        else:
            copy_bundle_file(child, target, mutable=mutable)


def prepare_background_bundle(source_root=ROOT, host_root=None):
    source_root = Path(source_root).resolve(strict=True)
    host_root = Path(host_root) if host_root is not None else background_path()
    # Application Support can contain unrelated apps; only our two directories
    # are tightened. All live files stay outside Desktop/Documents TCC folders.
    private_directory(host_root.parent.parent, tighten=False)
    private_directory(host_root.parent)
    private_directory(host_root)
    runtime = host_root / '.runtime'
    private_directory(runtime)
    source_runtime = source_root / '.runtime'
    trusted_directory(source_runtime)
    trusted_directory(source_runtime / 'tailscale')
    for name in HOST_FILES:
        copy_bundle_file(source_root / name, host_root / name)
    vendor = source_root / 'vendor'
    novnc = vendor / 'novnc'
    for directory in (vendor, novnc, novnc / 'docs'):
        trusted_directory(directory)
    private_directory(host_root / 'vendor')
    private_directory(host_root / 'vendor' / 'novnc')
    for name in VENDOR_FILES:
        copy_bundle_file(novnc / name, host_root / 'vendor' / 'novnc' / name)
    copy_bundle_tree(source_runtime / 'python-deps', runtime / 'python-deps')
    private_directory(runtime / 'tailscale')
    for name in ('tailscale', 'tailscaled'):
        copy_bundle_file(source_runtime / 'tailscale' / name, runtime / 'tailscale' / name)
    copy_bundle_tree(source_runtime / 'tailscale' / 'state', runtime / 'tailscale' / 'state', mutable=True)
    for name in ('auth.json', 'remote.json'):
        copy_bundle_file(source_runtime / name, runtime / name, mutable=True)
    python = (source_runtime / 'python').resolve(strict=True)
    trusted_file(python)
    if not os.access(python, os.X_OK):
        raise ServiceError('The saved Python runtime is unavailable.')
    target_python = runtime / 'python'
    if target_python.is_symlink():
        if target_python.resolve(strict=True) != python:
            raise ServiceError('The installed host points to a different Python runtime.')
    elif target_python.exists():
        raise ServiceError('The installed Python runtime entry must be the expected link.')
    else:
        target_python.symlink_to(python)
    return host_root


def build_configuration(root=ROOT):
    root = Path(root).resolve(strict=True)
    runtime = root / '.runtime'
    private_directory(runtime)
    try:
        python = (runtime / 'python').resolve(strict=True)
    except OSError as error:
        raise ServiceError('Restore the saved Python runtime before preparing the service.') from error
    runner = root / 'start_host.py'
    for path in (python, runner):
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ServiceError('The host program must be a regular file owned by this user and protected from other users.')
    if runner.is_symlink() or not os.access(python, os.X_OK):
        raise ServiceError('The saved host program or Python runtime is unavailable.')
    output = runtime / 'host.stdout.log'
    errors = runtime / 'host.stderr.log'
    private_file(output)
    private_file(errors)
    return {
        'Label': LABEL,
        'ProgramArguments': [str(python), '-B', str(runner), '--no-browser'],
        'WorkingDirectory': str(root),
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 30,
        'Umask': 0o077,
        'StandardOutPath': str(output),
        'StandardErrorPath': str(errors),
    }


def prepare(root=ROOT):
    configuration = build_configuration(root)
    path = Path(configuration['WorkingDirectory']) / '.runtime' / (LABEL + '.plist')
    private_file(path, plistlib.dumps(configuration, fmt=plistlib.FMT_XML, sort_keys=True))
    return path, configuration


def prepare_preview(source_root=ROOT, host_root=None):
    source_root = Path(source_root).resolve(strict=True)
    host_root = Path(host_root) if host_root is not None else background_path()
    configuration = build_configuration(source_root)
    configuration['ProgramArguments'][2] = str(host_root / 'start_host.py')
    configuration['WorkingDirectory'] = str(host_root)
    configuration['StandardOutPath'] = str(host_root / '.runtime' / 'host.stdout.log')
    configuration['StandardErrorPath'] = str(host_root / '.runtime' / 'host.stderr.log')
    path = source_root / '.runtime' / (LABEL + '.plist')
    private_file(path, plistlib.dumps(configuration, fmt=plistlib.FMT_XML, sort_keys=True))
    return path, configuration


def old_source_configuration(configuration, source_root):
    old = dict(configuration)
    source_root = Path(source_root).resolve(strict=True)
    old['ProgramArguments'] = list(configuration['ProgramArguments'])
    old['ProgramArguments'][2] = str(source_root / 'start_host.py')
    old['WorkingDirectory'] = str(source_root)
    old['StandardOutPath'] = str(source_root / '.runtime' / 'host.stdout.log')
    old['StandardErrorPath'] = str(source_root / '.runtime' / 'host.stderr.log')
    return old


def install(path, configuration, source_root=ROOT):
    if sys.platform != 'darwin' or os.geteuid() != os.getuid() or os.getuid() == 0:
        raise ServiceError('Install this login service as the current Mac user, without sudo.')
    home = Path.home().resolve(strict=True)
    library = home / 'Library'
    if library.is_symlink() or not library.is_dir() or library.stat().st_uid != os.getuid():
        raise ServiceError('The current user Library folder is unavailable.')
    agents = library / 'LaunchAgents'
    private_directory(agents, tighten=False)
    destination = agents / (LABEL + '.plist')
    if destination.is_symlink():
        raise ServiceError('The login service file cannot be a symbolic link.')
    existing = None
    if destination.exists():
        info = destination.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ServiceError('The existing login service file belongs to another user.')
        try:
            existing = plistlib.loads(destination.read_bytes())
        except (ValueError, plistlib.InvalidFileException) as error:
            raise ServiceError('Review the existing login service file before replacing it.') from error
        if existing not in (configuration, old_source_configuration(configuration, source_root)):
            raise ServiceError('A different service configuration already uses this file. Review it before replacing it.')
    domain = 'gui/' + str(os.getuid())
    loaded = subprocess.run(['/bin/launchctl', 'print', domain + '/' + LABEL],
                            capture_output=True, timeout=10, check=False)
    if loaded.returncode == 0:
        if not destination.exists():
            raise ServiceError('This label is already loaded without the expected service file. Review it before installing.')
        if existing == configuration:
            print('The emulator login service is already installed for this Mac user.')
            return
        # Migration removes only our known Documents-based job, never another
        # service. --install explicitly authorizes this service replacement.
        removed = subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + LABEL],
                                 capture_output=True, timeout=15, check=False)
        if removed.returncode:
            raise ServiceError('macOS could not replace the previous emulator login service.')
    private_file(destination, path.read_bytes())
    result = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(destination)],
                            capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ServiceError('The login service file is saved, but macOS could not start it (exit ' + str(result.returncode) + ').')
    print('The emulator host now starts automatically when this Mac user signs in.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare this user\'s emulator login service for review, or explicitly install it.')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--write-only', action='store_true', help='Save a reviewable service file without installing it (default).')
    action.add_argument('--install', action='store_true', help='Install only this user\'s emulator service and start it with macOS.')
    args = parser.parse_args(argv)
    try:
        if args.install:
            if sys.platform != 'darwin' or os.geteuid() != os.getuid() or os.getuid() == 0:
                raise ServiceError('Install this login service as the current Mac user, without sudo.')
            host_root = prepare_background_bundle()
            path, configuration = prepare(host_root)
        else:
            path, configuration = prepare_preview()
        print('Prepared login service: ' + str(path))
        if args.install:
            install(path, configuration)
        else:
            print('Nothing was installed. Use --install to enable automatic startup for this Mac user.')
    except (OSError, ValueError, ServiceError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, ServiceError) else 'The emulator login service could not be prepared or installed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
