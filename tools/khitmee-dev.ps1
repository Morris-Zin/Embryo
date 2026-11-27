# Send argv as JSON so Windows PowerShell 5 and pwsh preserve nested quotes.
$cliArgs = [string[]]$args
if ($cliArgs.Count -gt 0 -and $cliArgs[-1] -eq '-') {
    $cliArgs[-1] = (($input | Out-String) -replace "`r`n", "`n").TrimStart([char]0xfeff)
}
$argumentsJson = ConvertTo-Json -InputObject $cliArgs -Compress
$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($argumentsJson))
& wsl.exe -d KhitmeeERPNext --cd (Split-Path -Parent $PSScriptRoot) --exec python3 -B tools/khitmee_dev.py --windows-args $payload
exit $LASTEXITCODE
