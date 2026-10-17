<#
.SYNOPSIS
  Richtet sub_translate.py als geplante Aufgabe ein (Start bei Anmeldung, unsichtbar, ohne Zeitlimit).

.EXAMPLE
  .\install_task.ps1 -Watch "C:\Users\me\Desktop\Filme" -MoveTo "\\SERVER\Medien\Filme"

.EXAMPLE
  .\install_task.ps1 -Watch "D:\Downloads\Filme" -Extra "--ocr-german --min-size-mb 20"
#>
param(
    [Parameter(Mandatory = $true)] [string] $Watch,          # Ordner, in dem neue Filme landen
    [string] $MoveTo = "",                                    # Zielordner (z. B. Medienserver), leer = nicht verschieben
    [int]    $Parallel = 5,                                   # gleichzeitige Anfragen an Ollama
    [string] $InstallDir = $PSScriptRoot,                     # Ordner mit sub_translate.py und venv
    [string] $TaskName = "SubTranslate",
    [string] $Extra = "--prefer-sdh --move-skipped"           # weitere Optionen
)

$ErrorActionPreference = "Stop"
$pythonw = Join-Path $InstallDir "venv\Scripts\pythonw.exe"
$script  = Join-Path $InstallDir "sub_translate.py"
$logFile = Join-Path $InstallDir "log.txt"

foreach ($p in @($pythonw, $script)) {
    if (-not (Test-Path $p)) { throw "Nicht gefunden: $p (siehe README, Abschnitt Installation)" }
}
if (-not (Test-Path $Watch)) { throw "Überwachter Ordner existiert nicht: $Watch" }

$watchArg = $Watch -replace '\\', '/'
$taskArgs = "`"$script`" `"$watchArg`" --watch --ollama-parallel $Parallel --log-file `"$logFile`""
if ($MoveTo) {
    if (-not (Test-Path $MoveTo)) { Write-Warning "Zielordner im Moment nicht erreichbar: $MoveTo" }
    $taskArgs += " --move-to `"$MoveTo`""
} else {
    $Extra = ($Extra -replace '--move-skipped', '').Trim()
}
if ($Extra) { $taskArgs += " $Extra" }

$action   = New-ScheduledTaskAction -Execute $pythonw -WorkingDirectory $InstallDir -Argument $taskArgs
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT1M"   # eine Minute warten, damit Ollama zuerst starten kann
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Untertitel automatisch ins Deutsche übersetzen (sub_translate.py)" -Force | Out-Null

Write-Host "Aufgabe '$TaskName' eingerichtet:"
Write-Host "  $pythonw $taskArgs"
Write-Host ""
Write-Host "Starten:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Log:      Get-Content `"$logFile`" -Tail 30 -Encoding UTF8 -Wait"
