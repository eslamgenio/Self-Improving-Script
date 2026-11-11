$ErrorActionPreference = 'Stop'

$toolPath = Join-Path $PSScriptRoot 'self_improve.py'
$settingsPath = Join-Path $PSScriptRoot 'setup-scrum.json'
$toolArgs = @('-3.12', $toolPath, '--launcher-settings', $settingsPath)

& py @toolArgs doctor
if ($LASTEXITCODE -ne 0) {
    throw "Doctor failed with exit code $LASTEXITCODE."
}
Write-Host "Launcher settings are stored in '$settingsPath'."

do {
    $runMode = (Read-Host 'Run mode: [1] One cycle (default), [2] Continuous, [3] Exit').Trim()
    if ($runMode -notin @('', '1', '2', '3')) {
        Write-Warning 'Enter 1 for one cycle, 2 for continuous mode, or 3 to exit.'
    }
} while ($runMode -notin @('', '1', '2', '3'))

if ($runMode -eq '3') {
    Write-Host 'Doctor passed. Exiting without starting an improvement cycle.'
    return
}

$runArgs = $toolArgs + 'run'
if ($runMode -eq '2') {
    Write-Host 'Running continuously. Press Ctrl+C to stop.'
} else {
    $runArgs += '--once'
}
& py @runArgs
if ($LASTEXITCODE -notin @(0, 130)) {
    throw "The improvement run ended with exit code $LASTEXITCODE."
}
