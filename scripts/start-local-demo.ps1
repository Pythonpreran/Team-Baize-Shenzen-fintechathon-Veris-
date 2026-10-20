[CmdletBinding()]
param(
    [string]$PortfolioDbPath,
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$Install,
    [switch]$SeedCorpus
)

# Starts only the offline local demo. It does not read, create, or print a .env file.
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

Set-Location $RepoRoot
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $VenvRoot = Join-Path $RepoRoot ".venv"
    if (Test-Path -LiteralPath $VenvRoot) {
        throw "The project-local .venv exists but is unusable. Repair or recreate only '$VenvRoot', then rerun this script; do not use a global installation."
    }
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3.12 -m venv $VenvRoot
    } elseif ($SystemPython) {
        & $SystemPython.Source -m venv $VenvRoot
    } else {
        throw "Python 3.11 or newer is required to create the project-local .venv."
    }
}

$PythonVersion = (& $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ([version]$PythonVersion -lt [version]"3.11") {
    throw "The project-local interpreter is Python $PythonVersion; VERIS requires Python 3.11 or newer."
}

if ($Install) {
    & $VenvPython -m pip install --no-input -r requirements.txt -e .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $VenvPython -c "import proven; print(proven.__file__)"
if ($LASTEXITCODE -ne 0) {
    throw "The local environment cannot import this checkout. Re-run with -Install to repair the editable installation."
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
    throw "VERIS_PORTFOLIO_DB_PATH must be outside the repository so local records cannot enter source control."
}
$DatabaseDirectory = Split-Path -Parent $PortfolioDbPath
if (-not (Test-Path -LiteralPath $DatabaseDirectory)) {
    New-Item -ItemType Directory -Path $DatabaseDirectory -Force | Out-Null
}

$env:VERIS_MODE = "local"
$env:VERIS_PORTFOLIO_DB_PATH = $PortfolioDbPath
$env:VERIS_LLM_PROFILE = "none"
$env:PROVEN_LIVE_SOURCES = "0"

if ($SeedCorpus) {
    $SeedDirectory = Join-Path (Split-Path -Parent $RepoRoot) "06-evidence"
    if (-not (Test-Path -LiteralPath $SeedDirectory -PathType Container)) {
        throw "Local corpus seed directory is absent: $SeedDirectory"
    }
    & $VenvPython scripts/fetch_corpus.py --seed-from $SeedDirectory
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Corpus seeding reported unavailable or mismatched material. The application will report absent wording rather than infer it; review the status below."
    }
    & $VenvPython scripts/fetch_corpus.py --status
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "VERIS local demo uses portfolio storage outside Git: $PortfolioDbPath"
Write-Host "Opening only http://127.0.0.1:$Port (no network bind, no reload watcher)."
& $VenvPython -m uvicorn proven.api:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
