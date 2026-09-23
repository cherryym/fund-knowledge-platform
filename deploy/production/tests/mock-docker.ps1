# Synthetic CLI harness only. Does not run a Docker engine or migrate a database.
param([string]$Installer,[string]$Action,[string]$ConfigDir,[string]$ImageDirectory,[string]$CapturePath,[switch]$Ack)
if ($env:FKB_TEST_DOCKER_STUB -ne '1') { throw 'Synthetic test harness requires explicit opt-in' }
$ErrorActionPreference = 'Stop'
$global:FKBSyntheticTracePath = $CapturePath
function global:docker {
    $arguments = @($args | ForEach-Object { [string]$_ })
    Add-Content -LiteralPath $global:FKBSyntheticTracePath -Encoding UTF8 -Value (ConvertTo-Json -InputObject $arguments -Compress)
    $global:LASTEXITCODE = 0
    if ($arguments[0] -eq 'info') { '{"OSType":"linux","Architecture":"x86_64"}'; return }
    if ($arguments[0] -eq 'compose' -and $arguments[1] -eq 'version') { '2.30.0'; return }
    if ($arguments[0] -eq 'compose' -and $arguments -contains 'json') {
        '{"services":{"api":{"image":"fundkb-api:0.1.0-production.20260923"},"web":{"image":"fundkb-web:0.1.0-production.20260923"},"broker":{"image":"rabbitmq:4.3.6"},"qdrant":{"image":"qdrant/qdrant:v1.19.1"},"clamav":{"image":"clamav/clamav:1.5.4"}}}'
        return
    }
    if ($arguments[0] -eq 'image') {
        $reference = $arguments[-1]
        $letter = 'a'
        if ($reference -match 'web|nginx') { $letter = 'b' }
        if ($reference -match 'broker|rabbit') { $letter = 'c' }
        if ($reference -match 'qdrant') { $letter = 'd' }
        if ($reference -match 'clamav') { $letter = 'e' }
        'sha256:' + ($letter * 64) + ' linux/amd64'
        return
    }
    if ($arguments[0] -eq 'save') {
        $position = [array]::IndexOf($arguments, '--output')
        [IO.File]::WriteAllText($arguments[$position + 1], 'SYNTHETIC_NOT_A_DOCKER_IMAGE')
    }
}
$parameters = @{Action=$Action; ConfigDir=$ConfigDir}
if ($ImageDirectory) { $parameters.ImageDirectory=$ImageDirectory }
if ($Ack) { $parameters.AcknowledgeDatabaseChange=$true }
& $Installer @parameters
