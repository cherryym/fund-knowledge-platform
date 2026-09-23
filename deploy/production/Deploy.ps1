#requires -Version 5.1
<##
Windows x64 operator entry. No global execution-policy changes, engine switching,
volume deletion, automatic model calls, credential printing or database downgrade.
##>
[CmdletBinding()]
param(
    [ValidateSet('Verify','Init','Build','LoadImages','ExportImages','Check','Services','Doctor','Migrate','Start','Status','Stop','Sql')]
    [string]$Action = 'Verify',
    [string]$ConfigDir,
    [ValidateSet('Local','Http')][string]$ModelMode = 'Local',
    [ValidatePattern('^[a-z][a-z0-9-]{2,40}$')][string]$ProjectName = 'fundkb-production',
    [ValidatePattern('^(\d{1,3}\.){3}\d{1,3}$')][string]$BindAddress = '127.0.0.1',
    [ValidateRange(1,65535)][int]$HttpsPort = 443,
    [switch]$AcknowledgeDatabaseChange,
    [string]$ImageDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$BundleRoot = $PSScriptRoot
$ComposePath = Join-Path $BundleRoot 'deploy/production/compose.yaml'
$ImageEnvPath = Join-Path $BundleRoot 'deploy/production/images.env'
$ComposeOverrides = @()
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-NewText([string]$Path, [string]$Value) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $bytes = $Utf8.GetBytes($Value); $stream.Write($bytes, 0, $bytes.Length) }
    finally { $stream.Dispose() }
}

function Random-Bytes([int]$Count) {
    $bytes = New-Object byte[] $Count
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return ,$bytes
}

function Assert-Manifest([string]$Directory, [string]$ManifestName) {
    $manifest = Join-Path $Directory $ManifestName
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Missing $ManifestName" }
    $base = [IO.Path]::GetFullPath($Directory).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $count = 0
    foreach ($line in [IO.File]::ReadAllLines($manifest)) {
        if ($line -notmatch '^([a-f0-9]{64})  (.+)$') { throw 'Invalid checksum manifest' }
        $expected = $Matches[1]; $relative = $Matches[2]
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[/\\])\.\.([/\\]|$)') { throw 'Unsafe checksum path' }
        $file = [IO.Path]::GetFullPath((Join-Path $Directory $relative))
        if (-not $file.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Checksum path outside bundle' }
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing package file: $relative" }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw "Checksum mismatch: $relative"
        }
        $count++
    }
    if ($count -lt 1) { throw 'Empty checksum manifest' }
    Write-Host "PASS checksums ($count files)"
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI not found' }
    $info = & docker info --format '{{json .}}' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine unavailable' }
    $engine = $info | ConvertFrom-Json
    if ($engine.OSType -ne 'linux') { throw 'Linux-container engine required. This script never switches engines.' }
    if ($engine.Architecture -notin @('x86_64','amd64')) { throw 'Production package requires a Linux amd64 engine' }
    $versionText = & docker compose version --short 2>$null
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '(\d+)\.(\d+)\.(\d+)') { throw 'Docker Compose unavailable' }
    $version = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
    if ($version -lt [version]'2.30.0') { throw 'Docker Compose >= 2.30.0 required (raw env files)' }
    Write-Host "PASS Linux amd64 / Compose $version"
}

function Invoke-Compose([string[]]$ComposeArguments) {
    & docker compose --project-name $ProjectName --project-directory $BundleRoot --env-file $ImageEnvPath --file $ComposePath @ComposeOverrides @ComposeArguments
    if ($LASTEXITCODE -ne 0) { throw "Compose action failed (exit $LASTEXITCODE). No secrets are printed by this installer." }
}

