$ErrorActionPreference = 'Stop'

$toolPath = Join-Path $PSScriptRoot 'self_improve.py'
$settingsPath = Join-Path $PSScriptRoot 'setup-scrum.json'

function Get-ApplicationRoot($path) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { return }
    try {
        $root = & git -C $path rev-parse --show-toplevel 2>$null
    } catch {
        return
    }
    if ($LASTEXITCODE -eq 0) { $root }
}

$savedSettings = $null
if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
    try {
        $savedSettings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($savedSettings -isnot [pscustomobject]) { throw 'Expected a JSON object.' }
    } catch {
        throw "Could not read launcher settings at '$settingsPath'. Check that it contains a valid JSON object."
    }
}

function Save-LauncherSettings($applicationPath, $email, $token, $resolved) {
    $settingsJson = [ordered]@{
        application_path = $applicationPath
        jira_url = $resolved.jira_url
        jira_project = $resolved.jira_project
        jira_board_id = $resolved.jira_board_id
        jira_email = $email
        jira_api_token = $token
        skills_commit = $resolved.skills_commit
    } | ConvertTo-Json

    $tempPath = Join-Path $PSScriptRoot ("setup-scrum-{0}.tmp" -f [guid]::NewGuid())
    try {
        [IO.File]::WriteAllText($tempPath, $settingsJson, [Text.Encoding]::UTF8)
        if ([IO.File]::Exists($settingsPath)) {
            [IO.File]::Replace($tempPath, $settingsPath, [NullString]::Value)
        } else {
            [IO.File]::Move($tempPath, $settingsPath)
        }
    } finally {
        [IO.File]::Delete($tempPath)
    }
    Write-Host "Saved launcher settings to '$settingsPath'."
}

$appPath = $savedSettings.application_path
if ($appPath -isnot [string] -or [string]::IsNullOrWhiteSpace($appPath)) {
    $appPath = $null
}

while (-not $appPath) {
    $appPath = ([string](Read-Host 'Application repository path')).Trim().Trim('"')
    if (-not $appPath) { Write-Warning 'An application repository path is required.' }
}

$appRepo = Get-ApplicationRoot $appPath
if (-not $appRepo) { throw 'Provide a path inside an existing Git repository in setup-scrum.json or at the path prompt.' }
$toolArgs = @('-3.12', $toolPath, '--repo', $appRepo, '--launcher-settings', $settingsPath)

$previousJiraEmail = $env:JIRA_EMAIL
$previousJiraToken = $env:JIRA_API_TOKEN
try {
    $sessionEmail = ([string]$previousJiraEmail).Trim()
    $savedEmail = ''
    if ($savedSettings.jira_email -is [string]) { $savedEmail = $savedSettings.jira_email.Trim() }
    $jiraEmail = $savedEmail
    if (-not $jiraEmail) { $jiraEmail = $sessionEmail }
    while (-not $jiraEmail) {
        $jiraEmail = ([string](Read-Host 'Exact Atlassian sign-in email')).Trim()
        if (-not $jiraEmail) { Write-Warning 'An Atlassian email is required.' }
    }

    $jiraToken = $null
    if ($savedSettings.jira_api_token -is [string] -and
        -not [string]::IsNullOrWhiteSpace($savedSettings.jira_api_token)) {
        $jiraToken = $savedSettings.jira_api_token
    } elseif (-not [string]::IsNullOrWhiteSpace($previousJiraToken) -and
        (-not $sessionEmail -or $sessionEmail -eq $jiraEmail)) {
        $jiraToken = $previousJiraToken
    }
    while ([string]::IsNullOrWhiteSpace($jiraToken)) {
        $enteredToken = Read-Host 'Jira API token' -AsSecureString
        if ($enteredToken) {
            try {
                if ($enteredToken.Length -gt 0) {
                    $jiraToken = [System.Net.NetworkCredential]::new('', $enteredToken).Password
                }
            } finally {
                $enteredToken.Dispose()
            }
        }
        if ([string]::IsNullOrWhiteSpace($jiraToken)) { Write-Warning 'A Jira API token is required.' }
    }

    $env:JIRA_EMAIL = $jiraEmail
    $env:JIRA_API_TOKEN = $jiraToken

    $doctorOutput = & py @toolArgs doctor
    $doctorExit = $LASTEXITCODE
    if ($doctorExit -ne 0) {
        $doctorOutput | Write-Host
        throw "Doctor failed with exit code $doctorExit."
    }
    $doctorResult = ($doctorOutput -join "`n") | ConvertFrom-Json
    $resolved = $doctorResult.launcher_settings
    Write-Host "Using Jira project '$($resolved.jira_project)', board $($resolved.jira_board_id), at '$($resolved.jira_url)'."
    $doctorOutput | Write-Host
    Save-LauncherSettings $appRepo $jiraEmail $jiraToken $resolved

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
} finally {
    $env:JIRA_EMAIL = $previousJiraEmail
    $env:JIRA_API_TOKEN = $previousJiraToken
}
