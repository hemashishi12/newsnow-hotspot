param(
    [ValidateSet("serve", "collect", "analyze", "init")]
    [string]$Command = "serve",
    [switch]$NoAI
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "虚拟环境不存在，请先运行 .\setup.ps1"
}

Push-Location $PSScriptRoot
try {
    $arguments = @("main.py", $Command)
    if ($NoAI) { $arguments += "--no-ai" }
    & $python @arguments
}
finally {
    Pop-Location
}
