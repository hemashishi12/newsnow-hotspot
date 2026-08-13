$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

$project = $PSScriptRoot
$python = Join-Path $project ".venv\Scripts\python.exe"
$logDirectory = Join-Path $project "data\logs"
$supervisorLog = Join-Path $logDirectory "background-supervisor.log"
$stdoutLog = Join-Path $logDirectory "background-server.stdout.log"
$stderrLog = Join-Path $logDirectory "background-server.stderr.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment is missing. Run .\setup.ps1 first."
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Add-Content -LiteralPath $supervisorLog -Encoding UTF8 -Value "$(Get-Date -Format o) Port 8765 is already in use; background launch skipped."
    exit 0
}

Add-Content -LiteralPath $supervisorLog -Encoding UTF8 -Value "$(Get-Date -Format o) Starting NewsNow Hotspot service."
$serverProcess = Start-Process `
    -FilePath $python `
    -ArgumentList "main.py", "serve" `
    -WorkingDirectory $project `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru `
    -Wait
if ($serverProcess.ExitCode -ne 0) {
    throw "NewsNow Hotspot service exited with code $($serverProcess.ExitCode)."
}
