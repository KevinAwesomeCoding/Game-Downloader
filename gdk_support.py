"""Helpers for identifying GDK game installations without changing them."""

from pathlib import Path
import os
import subprocess
import json
import base64
import tempfile
import xml.etree.ElementTree as ET


def find_gdk_helper(game_dir, executable=None):
    """Find a helper beside the selected executable or at the game root.

    Avoid a recursive search: unrelated bundled tools or DLC may contain a
    helper for a different package.
    """
    root = Path(game_dir).resolve()
    candidates = []
    if executable:
        exe_dir = Path(executable).resolve().parent
        try:
            exe_dir.relative_to(root)
        except ValueError:
            pass
        else:
            candidates.append(exe_dir)
    candidates.append(root)
    for directory in dict.fromkeys(candidates):
        try:
            for item in directory.iterdir():
                if item.name.casefold() == 'gdk_helper.bat' and item.is_file():
                    resolved = item.resolve()
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        continue
                    return resolved
        except OSError:
            continue
    return None


def gdk_package_identity(game_dir):
    """Read package identity from the game's manifest, if it is well formed."""
    manifest = Path(game_dir) / 'appxmanifest.xml'
    try:
        root = ET.parse(manifest).getroot()
        for item in root:
            if item.tag.rsplit('}', 1)[-1] == 'Identity':
                name = item.get('Name', '').strip()
                publisher = item.get('Publisher', '').strip()
                return (name, publisher) if name and publisher else None
    except (OSError, ET.ParseError):
        pass
    return None


def run_gdk_helper(helper):
    """Run a recognized helper's registration choices with temporary elevation."""
    helper = Path(helper).resolve()
    contents = helper.read_text(encoding='utf-8', errors='replace').casefold()
    expected = ('set /p action=', ':developeron', ':developeroff', ':gameinstall',
                'wdapp', 'allowdevelopmentwithoutdevlicense', ':exit')
    if not all(part in contents for part in expected):
        raise RuntimeError('This GDK helper has an unrecognized menu; automatic setup is not supported for it.')
    with tempfile.TemporaryDirectory(prefix='gameinstaller-gdk-') as tmp:
        tmp_path = Path(tmp)
        answers = tmp_path / 'answers.txt'
        status = tmp_path / 'status.txt'
        trace = tmp_path / 'diagnostics.txt'
        answers.write_text('3\n1\n4\n5\n', encoding='ascii')
        def ps_value(value):
            return "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + base64.b64encode(str(value).encode('utf-8')).decode('ascii') + "'))"
        script = r"""
$ErrorActionPreference = 'Stop'
$helper = HELPER_VALUE
$answers = ANSWERS_VALUE
$status = STATUS_VALUE
$trace = TRACE_VALUE
Start-Transcript -Path $trace -Force | Out-Null
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock'
$created = -not (Test-Path -LiteralPath $key)
if ($created) { New-Item -Path $key -Force | Out-Null }
$existing = Get-ItemProperty -LiteralPath $key -Name AllowDevelopmentWithoutDevLicense -ErrorAction SilentlyContinue
try {
  $dx = Get-AppxPackage -Name Microsoft.DirectXRuntime | Where-Object { $_.Architecture -eq 'X64' }
  if (-not $dx) {
    $staged = Get-AppxPackage -AllUsers -Name Microsoft.DirectXRuntime | Where-Object { $_.Architecture -eq 'X64' } | Select-Object -First 1
    if ($staged -and $staged.InstallLocation) {
      Add-AppxPackage -Register (Join-Path $staged.InstallLocation 'AppxManifest.xml') -DisableDevelopmentMode
    }
  }
  $q = [string][char]34
  $args = '/d /s /c ' + $q + $q + $helper + $q + $q
  $p = Start-Process -FilePath $env:ComSpec -ArgumentList $args -WorkingDirectory (Split-Path -LiteralPath $helper) -RedirectStandardInput $answers -NoNewWindow -PassThru -Wait
  if ($p.ExitCode -ne 0) { throw "GDK helper exited with code $($p.ExitCode)." }
  Set-Content -LiteralPath $status -Value 'OK' -Encoding UTF8
} catch {
  Set-Content -LiteralPath $status -Value ('ERROR: ' + $_.Exception.Message) -Encoding UTF8
} finally {
  if ($null -ne $existing) {
    Set-ItemProperty -LiteralPath $key -Name AllowDevelopmentWithoutDevLicense -Value $existing.AllowDevelopmentWithoutDevLicense
  } else {
    Remove-ItemProperty -LiteralPath $key -Name AllowDevelopmentWithoutDevLicense -ErrorAction SilentlyContinue
  }
  Stop-Transcript | Out-Null
}
""".replace('HELPER_VALUE', ps_value(helper)).replace('ANSWERS_VALUE', ps_value(answers)).replace('STATUS_VALUE', ps_value(status)).replace('TRACE_VALUE', ps_value(trace))
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        launch = "$ErrorActionPreference='Stop'; $p=Start-Process -FilePath powershell.exe -Verb RunAs -ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand', $env:GAME_INSTALLER_GDK_SCRIPT) -Wait -PassThru; exit $p.ExitCode"
        env = os.environ.copy()
        env['GAME_INSTALLER_GDK_SCRIPT'] = encoded
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', launch],
                                env=env, capture_output=True, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if status.exists():
            message = status.read_text(encoding='utf-8-sig').strip()
            if message != 'OK':
                raise RuntimeError(message or 'GDK setup failed.')
        if result.returncode:
            detail = trace.read_text(encoding='utf-8-sig', errors='replace') if trace.exists() else ''
            raise RuntimeError((result.stderr or result.stdout or detail or f'Administrator setup exited with code {result.returncode}.').strip())
        if not status.exists():
            raise RuntimeError('The administrator setup did not report a result.')


