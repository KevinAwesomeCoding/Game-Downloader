$ErrorActionPreference = "Stop"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Game Downloader Sync" .\sync_app.py
Write-Host "Built: $PSScriptRoot\dist\Game Downloader Sync.exe"
