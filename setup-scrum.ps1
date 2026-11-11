$ErrorActionPreference = 'Stop'

$toolPath = Join-Path $PSScriptRoot 'self_improve.py'
$jiraUrl = 'https://ishussien.atlassian.net'
$settingsPath = Join-Path $PSScriptRoot 'setup-scrum.json'

function Get-ApplicationGitPaths($path) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { return }
    try {
        $paths = & git -C $path rev-parse --path-format=absolute --show-toplevel --git-common-dir 2>$null
    } catch {
        return
    }
    if ($LASTEXITCODE -eq 0 -and @($paths).Count -eq 2) {
        [pscustomobject]@{
            Root = $paths[0]
            Config = Join-Path $paths[1] 'self-improver/config.json'
        }
    }
}

function Test-ScrumConfiguration($config) {
    return ($config.app_id -ceq 'scrum' -and $config.jira.project -ceq 'SCRUM' -and
        $config.jira.board_id -eq 1 -and ([string]$config.jira.base_url).TrimEnd('/') -eq $jiraUrl)
}

$savedSettings = $null
if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
    try {
        $savedSettings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($savedSettings -isnot [pscustomobject]) { throw 'Expected a JSON object.' }
    } catch {
        $savedSettings = $null
        Write-Warning "Could not read launcher settings at '$settingsPath'. Check that it contains a valid JSON object."
    }
}

function Save-LauncherSettings($applicationPath, $email, $token) {
    $settingsJson = [ordered]@{
        application_path = $applicationPath
        jira_email = $email
        jira_api_token = $token
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
    foreach ($candidate in @((Get-Location).Path, (Join-Path $PSScriptRoot '../Self-Improving App'))) {
        $candidatePaths = Get-ApplicationGitPaths $candidate
        if (-not $candidatePaths -or -not (Test-Path -LiteralPath $candidatePaths.Config -PathType Leaf)) {
            continue
        }
        try {
            $candidateConfig = Get-Content -LiteralPath $candidatePaths.Config -Raw | ConvertFrom-Json
        } catch {
            continue
        }
        if (Test-ScrumConfiguration $candidateConfig) {
            $appPath = $candidatePaths.Root
            break
        }
    }
}

while (-not $appPath) {
    $appPath = ([string](Read-Host 'Application repository path')).Trim().Trim('"')
    if (-not $appPath) { Write-Warning 'An application repository path is required.' }
}

$appPaths = Get-ApplicationGitPaths $appPath
if (-not $appPaths) { throw 'Provide a path inside an existing Git repository in setup-scrum.json or at the path prompt.' }
$appRepo = $appPaths.Root
$configPath = $appPaths.Config

if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if (-not (Test-ScrumConfiguration $config)) {
        throw "The existing configuration at '$configPath' targets a different application or Jira board."
    }
    Write-Host "Using the existing SCRUM configuration at '$configPath'."
} else {
    $initArgs = @(
        '-3.12', $toolPath, '--repo', $appRepo, 'init', '--app-id', 'scrum',
        '--target-branch', 'main', '--jira-url', $jiraUrl, '--project', 'SCRUM', '--board-id', '1',
        '--bug-type', '10003', '--improvement-type', '10003', '--feature-type', '10004',
        '--priority-ids', '1,2,3,4,5', '--backlog-status', 'To Do',
        '--progress-status', 'In Progress', '--done-status', 'Done'
    )
    & py @initArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Initialization failed with exit code $LASTEXITCODE."
    }
}

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

    & py -3.12 $toolPath --repo $appRepo doctor
    if ($LASTEXITCODE -ne 0) {
        throw "Doctor failed with exit code $LASTEXITCODE."
    }
    Save-LauncherSettings $appRepo $jiraEmail $jiraToken

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

    $runArgs = @('-3.12', $toolPath, '--repo', $appRepo, 'run')
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
