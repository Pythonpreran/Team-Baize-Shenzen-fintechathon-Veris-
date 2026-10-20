[CmdletBinding()]
param(
    [string]$PortfolioDbPath,
    [string]$OutputRoot
)

# Fail fast while retaining one log per real command outside the checkout.
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

function Test-PathInside {
    param([string]$Candidate, [string]$Root)
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate).TrimEnd([char[]]@('\', '/'))
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    return $candidateFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidateFull.StartsWith($rootFull + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)
}

function Invoke-ReleaseStep {
    param([string]$Name, [scriptblock]$Command)
    $LogPath = Join-Path $RunDirectory ("{0}.log" -f $Name)
    Write-Host "`n=== $Name ==="
    & $Command 2>&1 | Tee-Object -FilePath $LogPath
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "Release gate stopped at '$Name' (exit $ExitCode). Actual output: $LogPath"
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Project-local .venv is unavailable. Run scripts/start-local-demo.ps1 -Install first; do not use global packages."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js is required for the existing JavaScript syntax checks."
}

Set-Location $RepoRoot
& $VenvPython -c "import proven"
if ($LASTEXITCODE -ne 0) {
    throw "The local environment cannot import this checkout. Run scripts/start-local-demo.ps1 -Install first."
}

if (-not $PortfolioDbPath) {
    $LocalAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($LocalAppData)) {
        throw "Provide -PortfolioDbPath because LocalApplicationData is unavailable."
    }
    $PortfolioDbPath = Join-Path $LocalAppData "veris-demo\portfolio.sqlite"
}
$PortfolioDbPath = [System.IO.Path]::GetFullPath($PortfolioDbPath)
if (Test-PathInside -Candidate $PortfolioDbPath -Root $RepoRoot) {
    throw "VERIS_PORTFOLIO_DB_PATH must be outside the repository."
}
$DatabaseDirectory = Split-Path -Parent $PortfolioDbPath
if (-not (Test-Path -LiteralPath $DatabaseDirectory)) {
    New-Item -ItemType Directory -Path $DatabaseDirectory -Force | Out-Null
}

$LocalAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
if ([string]::IsNullOrWhiteSpace($LocalAppData) -or -not (Test-Path -LiteralPath $LocalAppData)) {
    throw "A writable LocalApplicationData directory is required for ignored release-gate output."
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $LocalAppData "veris-demo\release-gate"
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
if (Test-PathInside -Candidate $OutputRoot -Root $RepoRoot) {
    throw "Release-gate output must be outside the repository so generated logs cannot be tracked."
}
$OutputParent = Split-Path -Parent $OutputRoot
if (-not (Test-Path -LiteralPath $OutputParent)) {
    New-Item -ItemType Directory -Path $OutputParent -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $OutputRoot)) {
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
}
$RunDirectory = Join-Path $OutputRoot (Get-Date -Format "yyyyMMdd-HHmmss")
New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null

$env:VERIS_MODE = "local"
$env:VERIS_PORTFOLIO_DB_PATH = $PortfolioDbPath
Remove-Item Env:VERIS_LLM_PROFILE -ErrorAction SilentlyContinue
$env:PROVEN_LIVE_SOURCES = "0"

Invoke-ReleaseStep "corpus-status" { & $VenvPython scripts/fetch_corpus.py --status }
Invoke-ReleaseStep "pytest" { & $VenvPython -m pytest -q -p no:warnings -ra }
Invoke-ReleaseStep "deployment-smoke" { & $VenvPython scripts/smoke_deployment.py }
Invoke-ReleaseStep "node-app" { node --check web/app.js }
Invoke-ReleaseStep "node-reviewer" { node --check web/reviewer.js }
Invoke-ReleaseStep "repository-scan" { & $VenvPython scripts/scan_repo.py }
Invoke-ReleaseStep "i18n" { & $VenvPython scripts/check_i18n.py }
Invoke-ReleaseStep "pip-audit" { & $VenvPython -m pip_audit --progress-spinner off }
Invoke-ReleaseStep "bandit" { & $VenvPython -m bandit -q -r src -ll }
Invoke-ReleaseStep "security-redteam" {
    & $VenvPython -m security.redteam --output-dir (Join-Path $RunDirectory "security-redteam")
}
Invoke-ReleaseStep "evaluation-benchmark" {
    & $VenvPython -m eval.bench --all --output-dir (Join-Path $RunDirectory "evaluation")
}
Invoke-ReleaseStep "compliance-matrix" { & $VenvPython scripts/generate_compliance_matrix.py --check }
Invoke-ReleaseStep "documentation" { & $VenvPython scripts/check_docs.py }
Invoke-ReleaseStep "evidence-verification" { & $VenvPython scripts/verify_evidence.py }

@{
    run_directory = $RunDirectory
    portfolio_database_outside_repository = $PortfolioDbPath
    mode = $env:VERIS_MODE
    live_sources = $env:PROVEN_LIVE_SOURCES
    llm_profile = "unset for pytest; eval.bench forces none"
    note = "Each command's actual output is in its .log file. pytest -ra preserves any corpus-dependent skips; a skip is not reported as a pass."
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RunDirectory "run-summary.json") -Encoding utf8

Write-Host "Release gate completed. Review actual logs at: $RunDirectory"
Write-Host "The gate does not turn unavailable corpus tests, external validation, or human review into passing evidence."
