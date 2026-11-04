$ErrorActionPreference = 'Stop'

# GitLab's Windows checkout path is too deep for Chromium's resource files.
# Build through a short drive alias while artifacts still land in the checkout.
if (Test-Path 'R:\') { throw 'Build drive R: is already in use.' }
subst R: $env:CI_PROJECT_DIR
if ($LASTEXITCODE -ne 0) { throw 'Could not create the short build drive.' }
Set-Location 'R:\'

$python = (Get-Command py -ErrorAction SilentlyContinue) ? 'py' : 'python'
if ($python -eq 'py') {
    & py -3.11 --version *> $null
    if ($LASTEXITCODE -ne 0) { choco install python311 --yes --no-progress; $python = 'C:\Python311\python.exe' }
    else { $pythonArgs = @('-3.11') }
}
if (-not $pythonArgs) { $pythonArgs = @() }
& $python @pythonArgs --version
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required.' }
& $python @pythonArgs -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& $python @pythonArgs -m pip install -r requirements.txt 'pyinstaller>=6.10.0'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python @pythonArgs -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Windows tests failed.' }
& $python @pythonArgs tools/build_host_helper.py
if ($LASTEXITCODE -ne 0) { throw 'Download browser helper build failed.' }

New-Item -ItemType Directory -Force bin, build/dll-collision-fixture, dist | Out-Null
Copy-Item 'C:\Program Files\7-Zip\7z.exe' bin/
Copy-Item 'C:\Program Files\7-Zip\7z.dll' bin/
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products '*' -property installationPath
if (-not $vs) { throw 'Visual Studio C++ tools were not found.' }
$vcvars = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$source = (Resolve-Path tools/windows_dll_safety_fixture.c).Path
$fixture = Join-Path (Resolve-Path build/dll-collision-fixture).Path 'winmm.dll'
$command = 'call "{0}" -arch=amd64 >nul && cl /nologo /LD "{1}" /link /OUT:"{2}"' -f $vcvars, $source, $fixture
cmd /d /c $command
if ($LASTEXITCODE -ne 0) { throw 'Could not compile the controlled DLL fixture.' }

$main = Get-Content main.py -Raw
$main = $main -replace '(?m)^VERSION = ".*"$', ('VERSION = "' + $env:RELEASE_TAG + '"')
Set-Content main.py -Value $main -Encoding utf8 -NoNewline

$common = @('--windowed', '--uac-admin', '--noupx', '--version-file', 'version_info.txt',
    '--collect-all', 'PyQt6', '--hidden-import', 'PyQt6.sip',
    '--add-data', 'build/host-resolver/HostResolver;HostResolver',
    '--add-data', 'games.json;.', '--add-data', 'bin/7z.exe;bin', '--add-data', 'bin/7z.dll;bin',
    '--runtime-hook', 'tools/windows_dll_safety_hook.py', '--name', 'GameInstaller')

& $python @pythonArgs -m PyInstaller --onedir --contents-directory . @common main.py
if ($LASTEXITCODE -ne 0) { throw 'Folder build failed.' }
& $python @pythonArgs tools/check_windows_dll_safety.py $fixture dist/GameInstaller/GameInstaller.exe
if ($LASTEXITCODE -ne 0) { throw 'Folder DLL collision check failed.' }
Compress-Archive -Path dist/GameInstaller -DestinationPath dist/GameInstaller.zip

& $python @pythonArgs -m PyInstaller --onefile @common main.py
if ($LASTEXITCODE -ne 0) { throw 'Single-file build failed.' }
Move-Item dist/GameInstaller.exe dist/GameInstaller-windows-x64.exe
& $python @pythonArgs tools/check_windows_dll_safety.py $fixture dist/GameInstaller-windows-x64.exe
if ($LASTEXITCODE -ne 0) { throw 'Single-file DLL collision check failed.' }

$folder = Start-Process -FilePath dist/GameInstaller/GameInstaller.exe -ArgumentList '--check-download-browser' -Wait -PassThru
if ($folder.ExitCode -ne 0) { throw 'Folder build browser check failed.' }
$single = Start-Process -FilePath dist/GameInstaller-windows-x64.exe -ArgumentList '--check-download-browser' -Wait -PassThru
if ($single.ExitCode -ne 0) { throw 'Single-file build browser check failed.' }
