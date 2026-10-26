[CmdletBinding()]
param(
    [switch]$EnableDailySchedule,
    [string]$PlanStart = "", # Compatibility only; each plan now uses Added Time.
    [string]$ResourceGroup = "TherapyScheduler",
    [string]$AppName = "orscheduler",
    [string]$JobName = "orscheduler-dailyjob"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $result = & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Azure operation failed; test job configuration stopped." }
    return $result
}
function Write-AzureJobConfiguration {
    param([string]$Path, [hashtable]$Document)
    # Windows PowerShell 5 adds a BOM with Set-Content -Encoding utf8.
    # Azure CLI reads --yaml using the Windows default encoding. ASCII JSON
    # (also valid YAML) avoids both the BOM and locale-dependent decoding.
    $json = $Document | ConvertTo-Json -Depth 15
    $json = [regex]::Replace($json, '[^\x00-\x7F]', {
        param($match)
        '\u{0:x4}' -f [int][char]$match.Value
    })
    [System.IO.File]::WriteAllText($Path, $json, [System.Text.Encoding]::ASCII)
}
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $projectRoot
$temporary = $null
try {
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $settingsJson = & $python -c "import json,os; from config import load_local_env_file; from pathlib import Path; load_local_env_file(Path('flow_test/.env')); from services.zoho_creator_service import settings_from_environment; s=settings_from_environment(); print(json.dumps({k:os.getenv('FLOW_TEST_ZOHO_'+k.upper()) or getattr(s,k) for k in ('client_id','client_secret','refresh_token')}))"
    if ($LASTEXITCODE -ne 0) { throw "Could not read existing local OAuth settings." }
    $settings = $settingsJson | ConvertFrom-Json
    if (-not $settings.client_id -or -not $settings.client_secret -or -not $settings.refresh_token) {
        throw "The existing OAuth connection must support automatic refresh."
    }
    # Validate the real read-only test report contract before provisioning a timer.
    & $python -m flow_test.zoho_daily
    if ($LASTEXITCODE -ne 0) { throw "Test report validation failed; no job was changed." }
    $app = (Invoke-Azure containerapp show --name $AppName --resource-group $ResourceGroup --output json --only-show-errors) | ConvertFrom-Json
    $baseUrl = "https://$($app.properties.configuration.ingress.fqdn)"
    $adminKey = (Get-Content -LiteralPath "flow_test/data/admin.token" -Raw).Trim()
    $headers = @{ Authorization = "Bearer $adminKey" }
    $status = Invoke-RestMethod -Uri "$baseUrl/test/api/zoho/status" -Headers $headers -TimeoutSec 30
    if ($status.source -ne "nidhin.kumar_turnb/test-gokul") { throw "Deploy the test import endpoints first." }
    if (-not $status.PSObject.Properties["checkpoint"]) { throw "Deploy the incremental test endpoints first." }
    if (-not $status.PSObject.Properties["sourceTimezones"] -or
        $status.sourceTimezones.Added_Time -ne "Asia/Kolkata" -or $status.sourceTimezones.Modified_Time -ne "Asia/Kolkata") {
        throw "Deploy the Added Time IST / Modified Time IST update first."
    }
    if ($status.reconciliationRequired) { throw "Run one manual --apply --reconcile import before configuring the job." }
    if (-not $status.PSObject.Properties["planStartSource"] -or $status.planStartSource -ne "Added_Time") {
        throw "Deploy the Added Time treatment-start update first."
    }
    if ($null -eq $status.sync) { throw "Complete one manual live TEST import before creating the daily job." }
    $registry = @($app.properties.configuration.registries)[0]
    if (-not $registry.username -or -not $registry.passwordSecretRef) {
        throw "This script reuses the app's existing registry credentials. Configure registry access for the job if the app uses a managed identity."
    }
    $secretRows = (Invoke-Azure containerapp secret list --name $AppName --resource-group $ResourceGroup --show-values --output json --only-show-errors) | ConvertFrom-Json
    $registrySecret = @($secretRows | Where-Object { $_.name -eq $registry.passwordSecretRef })[0].value
    $environmentId = $null
    foreach ($field in @("environmentId", "managedEnvironmentId")) {
        if ($app.properties.PSObject.Properties[$field] -and $app.properties.$field) {
            $environmentId = $app.properties.$field
            break
        }
    }
    if (-not $environmentId) { throw "Could not determine the existing Container Apps environment." }
    $configuration = @{
        triggerType = "Manual"; replicaTimeout = 600; replicaRetryLimit = 1
        manualTriggerConfig = @{ parallelism = 1; replicaCompletionCount = 1 }
        registries = @(@{ server = $registry.server; username = $registry.username; passwordSecretRef = "registry-password" })
        secrets = @(
            @{ name = "registry-password"; value = $registrySecret },
            @{ name = "zoho-client-id"; value = $settings.client_id },
            @{ name = "zoho-client-secret"; value = $settings.client_secret },
            @{ name = "zoho-refresh-token"; value = $settings.refresh_token },
            @{ name = "test-admin-key"; value = $adminKey }
        )
    }
    if ($EnableDailySchedule) {
        $configuration.Remove("manualTriggerConfig")
        $configuration.triggerType = "Schedule"
        # Azure evaluates cron in UTC: 21:30 UTC is 03:00 Asia/Kolkata next day.
        $configuration.scheduleTriggerConfig = @{ cronExpression = "30 21 * * *"; parallelism = 1; replicaCompletionCount = 1 }
    }
    $document = @{
        location = $app.location
        properties = @{
            environmentId = $environmentId
            configuration = $configuration
            template = @{ containers = @(@{
                name = "zoho-test-sync"; image = $app.properties.template.containers[0].image
                command = @("python", "-m", "flow_test.zoho_daily", "--apply")
                resources = @{ cpu = 0.25; memory = "0.5Gi" }
                env = @(
                    @{ name = "FLOW_TEST_APP_URL"; value = $baseUrl },
                    @{ name = "FLOW_TEST_ADMIN_TOKEN"; secretRef = "test-admin-key" },
                    @{ name = "FLOW_TEST_ZOHO_CLIENT_ID"; secretRef = "zoho-client-id" },
                    @{ name = "FLOW_TEST_ZOHO_CLIENT_SECRET"; secretRef = "zoho-client-secret" },
                    @{ name = "FLOW_TEST_ZOHO_REFRESH_TOKEN"; secretRef = "zoho-refresh-token" }
                )
            }) }
        }
    }
    $temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("zoho-test-job-" + [guid]::NewGuid().ToString("N") + ".json")
    Write-AzureJobConfiguration -Path $temporary -Document $document
    $existing = (Invoke-Azure containerapp job list --resource-group $ResourceGroup --query "[?name=='$JobName'].name" --output tsv --only-show-errors)
    if ($existing) {
        Invoke-Azure containerapp job update --name $JobName --resource-group $ResourceGroup --yaml $temporary --output none --only-show-errors
    } else {
        Invoke-Azure containerapp job create --name $JobName --resource-group $ResourceGroup --yaml $temporary --output none --only-show-errors
    }
    Write-Host "Test-only Zoho sync job configured: $JobName"
    if ($EnableDailySchedule) { Write-Host "Daily trigger: 03:00 IST. The job prepares tomorrow's draft; Sunday target dates are skipped." }
    else { Write-Host "Manual trigger only. Start with: az containerapp job start --name $JobName --resource-group $ResourceGroup" }
} finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force }
    $settings = $null
    $settingsJson = $null
    $secretRows = $null
    $registrySecret = $null
    $adminKey = $null
    $document = $null
    Pop-Location
}
