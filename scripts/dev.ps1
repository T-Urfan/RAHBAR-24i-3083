<#
.SYNOPSIS
  Windows (PowerShell) equivalents of the Makefile targets, for native Windows development.
  The Makefile stays the reference for Linux and CI; both run the same commands.

.EXAMPLE
  .\scripts\dev.ps1 install
  .\scripts\dev.ps1 test
  .\scripts\dev.ps1 cov
  .\scripts\dev.ps1 lint
  .\scripts\dev.ps1 dev
  .\scripts\dev.ps1 check
  .\scripts\dev.ps1 cluster
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "install", "test", "cov", "lint", "dev", "check", "cluster")]
    [string]$Target = "help"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$MinikubeMemory = if ($env:MINIKUBE_MEMORY) { $env:MINIKUBE_MEMORY } else { "5120" }  # MB; 16 GB laptop
$MinikubeCpus = if ($env:MINIKUBE_CPUS) { $env:MINIKUBE_CPUS } else { "4" }

function Invoke-Step([string]$Dir, [scriptblock]$Block) {
    Push-Location (Join-Path $Root $Dir)
    try { & $Block; if ($LASTEXITCODE -ne 0) { throw "failed with exit code $LASTEXITCODE" } }
    finally { Pop-Location }
}

switch ($Target) {
    "help" {
        Get-Help $PSCommandPath -Examples | Out-String | Write-Output
    }
    "install" {   # project-local virtual environment only; nothing system-wide
        Invoke-Step "." { py -3.12 -m venv .venv }
        Invoke-Step "." { & $Py -m pip install -q --upgrade pip }
        Invoke-Step "." { & $Py -m pip install -q -r api/requirements-dev.txt -r indexer/requirements-dev.txt }
    }
    "test" {      # stubs and fakes only: no cluster, no model, no network
        Invoke-Step "api" { & $Py -m pytest -q }
        Invoke-Step "indexer" { & $Py -m pytest -q }
    }
    "cov" {       # the CI gate: at least 60% on the app package
        Invoke-Step "api" { & $Py -m pytest -q --cov=app --cov-report=term-missing --cov-fail-under=60 }
        Invoke-Step "indexer" { & $Py -m pytest -q --cov=pipeline --cov-report=term-missing }
    }
    "lint" {
        Invoke-Step "." { & $Py -m ruff check api indexer eval corpus }
        Invoke-Step "api" { & $Py -m mypy app }
        Invoke-Step "indexer" { & $Py -m mypy pipeline }
    }
    "dev" {       # the API with stubs on http://localhost:8000, serving the chat UI too
        $env:SERVE_UI_DIR = "../ui"; $env:RETRIEVER = "stub"; $env:LLM_PROVIDER = "stub"
        Invoke-Step "api" { & $Py -m uvicorn app.main:app --reload --port 8000 }
    }
    "check" {
        Invoke-Step "." { & $Py scripts/check_submission.py }
    }
    "cluster" {   # Calico, so NetworkPolicy is actually enforced (section 9)
        minikube start --driver=docker --cni=calico --memory=$MinikubeMemory --cpus=$MinikubeCpus
        minikube addons enable metrics-server
        minikube addons enable ingress
    }
}
