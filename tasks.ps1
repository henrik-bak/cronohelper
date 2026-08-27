<#
Windows equivalent of the Makefile. The Makefile's recipes are POSIX (rm -rf,
find) and assume `make`, which Windows does not ship; use this script instead.
Docker is identical on both, so `up`/`down`/`logs` just shell out to compose.

Usage:
    .\tasks.ps1 dev
    .\tasks.ps1 test
    .\tasks.ps1 spike "Fitt májgaluskaleves"
    .\tasks.ps1 up
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('dev', 'test', 'spike', 'up', 'down', 'logs', 'clean', 'help')]
    [string]$Task = 'help',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $env:DATA_DIR) { $env:DATA_DIR = '.\data' }
if (-not $env:PORT) { $env:PORT = '8080' }
# Hungarian text end to end, including this console.
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

switch ($Task) {
    'dev' {
        Write-Host "http://127.0.0.1:$env:PORT" -ForegroundColor Cyan
        uv run uvicorn app.main:app --reload --port $env:PORT
    }
    'test' {
        # Never touches the real Cronometer API.
        uv run pytest -q
    }
    'spike' {
        $food = ($Rest -join ' ').Trim()
        if (-not $food) {
            Write-Host 'Pass the exact name of a custom food you created by hand:' -ForegroundColor Yellow
            Write-Host '    .\tasks.ps1 spike "Fitt májgaluskaleves"'
            exit 2
        }
        uv run python spike_food_search.py $food
    }
    'up' { docker compose up -d --build }
    'down' { docker compose down }
    'logs' { docker compose logs -f }
    'clean' {
        foreach ($p in '.venv', '.pytest_cache', 'data') {
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }
        Get-ChildItem -Recurse -Directory -Filter __pycache__ |
            Remove-Item -Recurse -Force
    }
    default {
        Write-Host 'Tasks: dev, test, spike <food name>, up, down, logs, clean'
    }
}
