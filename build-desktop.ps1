param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($InstallDependencies) {
    python -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed.' }
}
python -m PyInstaller --noconfirm AlphaLens.spec
if ($LASTEXITCODE -ne 0) { throw 'Desktop executable build failed.' }
Write-Host 'Desktop executable: dist\AlphaLens.exe'
