# Register all crypto-trend scheduled tasks (run once; safe to re-run, it overwrites)
# usage: powershell -ExecutionPolicy Bypass -File C:\CODE\crypto-trend\setup_tasks.ps1
$dir = "C:\CODE\crypto-trend"

# run on battery, catch up missed runs, wake from sleep, kill if stuck > 10 min
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# S4U = run in background (no black window to close by mistake), even when screen is locked
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

function Reg($name, $bat, $trigger) {
    $action = New-ScheduledTaskAction -Execute "$dir\$bat" -WorkingDirectory $dir
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "OK: $name -> $bat"
}

Reg "CryptoPaper"    "run_paper.bat"        (New-ScheduledTaskTrigger -Daily -At 9:05am)
Reg "CryptoWatch_AM" "run_watch_report.bat" (New-ScheduledTaskTrigger -Daily -At 9:10am)
Reg "CryptoWatch_PM" "run_watch_report.bat" (New-ScheduledTaskTrigger -Daily -At 9:10pm)

$h = New-ScheduledTaskTrigger -Daily -At 12:40am
$h.Repetition = (New-ScheduledTaskTrigger -Once -At 12:40am -RepetitionInterval (New-TimeSpan -Hours 1) `
                 -RepetitionDuration (New-TimeSpan -Hours 23)).Repetition
Reg "CryptoWatch_Hourly" "run_watch.bat" $h

Write-Host ""
Get-ScheduledTask -TaskName "Crypto*" | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastRunTime, LastTaskResult | Format-Table -AutoSize
