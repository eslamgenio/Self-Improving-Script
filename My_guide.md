$env:JIRA_EMAIL = Read-Host 'Atlassian email'

$jiraToken = Read-Host 'Jira API token' -AsSecureString

$env:JIRA_API_TOKEN = [System.Net.NetworkCredential]::new('', $jiraToken).Password

$tool = 'C:\Eslam\2- Areas\Projects\Self-Improving Script\self_improve.py'

Set-Location 'C:\Eslam\2- Areas\Projects\Self-Improving App'

py -3 $tool --repo 'C:\Eslam\2- Areas\Projects\Self-Improving App' doctor

py -3 $tool init --app-id scrum `
  --jira-url 'https://ishussien.atlassian.net' `
  --project Scrum --board-id 1