function Assert-ConfigFiles {
    foreach ($name in @('settings.json','broker.env','qdrant.env')) {
        if (-not (Test-Path -LiteralPath (Join-Path $ConfigDir $name) -PathType Leaf)) { throw "Missing config file: $name" }
    }
    foreach ($name in @('fullchain.pem','privkey.pem')) {
        if (-not (Test-Path -LiteralPath (Join-Path $ConfigDir "tls/$name") -PathType Leaf)) { throw "Missing TLS file: $name" }
    }
    # Do not display JSON values, DSNs, credentials or expanded Compose config.
    try { $settings = Get-Content -LiteralPath (Join-Path $ConfigDir 'settings.json') -Encoding UTF8 -Raw | ConvertFrom-Json }
    catch { throw 'settings.json is not valid JSON' }
    if ($settings.site_url -match 'CHANGE_ME') { throw 'Complete site_url and production settings first' }
    $site = [uri]$settings.site_url
    if ($site.Port -ne $HttpsPort) { throw 'site_url port must equal HttpsPort' }
    Invoke-Compose @('config','--quiet')
}

function Assert-ImageBundle([string]$Directory) {
    Assert-Manifest $Directory 'SHA256SUMS'
    $required = @('fundkb-linux-amd64.tar','image-manifest.json','offline.compose.yaml')
    $names = @([IO.File]::ReadAllLines((Join-Path $Directory 'SHA256SUMS')) | ForEach-Object { $_.Substring(66) })
    if ($names.Count -ne 3 -or @($names | Sort-Object -Unique).Count -ne 3) { throw 'Incomplete image checksum manifest' }
    foreach ($name in $required) { if ($name -notin $names) { throw "Image checksum entry missing: $name" } }
}

function Assert-LoadedImages([string]$Directory) {
    $records = @(Get-Content -LiteralPath (Join-Path $Directory 'image-manifest.json') -Encoding UTF8 -Raw | ConvertFrom-Json)
    $expectedServices = @('api','web','broker','qdrant','clamav')
    if ($records.Count -ne 5 -or @($records.service | Sort-Object -Unique).Count -ne 5) { throw 'Image manifest must contain exactly five distinct images' }
    foreach ($record in $records) {
        if ($record.service -notin $expectedServices -or $record.id -notmatch '^sha256:[a-f0-9]{64}$' -or
            $record.alias -ne "fundkb-bundle/$($record.service):0.1.0-production.20260923") { throw 'Invalid image manifest entry' }
        $actual = & docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' $record.alias
        if ($LASTEXITCODE -ne 0 -or $actual -ne "$($record.id) linux/amd64") { throw "Loaded image identity/platform mismatch: $($record.service)" }
    }
}

function Initialize-Config {
    if ($env:OS -ne 'Windows_NT') { throw 'Init requires Windows. Use the documented Linux procedure for a Linux VM.' }
    if (Test-Path -LiteralPath $ConfigDir) { throw 'Config directory already exists; refusing to overwrite or rotate keys' }
    $null = New-Item -ItemType Directory -Path $ConfigDir
    # Remove inherited access BEFORE generating secrets. Current operator, SYSTEM and Administrators only.
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls $ConfigDir /inheritance:r /grant:r "*${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect config directory ACL; no secrets generated' }
    $null = New-Item -ItemType Directory -Path (Join-Path $ConfigDir 'tls')
    $settings = Get-Content -LiteralPath (Join-Path $BundleRoot 'deploy/production/settings.example.json') -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($ModelMode -eq 'Local') {
        $app = $settings.application
        $app.FKB_EMBEDDING_MODE = 'transformers'
        $app.FKB_EMBEDDING_BASE_URL = $null
        $app.FKB_EMBEDDING_REVISION = 'loaded-from-prepared-local-profile'
        $app.FKB_RERANKER_MODE = 'local'
        $app | Add-Member -NotePropertyName FKB_RETRIEVAL_PROFILE -NotePropertyValue '/app/data/production-profiles/qwen3-4b.json'
        $app | Add-Member -NotePropertyName FKB_RETRIEVAL_PROFILES_FILE -NotePropertyValue '/app/data/production-profiles/registry.json'
    }
    $master = [Convert]::ToBase64String((Random-Bytes 32)).Replace('+','-').Replace('/','_')
    $broker = ([BitConverter]::ToString((Random-Bytes 32))).Replace('-','').ToLowerInvariant()
    $qdrant = ([BitConverter]::ToString((Random-Bytes 32))).Replace('-','').ToLowerInvariant()
    $settings.application.FKB_PROVIDER_MASTER_KEY = $master
    $settings.application.FKB_CELERY_BROKER_URL = "amqp://fundkb:${broker}@broker:5672/fundkb"
    $settings.application.FKB_QDRANT_API_KEY = $qdrant
    Write-NewText (Join-Path $ConfigDir 'settings.json') ($settings | ConvertTo-Json -Depth 30)
    Write-NewText (Join-Path $ConfigDir 'broker.env') "RABBITMQ_DEFAULT_USER=fundkb`nRABBITMQ_DEFAULT_PASS=$broker`nRABBITMQ_DEFAULT_VHOST=fundkb`n"
    Write-NewText (Join-Path $ConfigDir 'qdrant.env') "QDRANT__SERVICE__API_KEY=$qdrant`nQDRANT__TELEMETRY_DISABLED=true`n"
    Write-Host 'Initialized protected config. Complete Oracle/OIDC/embedding/site settings and place TLS certificate/key in tls. Keep this directory out of Git and backups must be encrypted.'
}

