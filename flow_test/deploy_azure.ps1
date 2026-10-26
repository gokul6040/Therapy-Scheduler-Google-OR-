[CmdletBinding()]
param(
    [string]$ResourceGroup = "TherapyScheduler",
    [string]$AppName = "orscheduler",
    [string]$Registry = "orscheduler0488dadb",
    [ValidateSet("session", "template")][string]$MessageMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AzArguments)
    & az @AzArguments
    if ($LASTEXITCODE -ne 0) { throw "Azure command failed; deployment stopped." }
}

function Resolve-TestMessagingMode {
    param([string]$Requested, [string]$Existing)
    $resolved = if ($Requested) { $Requested } elseif ($Existing) { $Existing } else { "session" }
    if ($resolved -notin @("session", "template")) { throw "The test messaging mode must be session or template." }
    return $resolved
}

function Wait-TestWorkspace {
    param([string]$BaseUrl, [string]$AdminKey, [string]$ScenarioVersion = "",
          [string]$ExpectedImage = "", [string]$AppName = "orscheduler", [string]$ResourceGroup = "TherapyScheduler",
          [int]$Attempts = 12, [int]$DelaySeconds = 5)
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            if ($ExpectedImage) {
                $revision = (& az containerapp show --name $AppName --resource-group $ResourceGroup --query "{image:properties.template.containers[0].image,ready:properties.latestReadyRevisionName,latest:properties.latestRevisionName}" --output json --only-show-errors) | ConvertFrom-Json
                if ($LASTEXITCODE -ne 0 -or $revision.image -ne $ExpectedImage -or -not $revision.ready -or $revision.ready -ne $revision.latest) {
                    throw "The requested image is not the ready revision yet."
                }
            }
            $state = Invoke-RestMethod -Uri "$BaseUrl/test/api/state" -Headers @{Authorization = "Bearer $AdminKey"} -TimeoutSec 15
            # During revision rollout, the old app may return its HTML shell
            # with HTTP 200. StrictMode forbids accessing .mode on that string.
            if ($null -ne $state -and $null -ne $state.PSObject.Properties["mode"] -and $state.mode -like "LIVE*") {
                if (-not $ScenarioVersion -or (
                    $null -ne $state.PSObject.Properties["scenario"] -and
                    $null -ne $state.scenario -and
                    $null -ne $state.scenario.PSObject.Properties["version"] -and
                    $state.scenario.version -eq $ScenarioVersion
                )) {
                    # The same scenario version also exists on older revisions.
                    # Wait for the sync routes before declaring this rollout ready.
                    $sync = Invoke-RestMethod -Uri "$BaseUrl/test/api/zoho/status" -Headers @{Authorization = "Bearer $AdminKey"} -TimeoutSec 15
                    if ($null -ne $sync -and
                        $null -ne $sync.PSObject.Properties["source"] -and
                        $sync.source -eq "nidhin.kumar_turnb/test-gokul" -and
                        $null -ne $sync.PSObject.Properties["checkpoint"] -and
                        $null -ne $sync.PSObject.Properties["sourceTimezones"] -and
                        $sync.sourceTimezones.Added_Time -eq "Asia/Kolkata" -and
                        $sync.sourceTimezones.Modified_Time -eq "Asia/Kolkata" -and
                        $null -ne $sync.PSObject.Properties["planStartSource"] -and
                        $sync.planStartSource -eq "Added_Time") {
                        return $state
                    }
                }
            }
        } catch {
            # Startup errors/timeouts may resolve when traffic moves to the new
            # revision. Never print response bodies or authorization headers.
        }
        if ($attempt -lt $Attempts) {
            Write-Host "Waiting for the live test workspace ($attempt/$Attempts)..."
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw "Image/config updates may have succeeded, but the live test endpoint was not verified. Check the revision before rebuilding or publishing offers."
}

