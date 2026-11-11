$tool = 'C:\Eslam\2- Areas\Projects\Self-Improving Script\self_improve.py'

# Set up and verify the launcher settings.
py -3.12 $tool init
py -3.12 $tool doctor

# Set up a separate application repository configuration.
Set-Location 'C:\Eslam\2- Areas\Projects\Self-Improving App'
py -3.12 $tool --repo-settings init --app-id scrum `
  --jira-url 'https://ishussien.atlassian.net' `
  --project Scrum --board-id 1

$env:JIRA_EMAIL = Read-Host 'Atlassian email'
$jiraToken = Read-Host 'Jira API token' -AsSecureString
$env:JIRA_API_TOKEN = [System.Net.NetworkCredential]::new('', $jiraToken).Password
py -3.12 $tool --repo-settings doctor
