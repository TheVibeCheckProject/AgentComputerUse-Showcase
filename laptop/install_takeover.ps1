# Starts this computer's Take over at every login, in the background, so DeskAI can show its screen (through the
# agent on the Mini) when the owner isn't at it. Listens only on the Tailscale address, with the key kept in
# data\takeover_token (the agent's .env holds a copy as LAPTOP_TAKEOVER_TOKEN). Run once:
#   powershell -ExecutionPolicy Bypass -File laptop\install_takeover.ps1
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { $py = (Get-Command pythonw.exe -ErrorAction Stop).Source }
$args = "agentcomputeruse.py takeover-serve --host tailscale --port 8765 --token-file data\takeover_token"
$action = New-ScheduledTaskAction -Execute $py -Argument $args -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AgentComputerUse Take over" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "AgentComputerUse Take over"
"Take over runs at login (task 'AgentComputerUse Take over'), started now."
