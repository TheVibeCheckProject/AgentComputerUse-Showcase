# Runs one command inside the owner's signed-in desktop session on the Mini, and prints its output.
# Why: SSH sessions run outside the desktop: they can't see the screen, use the mouse and keyboard, or reach
# the stored GitHub sign-in. A one-off scheduled task with an Interactive logon runs in the desktop session
# (the same trick the agent's scripts\pull_task.ps1 uses for git pull).
#   powershell -File run_in_session.ps1 -Command "git clone ..." -WorkDir C:\Projects [-TimeoutSec 300]
param(
    [Parameter(Mandatory = $true)][string]$Command,
    [string]$WorkDir = "C:\Projects",
    [int]$TimeoutSec = 300
)
$name = "ACU-Run-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$log = Join-Path $env:TEMP "$name.log"
# Hidden: wscript runs the command with no window (Run(..., 0, True)) and passes its exit code on. A plain cmd.exe
# opened a console on the desktop, on top of the very window a computer-use run was working in (2026-10-08).
$vbs = Join-Path $env:TEMP "$name.vbs"
$cmdline = "cmd.exe /c $Command > `"$log`" 2>&1"
$vbsLine = 'WScript.Quit CreateObject("WScript.Shell").Run("' + $cmdline.Replace('"', '""') + '", 0, True)'
Set-Content -Path $vbs -Value $vbsLine -Encoding Unicode
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$vbs`"" -WorkingDirectory $WorkDir
# The computer name, not USERDOMAIN: over SSH USERDOMAIN doesn't name the local account ("AGENT-MINI\owner")
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds ($TimeoutSec + 30)) -AllowStartIfOnBatteries
try {
    Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
} catch {
    "COULD NOT CREATE THE TASK: $($_.Exception.Message)"
    Remove-Item $vbs -ErrorAction SilentlyContinue
    exit 1
}
try {
    Start-ScheduledTask -TaskName $name
    $end = (Get-Date).AddSeconds($TimeoutSec)
    Start-Sleep -Seconds 2
    while ((Get-ScheduledTask -TaskName $name).State -eq "Running" -and (Get-Date) -lt $end) { Start-Sleep -Seconds 1 }
    $state = (Get-ScheduledTask -TaskName $name).State
    if ($state -eq "Running") {
        # End the whole tree: stopping the task alone left the program it started running (2026-10-08, a
        # computer-use run outlived its timeout). The cmd that writes this task's log is the root.
        Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" |
            Where-Object { $_.CommandLine -like "*$name.log*" } |
            ForEach-Object { & taskkill.exe /T /F /PID $_.ProcessId | Out-Null }
        Stop-ScheduledTask -TaskName $name
        "TIMED OUT after $TimeoutSec s"
    }
    $result = (Get-ScheduledTaskInfo -TaskName $name).LastTaskResult
    if (Test-Path $log) { Get-Content $log -Encoding UTF8 }
    "[exit code $result]"
} finally {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Remove-Item $log -ErrorAction SilentlyContinue
    Remove-Item $vbs -ErrorAction SilentlyContinue
}