$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $projectRoot
try {
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "Create the project .venv and install requirements first." }
    $settingsJson = & $python -c "import json; from pathlib import Path; from flow_test.provision_postgres import read_settings; print(json.dumps(read_settings(Path('flow_test/.env'))))"
    if ($LASTEXITCODE -ne 0) { throw "Could not read local test database settings." }
    $settings = $settingsJson | ConvertFrom-Json
    $databaseUrl = $settings.FLOW_TEST_DATABASE_URL
    $databaseSchema = $settings.FLOW_TEST_DATABASE_SCHEMA
    $scenario = Get-Content -LiteralPath "flow_test/scenario.json" -Raw | ConvertFrom-Json
    if (-not $databaseUrl -or $databaseSchema -ne "flow_test") {
        throw "Expected the existing restricted test database URL and flow_test schema in flow_test/.env."
    }
    $adminKey = (Get-Content -LiteralPath "flow_test/data/admin.token" -Raw).Trim()
    if (-not $adminKey) { throw "The existing local test admin key is empty." }
    # Optional fixture contacts; imported Zoho parents need no separate entry.
    & $python -c "from pathlib import Path; from flow_test.service import load_recipients, load_recipient_names; p=Path('flow_test/recipients.local.json'); (load_recipients(p), load_recipient_names(p)) if p.exists() else None"
    if ($LASTEXITCODE -ne 0) { throw "Test recipients failed validation." }
    # Encode the same configuration the app loader will use, including any
    # explicitly configured deployment override. Encoding is not encryption.
    $recipientsB64 = & $python -c "import base64,json; from pathlib import Path; from flow_test.service import _recipient_data; p=Path('flow_test/recipients.local.json'); data=_recipient_data(p) if p.exists() else {}; print(base64.b64encode(json.dumps(data).encode()).decode())"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare test recipients." }

    # The calendar pushes from the server. Keep OAuth in additive Azure
    # secrets; never include it in UI configuration or the container image.
    $zohoJson = & $python -c "from pathlib import Path; import json; from config import load_local_env_file; load_local_env_file(Path('flow_test/.env')); from flow_test.zoho_push import export_settings; s=export_settings(); print(json.dumps({k:getattr(s,k) for k in ('client_id','client_secret','refresh_token')}))"
    if ($LASTEXITCODE -ne 0) { throw "Could not read saved test Zoho connection." }
    $zoho = $zohoJson | ConvertFrom-Json
    $zohoEnvironment = @()
    $hasZoho = $zoho.client_id -and $zoho.client_secret -and $zoho.refresh_token

    $appJson = Invoke-Azure containerapp show --name $AppName --resource-group $ResourceGroup --query "{mode:properties.configuration.activeRevisionsMode,image:properties.template.containers[0].image,names:properties.template.containers[0].env[].name,host:properties.configuration.ingress.fqdn,messagingModes:properties.template.containers[0].env[?name=='FLOW_TEST_MESSAGE_MODE'].value}" --output json --only-show-errors
    $app = $appJson | ConvertFrom-Json
    $existingMode = if (@($app.messagingModes).Count) { @($app.messagingModes)[0] } else { "" }
    $effectiveMode = Resolve-TestMessagingMode -Requested $MessageMode -Existing $existingMode
    if ($app.mode -ne "Single") { throw "This test worker requires Single revision mode. Review the existing app before deploying." }
    foreach ($required in @("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_GRAPH_API_VERSION")) {
        if ($required -notin $app.names) { throw "The existing app is missing $required." }
    }

    Write-Host "Running local tests; no real WhatsApp messages are sent by these tests."
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; no Azure changes have been made." }

    $tag = "test-flow-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    $repositoryImage = "therapy-scheduler:$tag"
    Write-Host "Building $repositoryImage in the existing registry."
    Invoke-Azure acr build --registry $Registry --image $repositoryImage --file Dockerfile . --only-show-errors

    # These are additive secrets. Existing WhatsApp and production DB secrets
    # remain in place. Local .env and recipients files stay outside the image.
    Invoke-Azure containerapp secret set --name $AppName --resource-group $ResourceGroup --secrets "flow-test-db-url=$databaseUrl" "flow-test-admin-key=$adminKey" "flow-test-recipients=$recipientsB64" --output none --only-show-errors

    if ($hasZoho) {
        Invoke-Azure containerapp secret set --name $AppName --resource-group $ResourceGroup --secrets "flow-test-zoho-client-id=$($zoho.client_id)" "flow-test-zoho-client-secret=$($zoho.client_secret)" "flow-test-zoho-refresh-token=$($zoho.refresh_token)" --output none --only-show-errors
        $zohoEnvironment = @("FLOW_TEST_ZOHO_CLIENT_ID=secretref:flow-test-zoho-client-id", "FLOW_TEST_ZOHO_CLIENT_SECRET=secretref:flow-test-zoho-client-secret", "FLOW_TEST_ZOHO_REFRESH_TOKEN=secretref:flow-test-zoho-refresh-token")
    } else {
        Write-Warning "No complete local test Zoho connection was found. The calendar export will need the saved connection before it can push."
    }

    $loginServer = Invoke-Azure acr show --name $Registry --query loginServer --output tsv --only-show-errors
    Write-Host "Updating the existing app. Previous image: $($app.image)"
    $testEnvironment = @("FLOW_TEST_LIVE=true", "FLOW_TEST_MESSAGE_MODE=$effectiveMode", "FLOW_TEST_OFFER_MINUTES=600", "FLOW_TEST_SEND_BATCH_SIZE=5", "FLOW_TEST_DATABASE_URL=secretref:flow-test-db-url", "FLOW_TEST_DATABASE_SCHEMA=$databaseSchema", "FLOW_TEST_ADMIN_TOKEN=secretref:flow-test-admin-key", "FLOW_TEST_RECIPIENTS_B64=secretref:flow-test-recipients") + $zohoEnvironment
    Invoke-Azure containerapp update --name $AppName --resource-group $ResourceGroup --image "$loginServer/$repositoryImage" --min-replicas 1 --max-replicas 1 --set-env-vars @testEnvironment --output none --only-show-errors

    $baseUrl = "https://$($app.host)"
    Write-Host "Deployment command completed. Checking the authenticated test workspace."
    $state = Wait-TestWorkspace -BaseUrl $baseUrl -AdminKey $adminKey -ScenarioVersion $scenario.version -ExpectedImage "$loginServer/$repositoryImage" -AppName $AppName -ResourceGroup $ResourceGroup
    Write-Host "Live test workspace: $baseUrl/test"
    Write-Host "Use the existing admin key in flow_test/data/admin.token."
    Write-Host "Next: check coworker contact readiness, then generate/review/save tomorrow's calendar to queue its offers."
    Write-Host "This script does not generate appointments or publish offers."
} finally {
    $databaseUrl = $null
    $adminKey = $null
    $recipientsB64 = $null
    $zoho = $null
    $zohoJson = $null
    Pop-Location
}
