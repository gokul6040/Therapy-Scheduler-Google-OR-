"""Azure CLI's Windows YAML reader must accept the job configuration bytes."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


POWERSHELL = shutil.which("powershell.exe")
SCRIPT = Path(__file__).resolve().parents[1] / "flow_test/deploy_zoho_job.ps1"


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell 5 compatibility check")
@pytest.mark.parametrize("value", ['dummy-token: {value} \\"quoted\\"', "Fictional café മലയാളം 🙂"])
def test_job_configuration_round_trips_with_windows_cli_default_encoding(tmp_path, value):
    source = tmp_path / "dummy-input.json"
    output = tmp_path / "job.json"
    expected = {"properties": {"configuration": {"secrets": [{"name": "dummy", "value": value}]}}}
    source.write_text(json.dumps(expected, ensure_ascii=True), encoding="ascii")
    command = r"""param($scriptPath, $inputPath, $outputPath)
    $ErrorActionPreference = 'Stop'
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Script syntax error' }
    $writer = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Write-AzureJobConfiguration'
    }, $true)
    Invoke-Expression $writer.Extent.Text
    $document = @{ properties = (Get-Content -LiteralPath $inputPath -Raw | ConvertFrom-Json).properties }
    Write-AzureJobConfiguration -Path $outputPath -Document $document
"""
    driver = tmp_path / "write-config.ps1"
    driver.write_text(command, encoding="ascii")
    # Paths are positional arguments, not interpolated into executable code.
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
         str(driver), str(SCRIPT), str(source), str(output)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    raw = output.read_bytes()
    assert raw and all(byte < 128 for byte in raw)
    assert json.loads(raw.decode("cp1252")) == expected


@pytest.mark.skipif(POWERSHELL is None, reason="Windows deployment compatibility check")
@pytest.mark.parametrize("requested,existing,expected", [
    ("", "template", "template"), ("", "session", "session"),
    ("", "", "session"), ("template", "session", "template"),
])
def test_app_deployment_preserves_existing_messaging_mode(tmp_path, requested, existing, expected):
    script = SCRIPT.with_name("deploy_azure.ps1")
    command = r"""param($scriptPath, $requested, $existing)
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Script syntax error' }
    $resolver = $ast.Find({param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Resolve-TestMessagingMode'
    }, $true)
    Invoke-Expression $resolver.Extent.Text
    Resolve-TestMessagingMode -Requested $requested -Existing $existing
"""
    driver = tmp_path / "resolve-mode.ps1"
    driver.write_text(command, encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
                             str(driver), str(script), requested, existing],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.skipif(POWERSHELL is None, reason="Windows deployment compatibility check")
@pytest.mark.parametrize("ready_image,expected", [("old-image", "blocked"), ("new-image", "ready")])
def test_readiness_requires_requested_image_not_just_healthy_old_endpoints(tmp_path, ready_image, expected):
    driver = tmp_path / "readiness.ps1"
    driver.write_text(r"""param($scriptPath, $readyImage)
    $ErrorActionPreference = 'Stop'
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Script syntax error' }
    $definition = $ast.Find({param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-TestWorkspace'
    }, $true)
    Invoke-Expression $definition.Extent.Text
    function az { $global:LASTEXITCODE=0; @{image=$readyImage;ready='revision-2';latest='revision-2'} | ConvertTo-Json }
    function Invoke-RestMethod {
        param($Uri, $Headers, $TimeoutSec)
        if ($Uri.EndsWith('/state')) { return [pscustomobject]@{mode='LIVE test'; scenario=[pscustomobject]@{version='v3'}} }
        return [pscustomobject]@{source='nidhin.kumar_turnb/test-gokul'; checkpoint=$null; sourceTimezones=[pscustomobject]@{Added_Time='Asia/Kolkata';Modified_Time='Asia/Kolkata'};planStartSource='Added_Time'}
    }
    try {
        $null=Wait-TestWorkspace -BaseUrl 'https://example.invalid' -AdminKey 'dummy' -ExpectedImage 'new-image' -Attempts 1
        Write-Output 'ready'
    } catch { Write-Output 'blocked' }
""", encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
                             str(driver), str(SCRIPT.with_name("deploy_azure.ps1")), ready_image],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
