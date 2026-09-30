# Registers the two BotKit autostart tasks (hidden, no console, restart on failure).
# Re-run any time; it replaces only the "BotKit Host" and "BotKit LibreTranslate" tasks.
# Run from an ELEVATED PowerShell with -AtBoot to also start them at boot (before logon).
param([switch]$AtBoot)
$ErrorActionPreference = 'Stop'
$root = 'A:\botkit-host'
$user = "$env:USERDOMAIN\$env:USERNAME"
$defs = @(
  @{ Name='BotKit Host';           Exe="$root\venv\Scripts\pythonw.exe";    Args="`"$root\host.py`" run" },
  @{ Name='BotKit LibreTranslate'; Exe="$root\lt-venv\Scripts\pythonw.exe"; Args="`"$root\host.py`" lt --host 127.0.0.1 --port 5000 --langs en,es,fr,de,pt,ja" }
)
foreach ($d in $defs) {
  $action = New-ScheduledTaskAction -Execute $d.Exe -Argument $d.Args -WorkingDirectory $root
  $triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $user)
  # watchdog: every 5 min; ignored while the task is running, relaunches it if it died
  $wd = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
  $triggers += $wd
  if ($AtBoot) { $triggers += New-ScheduledTaskTrigger -AtStartup }
  $settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
     -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
     -DontStopIfGoingOnBatteries -StartWhenAvailable -DontStopOnIdleEnd
  if ($AtBoot) { $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited }
  else         { $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited }
  Register-ScheduledTask -TaskName $d.Name -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
     -Description 'BotKit hosting (A:\botkit-host). See A:\botkit-host\HOSTING.md' -Force | Out-Null
  "registered: $($d.Name)"
}
