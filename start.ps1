param([int]$Port = 8010, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $installedVersions = (& py --list-paths | Out-String)
        if ($installedVersions -match '3\.10') { & py -3.10 -m venv .venv }
        elseif ($installedVersions -match '3\.11') { & py -3.11 -m venv .venv }
        else { & py -3 -m venv .venv }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw 'Python was not found. Install Python 3.10 or 3.11, then run again.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
}
& $venvPython -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ is required"'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required. Python 3.10 or 3.11 is recommended.' }
$requirementsPath = Join-Path $PSScriptRoot 'requirements.txt'
$markerPath = Join-Path $PSScriptRoot '.venv\requirements.sha256'
$currentHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
$previousHash = if (Test-Path -LiteralPath $markerPath) { (Get-Content -LiteralPath $markerPath -Raw).Trim() } else { '' }
if ($previousHash -ne $currentHash) {
    Write-Host 'Installing dependencies. The first launch requires an internet connection...'
    & $venvPython -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and run again.' }
    Set-Content -LiteralPath $markerPath -Value $currentHash -Encoding ASCII
}
$launchArgs = @('-m', 'app.launch', '--port', "$Port")
if ($NoBrowser) { $launchArgs += '--no-browser' }
& $venvPython @launchArgs
if ($LASTEXITCODE -ne 0) { throw 'The server stopped with an error.' }
