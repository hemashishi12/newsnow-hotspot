param(
    [switch]$Update
)

$ErrorActionPreference = "Stop"
$engineCommit = "1f9f19c2021a68d04df228f33e9099a0c947f6f8"
$engineRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\MoneyPrinterTurbo"))
$githubRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

if (-not $engineRoot.StartsWith($githubRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Video engine must be installed next to this project."
}

if (-not (Test-Path -LiteralPath $engineRoot)) {
    git clone --filter=blob:none https://github.com/harry0703/MoneyPrinterTurbo.git $engineRoot
} elseif (-not (Test-Path -LiteralPath (Join-Path $engineRoot ".git"))) {
    throw "Target exists but is not a MoneyPrinterTurbo Git repository: $engineRoot"
}

$remote = git -C $engineRoot remote get-url origin
if ($remote -notmatch "harry0703/MoneyPrinterTurbo") {
    throw "Target origin is not the official MoneyPrinterTurbo repository."
}

if ($Update) {
    git -C $engineRoot fetch --filter=blob:none origin main
    $engineCommit = (git -C $engineRoot rev-parse origin/main).Trim()
} else {
    git -C $engineRoot fetch --filter=blob:none --depth 1 origin $engineCommit
}
git -C $engineRoot checkout --detach $engineCommit

$pythonPath = Join-Path $engineRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    py -3.11 -m venv (Join-Path $engineRoot ".venv")
}

& $pythonPath -m pip install --upgrade pip
& $pythonPath -m pip install --editable $engineRoot

$configPath = Join-Path $engineRoot "config.toml"
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $engineRoot "config.example.toml") -Destination $configPath
}

Write-Host "MoneyPrinterTurbo installed at: $engineRoot"
Write-Host "Configure a stock provider API key in NewsNow settings. The engine starts on first use."
