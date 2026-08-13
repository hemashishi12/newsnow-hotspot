$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$project = $PSScriptRoot
$venvPython = Join-Path $project ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    py -3.12 -m venv (Join-Path $project ".venv")
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $project "requirements.txt")
if (-not (Test-Path -LiteralPath (Join-Path $project ".env"))) {
    Copy-Item -LiteralPath (Join-Path $project ".env.example") -Destination (Join-Path $project ".env")
}
Write-Host "安装完成。请编辑 .env 填写 AI_API_KEY，然后运行 .\run.ps1 serve"
