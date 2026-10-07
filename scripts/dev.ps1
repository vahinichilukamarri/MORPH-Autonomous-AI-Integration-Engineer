<#
.SYNOPSIS
  MORPH developer commands.
.EXAMPLE
  ./scripts/dev.ps1 up
  ./scripts/dev.ps1 test
  ./scripts/dev.ps1 test-slow
  ./scripts/dev.ps1 ingest crm mock_systems/openapi/crm.v1.json
  ./scripts/dev.ps1 ingest crm http://localhost:8101/openapi.json
  ./scripts/dev.ps1 mapping-eval --n-runs 1
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('up', 'down', 'test', 'test-slow', 'lint', 'reset-db', 'ingest', 'mapping-eval')]
    [string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $Root '.run'
$PythonProjects = 'backend', 'mock_systems', 'bench'

function Invoke-Native {
    # Runs a native command and fails the script on a non-zero exit code.
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "'$File $($Arguments -join ' ')' exited with code $LASTEXITCODE" }
}

function Invoke-Uv {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { Invoke-Native 'uv' $args } else { Invoke-Native 'python' (@('-m', 'uv') + $args) }
}

function Set-EmbeddingCache {
    # Keep the downloaded embedding model in a git-ignored folder, not the temp directory.
    if (-not $env:EMBEDDING_CACHE_DIR) {
        $env:EMBEDDING_CACHE_DIR = Join-Path $Root '.cache/fastembed'
    }
}

function Import-DotEnv {
    $file = Join-Path $Root '.env'
    if (-not (Test-Path $file)) { $file = Join-Path $Root '.env.example' }
    foreach ($line in Get-Content $file) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }
}

function Wait-Http {
    param([string]$Url, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod -Uri $Url -TimeoutSec 3 | Out-Null
            return
        } catch { Start-Sleep -Milliseconds 500 }
    }
    throw "Timed out waiting for $Url"
}

function Start-Background {
    param([string]$Name, [string]$WorkDir, [string]$File, [string[]]$Arguments)
    New-Item -ItemType Directory -Force $RunDir | Out-Null
    $p = Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $WorkDir `
        -RedirectStandardOutput (Join-Path $RunDir "$Name.out.log") `
        -RedirectStandardError (Join-Path $RunDir "$Name.err.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $p.Id
}

function Stop-Background {
    foreach ($pidFile in Get-ChildItem -Path $RunDir -Filter '*.pid' -ErrorAction SilentlyContinue) {
        $procId = [int](Get-Content $pidFile.FullName)
        # A stale pid file (the process already exited) is not an error.
        if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
            & taskkill /PID $procId /T /F 2>&1 | Out-Null
        }
        Remove-Item $pidFile.FullName
    }
}

switch ($Command) {
    'up' {
        Import-DotEnv
        Set-EmbeddingCache
        Stop-Background
        Invoke-Native 'docker' @('compose', '--project-directory', $Root, 'up', '-d', '--build', '--wait')
        Push-Location (Join-Path $Root 'backend')
        try {
            Invoke-Uv sync --frozen --group embeddings
            Invoke-Uv run alembic upgrade head
        } finally { Pop-Location }
        Start-Background 'backend' (Join-Path $Root 'backend') `
            (Join-Path $Root 'backend/.venv/Scripts/python.exe') @('-m', 'uvicorn', 'app.main:app', '--port', '8000')
        Wait-Http 'http://localhost:8000/health'
        Push-Location (Join-Path $Root 'frontend')
        try { Invoke-Native 'npm' @('ci') } finally { Pop-Location }
        Start-Background 'frontend' (Join-Path $Root 'frontend') 'cmd.exe' @(
            '/c', 'npm run dev -- --host 127.0.0.1 --strictPort')
        Wait-Http 'http://127.0.0.1:5173/api/health'
        Write-Host 'MORPH is up: frontend http://localhost:5173, backend http://localhost:8000/health'
    }
    'down' {
        Stop-Background
        Invoke-Native 'docker' @('compose', '--project-directory', $Root, 'down')
    }
    'test' {
        foreach ($proj in $PythonProjects) {
            if (-not (Test-Path (Join-Path $Root "$proj/pyproject.toml"))) { continue }
            Push-Location (Join-Path $Root $proj)
            try { Invoke-Uv run pytest -q } finally { Pop-Location }
        }
    }
    'test-slow' {
        # Tests that need the real embedding model (downloaded on first run).
        Set-EmbeddingCache
        Push-Location (Join-Path $Root 'backend')
        try {
            Invoke-Uv sync --frozen --group embeddings
            Invoke-Uv run pytest -m slow -q
        } finally { Pop-Location }
    }
    'ingest' {
        if ($Rest.Count -ne 2) { throw 'usage: dev.ps1 ingest <name> <spec path or url>' }
        $name, $source = $Rest
        if ($source -match '^https?://') {
            $spec = @{ url = $source }
        } else {
            $spec = @{ file = (Resolve-Path $source).Path }
        }
        $body = @{ name = $name; source = $spec } | ConvertTo-Json -Depth 4
        try {
            # The first call downloads the embedding model, so allow plenty of time.
            Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/systems/ingest' `
                -ContentType 'application/json' -Body $body -TimeoutSec 900 | ConvertTo-Json
        } catch {
            if ($_.ErrorDetails.Message) { Write-Error $_.ErrorDetails.Message } else { throw }
        }
    }
    'mapping-eval' {
        # The real mapping evaluation (needs GROQ_API_KEY in .env); arguments go to the script.
        Set-EmbeddingCache
        Push-Location (Join-Path $Root 'bench')
        try {
            Invoke-Uv run --group embeddings python -m scripts.run_mapping_eval @Rest
        } finally { Pop-Location }
    }
    'lint' {
        foreach ($proj in $PythonProjects) {
            if (-not (Test-Path (Join-Path $Root "$proj/pyproject.toml"))) { continue }
            Push-Location (Join-Path $Root $proj)
            try {
                Invoke-Uv run ruff check .
                Invoke-Uv run ruff format --check .
                Invoke-Uv run mypy
            } finally { Pop-Location }
        }
        Push-Location (Join-Path $Root 'frontend')
        try {
            Invoke-Native 'npm' @('run', 'typecheck')
            Invoke-Native 'npm' @('run', 'lint')
        } finally { Pop-Location }
    }
    'reset-db' {
        Import-DotEnv
        Invoke-Native 'docker' @('compose', '--project-directory', $Root, 'rm', '-sfv', 'postgres')
        Invoke-Native 'docker' @('volume', 'rm', '-f', 'morph_pgdata')
        Invoke-Native 'docker' @('compose', '--project-directory', $Root, 'up', '-d', '--wait', 'postgres')
        Push-Location (Join-Path $Root 'backend')
        try { Invoke-Uv run alembic upgrade head } finally { Pop-Location }
        Write-Host 'Database reset and migrated.'
    }
}