def registered_gdk_app(game_dir):
    """Return the registered app ID for this exact directory and manifest."""
    identity = gdk_package_identity(game_dir)
    if not identity:
        raise RuntimeError('The GDK package manifest is missing or invalid.')
    env = os.environ.copy()
    env.update(GAME_INSTALLER_GDK_NAME=identity[0],
               GAME_INSTALLER_GDK_PUBLISHER=identity[1],
               GAME_INSTALLER_GDK_DIRECTORY=str(Path(game_dir).resolve()))
    script = r"""
$ErrorActionPreference='Stop'
$p = @(Get-AppxPackage -Name $env:GAME_INSTALLER_GDK_NAME | Where-Object {
    $_.Name -eq $env:GAME_INSTALLER_GDK_NAME -and
    $_.Publisher -eq $env:GAME_INSTALLER_GDK_PUBLISHER -and
    $_.InstallLocation.TrimEnd('\') -ieq $env:GAME_INSTALLER_GDK_DIRECTORY.TrimEnd('\') -and
    $_.Status -eq 'Ok'
})
if ($p.Count -ne 1) { exit 3 }
$m = Get-AppxPackageManifest -Package $p[0].PackageFullName
$apps = @($m.Package.Applications.Application)
if ($apps.Count -ne 1 -or -not $apps[0].Id) { exit 4 }
@{ appId = ($p[0].PackageFamilyName + '!' + $apps[0].Id) } | ConvertTo-Json -Compress
"""
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            env=env, capture_output=True, text=True, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('The game is not registered for this Windows account, or its registered app could not be identified. Finish setup in GDK Helper and try again.')
    try:
        app_id = json.loads(result.stdout)['appId']
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Windows returned an invalid registered app identifier.') from exc
    if not isinstance(app_id, str) or not app_id or any(c in app_id for c in '\r\n\\/'):
        raise RuntimeError('Windows returned an invalid registered app identifier.')
    return app_id
