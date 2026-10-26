[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $projectRoot
try {
    Write-Host "Deploying the current test app, then clearing old live /test history."
    Write-Host "Only the isolated flow_test schema is reset. Production data and phone chat history remain."
    & (Join-Path $PSScriptRoot "deploy_azure.ps1")
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    & $python -m flow_test.reset_live --confirm RESET-flow-test
    if ($LASTEXITCODE -ne 0) {
        throw "Live reset was not verified. Read the output and inspect /test before rerunning."
    }
} finally {
    Pop-Location
}