$oldEnvironment = @{}
foreach ($name in @('FKB_CONFIG_DIR','FKB_BIND_IP','FKB_HTTPS_PORT','COMPOSE_DISABLE_ENV_FILE')) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    Assert-Manifest $BundleRoot 'SHA256SUMS'
    if ($Action -eq 'Verify') { return }
    if (-not $ConfigDir) {
        if (-not $env:ProgramData) { throw 'Supply -ConfigDir explicitly' }
        $ConfigDir = Join-Path $env:ProgramData 'FundKB/production'
    }
    $ConfigDir = [IO.Path]::GetFullPath($ConfigDir)
    if ($ConfigDir -eq $BundleRoot.TrimEnd('\','/') -or $ConfigDir.StartsWith($BundleRoot.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'ConfigDir must be outside the immutable installer directory'
    }
    if ($Action -eq 'Init') { Initialize-Config; return }
    Assert-Docker
    $env:FKB_CONFIG_DIR = $ConfigDir.Replace('\','/')
    $env:FKB_BIND_IP = $BindAddress
    $env:FKB_HTTPS_PORT = [string]$HttpsPort
    $env:COMPOSE_DISABLE_ENV_FILE = '1'
    if (-not $ImageDirectory -and (Test-Path -LiteralPath (Join-Path $BundleRoot 'images/offline.compose.yaml'))) {
        $ImageDirectory = Join-Path $BundleRoot 'images'
    }
    if ($ImageDirectory -and $Action -notin @('Build','ExportImages')) {
        $ImageDirectory = [IO.Path]::GetFullPath($ImageDirectory)
        Assert-ImageBundle $ImageDirectory
        $ComposeOverrides = @('--file', (Join-Path $ImageDirectory 'offline.compose.yaml'))
        if ($Action -ne 'LoadImages') { Assert-LoadedImages $ImageDirectory }
    }
    if ($Action -eq 'LoadImages') {
        if (-not $ImageDirectory) { $ImageDirectory = Join-Path $BundleRoot 'images' }
        Assert-ImageBundle $ImageDirectory
        $archive = Join-Path $ImageDirectory 'fundkb-linux-amd64.tar'
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw 'Missing image archive' }
        & docker load --input $archive
        if ($LASTEXITCODE -ne 0) { throw 'Image load failed' }
        Assert-LoadedImages $ImageDirectory
        Write-Host 'Image IDs and platform verified. Pass the same -ImageDirectory on Check/Services/Doctor/Migrate/Start.'
        return
    }
    # Build/export need placeholder files to exist, not real DB credentials or TLS.
    if ($Action -eq 'Build') {
        Invoke-Compose @('build','--pull','api','web')
        Invoke-Compose @('pull','broker','qdrant','clamav')
        Write-Host 'Images built; run ExportImages to prepare an offline handoff. Production not started.'
        return
    }
    if ($Action -eq 'ExportImages') {
        if (-not $ImageDirectory) { throw 'Provide a new -ImageDirectory outside the installer' }
        if (Test-Path -LiteralPath $ImageDirectory) { throw 'ImageDirectory exists; refusing overwrite' }
        $null = New-Item -ItemType Directory -Path $ImageDirectory
        $resolved = & docker compose --project-name $ProjectName --project-directory $BundleRoot --env-file $ImageEnvPath --file $ComposePath config --format json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve image-only manifest' }
        $configuration = ($resolved -join "`n") | ConvertFrom-Json
        $records = @(); $aliases = @(); $overrideLines = @('services:')
        foreach ($service in @('api','web','broker','qdrant','clamav')) {
            $image = $configuration.services.$service.image
            $identity = & docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' $image
            if ($LASTEXITCODE -ne 0 -or $identity -notmatch '^(sha256:[a-f0-9]{64}) linux/amd64$') { throw 'Missing or wrong-architecture image' }
            $imageId = $Matches[1]
            $alias = "fundkb-bundle/${service}:0.1.0-production.20260923"
            & docker tag $image $alias
            if ($LASTEXITCODE -ne 0) { throw 'Cannot create archive-local image tag' }
            $records += [PSCustomObject]@{service=$service; alias=$alias; id=$imageId; source=$image; platform='linux/amd64'}
            $aliases += $alias
            $overrideLines += @("  ${service}:", "    image: $alias", '    pull_policy: never')
            if ($service -eq 'api') { $overrideLines += @('  worker:', "    image: $alias", '    pull_policy: never') }
        }
        $archive = Join-Path $ImageDirectory 'fundkb-linux-amd64.tar'
        & docker save --output $archive @aliases
        if ($LASTEXITCODE -ne 0) { throw 'Image export failed' }
        Write-NewText (Join-Path $ImageDirectory 'image-manifest.json') ($records | ConvertTo-Json -Depth 10)
        Write-NewText (Join-Path $ImageDirectory 'offline.compose.yaml') (($overrideLines -join "`n") + "`n")
        $lines = foreach ($name in @('fundkb-linux-amd64.tar','image-manifest.json','offline.compose.yaml')) {
            ((Get-FileHash -LiteralPath (Join-Path $ImageDirectory $name) -Algorithm SHA256).Hash.ToLowerInvariant()) + '  ' + $name
        }
        Write-NewText (Join-Path $ImageDirectory 'SHA256SUMS') (($lines -join "`n") + "`n")
        Write-Host 'Exported verified-architecture images; databases, volumes, credentials and model weights are NOT included.'
        return
    }
    if ($Action -eq 'Status') { Invoke-Compose @('ps','--all'); return }
    if ($Action -eq 'Stop') {
        Invoke-Compose @('stop','web','api','worker')
        Write-Host 'Stopped this project application services only. Infrastructure and all volumes retained.'
        return
    }
    Assert-ConfigFiles
    Invoke-Compose @('run','--rm','--no-deps','api','config')
    switch ($Action) {
        'Check' { Write-Host 'Configuration valid; no DB changes or model calls.' }
        'Services' { Invoke-Compose @('up','-d','--no-build','--pull','never','--wait','--wait-timeout','900','broker','qdrant','clamav') }
        'Doctor' { Invoke-Compose @('run','--rm','--no-deps','api','doctor') }
        'Migrate' {
            if (-not $AcknowledgeDatabaseChange) { throw 'DBA approval/backup required. Supply -AcknowledgeDatabaseChange only after reading the runbook.' }
            Invoke-Compose @('run','--rm','--no-deps','api','doctor')
            Invoke-Compose @('run','--rm','--no-deps','api','migrate','--ack-empty-or-backed-up-schema')
        }
        'Start' {
            Invoke-Compose @('run','--rm','--no-deps','api','ready')
            Invoke-Compose @('up','-d','--no-build','--pull','never','--wait','--wait-timeout','180','api','worker','web')
            Write-Host 'Containers healthy. Complete HTTPS/OIDC/ACL/upload/retrieval acceptance before releasing to users.'
        }
        'Sql' { Invoke-Compose @('run','--rm','--no-deps','api','sql') }
    }
}
finally {
    foreach ($name in $oldEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], 'Process') }
}
