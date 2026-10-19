# Run with Windows PowerShell 5.1 or PowerShell 7. Uses temporary fixtures only.
# Native Windows reparse metadata is doubled; the C# tag classifier is real.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$sourceRoot = Split-Path -Parent $PSScriptRoot
foreach ($file in @('windows_setup.ps1','update_windows_launcher.ps1')) {
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $sourceRoot ('distribution/' + $file)), [ref]$null, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    foreach ($statement in $ast.EndBlock.Statements) {
        if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) { Invoke-Expression $statement.Extent.Text }
    }
}
function Check($Condition, [string]$Message) { if (!$Condition) { throw $Message } }
function Must-Fail([scriptblock]$Run, [string]$Pattern) {
    try { & $Run } catch {
        if ($_.Exception.Message -notlike $Pattern) { throw }
        return
    }
    throw ('Expected rejection matching: ' + $Pattern)
}
Initialize-ProfileMetadata # Compiles the actual native declarations and classifier.
for ($i=0; $i -le 15; $i++) {
    $tag = [Convert]::ToUInt32('9000001A',16) + $i * 4096
    Check ([X4Lan.ProfileMetadata]::IsCloudTag($tag)) ('CLOUD_' + $i + ' rejected')
}
Check ([X4Lan.ProfileMetadata]::IsCloudTag([Convert]::ToUInt32('80000021',16))) 'Legacy OneDrive tag rejected'
foreach ($tag in @('A000000C','A0000003','8000001B','9000002A','B000001A','00000000')) {
    Check (![X4Lan.ProfileMetadata]::IsCloudTag([Convert]::ToUInt32($tag,16))) ('Unsafe/unknown tag allowed: ' + $tag)
}

$fixture = Join-Path ([IO.Path]::GetTempPath()) ('x4lan-profile-' + [Guid]::NewGuid().ToString('N'))
$PackageRoot = Join-Path $fixture 'Windows package with spaces'
$ProfileId = '45841'
$profile = Join-Path $fixture ('OneDrive/Documents/Egosoft/X4/' + $ProfileId)
$Utf8 = New-Object Text.UTF8Encoding($false)
$script:tags = @{}
$script:launchCount = 0
$script:gameRunning = $false
$script:hashFailure = $false
function Get-Item([string]$LiteralPath, [switch]$Force) {
    $item = Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force
    if ($script:tags.ContainsKey($item.FullName)) {
        return [pscustomobject]@{ FullName=$item.FullName; Attributes=($item.Attributes -bor [IO.FileAttributes]::ReparsePoint) }
    }
    return $item
}
function Get-ProfileReparseTag([string]$Path) {
    if ($script:tags[$Path] -eq 'UNREADABLE') { throw 'Metadata fixture failure' }
    return [Convert]::ToUInt32($script:tags[$Path],16)
}
function Get-Process { if ($script:gameRunning) { return 'X4 fixture' } }
function Assert-Game {}
function Start-Process($FilePath, $ArgumentList) { $script:launchCount++; $script:launchArgs = $ArgumentList }
function Get-FileHash([string]$LiteralPath, [string]$Algorithm = 'SHA256') {
    if ($script:hashFailure) { throw 'Checksum fixture failure' }
    return Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm $Algorithm
}
try {
    New-Item -ItemType Directory -Path $profile -Force | Out-Null
    New-Item -ItemType Directory -Path $PackageRoot -Force | Out-Null
    $configPath = Join-Path $profile 'config.xml'
    $original = '<root><autosave>false</autosave><uisafemode>false</uisafemode><res_width>3840</res_width><res_height>2160</res_height><language>49</language></root>'
    [IO.File]::WriteAllText($configPath, $original, $Utf8)
    $stateFile = Join-Path $PackageRoot 'installation.json'
    @{profile=$profile;game='fixture-game';steam='fixture-steam';server='192.0.2.109';package_id='fixture-p2';version='5.0.1'} | ConvertTo-Json | Set-Content -LiteralPath $stateFile
    $pkgFile = Join-Path $PackageRoot 'package.json'
    @{steam_build='23660954';platform='Windows11';release=5;version='5.0.1';pair_id='fixture';package_id='fixture-p2';slot='p2'} | ConvertTo-Json | Set-Content -LiteralPath $pkgFile
    Assert-EmptyTestProfile $profile # Ordinary folder does not invoke native APIs.
    Must-Fail { Assert-EmptyTestProfile (Join-Path $fixture '70000001') } '*Unexpected test profile*'
    $script:tags[$profile] = '9000001A'
    $script:tags[$configPath] = '9000F01A'
    Start-TestGame
    Check ($script:launchCount -eq 1) 'OneDrive did not reach Steam launch'
    Check ($script:launchArgs -contains '45841' -and $script:launchArgs -contains 'custom_creative') 'Isolated launch arguments changed'
    [xml]$after = Get-Content -Raw -LiteralPath $configPath
    Check ($after.root.res_width -eq '1920' -and $after.root.res_height -eq '1080' -and $after.root.texturequality -eq 'low') 'Cloud config not set to 1080p Low'
    Check ($after.root.fullscreen -eq 'true' -and $after.root.borderless -eq 'false') 'Exclusive fullscreen was not selected'
    Check ($after.root.language -eq '49' -and $after.root.autosave -eq 'false') 'Unrelated settings/safeguards changed'
    $backedUp = @(Get-ChildItem -LiteralPath (Join-Path $PackageRoot 'backups') -Recurse -Filter config.xml)
    Check ($backedUp.Count -eq 1 -and [IO.File]::ReadAllText($backedUp[0].FullName) -ceq $original) 'Cloud configuration not backed up exactly'
    $unchangedConfig = [IO.File]::ReadAllText($configPath)
    foreach ($path in @($profile, $configPath)) {
        $oldTag = $script:tags[$path]
        foreach ($tag in @('A000000C','A0000003','8000001B','UNREADABLE')) {
            $script:tags[$path] = $tag
            Must-Fail { Start-TestGame } '*test profile item*'
            Check ($script:launchCount -eq 1) 'Unsafe profile reached Steam launch'
            Check ([IO.File]::ReadAllText($configPath) -ceq $unchangedConfig) 'Rejected profile modified configuration'
        }
        $script:tags[$path] = $oldTag
    }
    $saveDir = Join-Path $profile 'save'
    New-Item -ItemType Directory -Path $saveDir | Out-Null
    $save = Join-Path $saveDir 'save_001.xml.gz'
    [IO.File]::WriteAllText($save, 'save must remain unchanged', $Utf8)
    Must-Fail { Start-TestGame } '*test profile contains saves*'
    Check ([IO.File]::ReadAllText($save) -ceq 'save must remain unchanged') 'Existing save changed'
    Check ($script:launchCount -eq 1) 'Profile with saves launched'
    # A stale linked temporary config must not be followed during graphics writes.
    $temp = Join-Path $profile 'config.x4lan.tmp'
    [IO.File]::WriteAllText($temp, 'unrelated temporary target', $Utf8)
    $script:tags[$temp] = 'A000000C'
    Must-Fail { Assert-SafeProfileItem $temp } '*linked or unsupported*'
    Check ([IO.File]::ReadAllText($temp) -ceq 'unrelated temporary target') 'Temporary target changed'

    # Exercise the actual updater without modifying a game or paired credentials.
    [IO.File]::WriteAllText((Join-Path $PackageRoot 'setup.ps1'), '# Previous launcher', $Utf8)
    [IO.File]::WriteAllText((Join-Path $PackageRoot 'VERSION.txt'), "5.0.1`n", $Utf8)
    $sessionFile = Join-Path $PackageRoot 'session.json'
    [IO.File]::WriteAllText($sessionFile, '{"token":"fixture-private-key"}', $Utf8)
    $modFile = Join-Path $PackageRoot 'extension/x4_lan_lab/ui/config.lua'
    New-Item -ItemType Directory -Path (Split-Path -Parent $modFile) -Force | Out-Null
    [IO.File]::WriteAllText($modFile, 'fixture paired config', $Utf8)
    $lines = @('setup.ps1','VERSION.txt','package.json','session.json','extension/x4_lan_lab/ui/config.lua') | ForEach-Object {
        (Get-FileHash -LiteralPath (Join-Path $PackageRoot $_)).Hash.ToLowerInvariant() + '  ' + $_
    }
    [IO.File]::WriteAllLines((Join-Path $PackageRoot 'SHA256SUMS.txt'), [string[]]$lines, $Utf8)
    $payload = Join-Path $sourceRoot 'distribution/windows_setup.ps1'
    Update-WindowsLauncher $PackageRoot $payload
    $updated = Get-Content -Raw -LiteralPath $pkgFile | ConvertFrom-Json
    $state = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
    Check ($updated.version -eq '5.0.2' -and $updated.pair_id -eq 'fixture' -and $updated.package_id -eq 'fixture-p2') 'Update changed pairing metadata'
    Check ($state.version -eq '5.0.2' -and $state.server -eq '192.0.2.109' -and $state.profile -eq $profile) 'Update changed installed settings'
    Check ([IO.File]::ReadAllText($sessionFile) -ceq '{"token":"fixture-private-key"}') 'Update replaced session key'
    Check ([IO.File]::ReadAllText($modFile) -ceq 'fixture paired config') 'Update replaced mod configuration'
    Check ([IO.File]::ReadAllText($configPath) -ceq $unchangedConfig) 'Update touched game configuration'
    Check ([IO.File]::ReadAllText($save) -ceq 'save must remain unchanged') 'Update touched save'
    foreach ($line in (Get-Content -LiteralPath (Join-Path $PackageRoot 'SHA256SUMS.txt'))) {
        Check ($line -match '^([0-9a-f]{64})  (.+)$') 'Bad checksum line'
        Check ((Get-FileHash -LiteralPath (Join-Path $PackageRoot $Matches[2])).Hash.ToLowerInvariant() -ceq $Matches[1]) 'Updated checksum mismatch'
    }
    $launcherBackup = @(Get-ChildItem -LiteralPath (Join-Path $PackageRoot 'backups') -Recurse -Filter setup.ps1)
    Check ($launcherBackup.Count -eq 1 -and [IO.File]::ReadAllText($launcherBackup[0].FullName) -ceq '# Previous launcher') 'Missing exact launcher backup'
    Update-WindowsLauncher $PackageRoot $payload # Reapplying remains supported.
    $beforeFailure = @{}
    foreach ($name in @('setup.ps1','VERSION.txt','package.json','installation.json','SHA256SUMS.txt')) {
        $beforeFailure[$name] = [IO.File]::ReadAllText((Join-Path $PackageRoot $name))
    }
    $script:hashFailure = $true
    Must-Fail { Update-WindowsLauncher $PackageRoot $payload } '*Checksum fixture failure*'
    $script:hashFailure = $false
    foreach ($name in $beforeFailure.Keys) {
        Check ([IO.File]::ReadAllText((Join-Path $PackageRoot $name)) -ceq $beforeFailure[$name]) ('Failed update did not restore ' + $name)
    }
    $script:gameRunning = $true
    Must-Fail { Update-WindowsLauncher $PackageRoot $payload } '*Close X4*'
    $script:gameRunning = $false
    $updated.version = '5.0.3'
    $updated | ConvertTo-Json | Set-Content -LiteralPath $pkgFile
    Must-Fail { Update-WindowsLauncher $PackageRoot $payload } '*Do not downgrade*'
    $updated.version = '5.0.1'; $updated.platform = 'Linux'
    $updated | ConvertTo-Json | Set-Content -LiteralPath $pkgFile
    Must-Fail { Update-WindowsLauncher $PackageRoot $payload } '*requires a Windows*'
    $updated.platform = 'Windows11'; $updated.PSObject.Properties.Remove('version')
    $updated | ConvertTo-Json | Set-Content -LiteralPath $pkgFile
    Remove-Item -LiteralPath $stateFile # Simulate an uninstalled legacy v5 package.
    Update-WindowsLauncher $PackageRoot $payload
    Check (!(Test-Path -LiteralPath $stateFile)) 'Update invented an installation record'
    Check ((Get-Content -Raw -LiteralPath $pkgFile | ConvertFrom-Json).version -eq '5.0.2') 'Legacy v5 update failed'

    # IPv4 input is validated as text; .NET Framework Parse would read 010 as octal.
    Check ((Assert-IPv4 '192.0.2.109') -eq '192.0.2.109') 'Valid LAN address refused'
    Check ((Assert-IPv4 '127.0.0.1' $true) -eq '127.0.0.1') 'Host loopback refused'
    foreach ($bad in @('192.168.010.5','192.168.08.5','256.1.1.1','1.2.3',' 1.2.3.4',('19' + [char]0x0662 + '.168.1.5'))) {
        Must-Fail { Assert-IPv4 $bad } '*without leading zeros*'
    }
    Must-Fail { Assert-IPv4 '127.0.0.1' } '*not localhost*'
    Must-Fail { Assert-IPv4 '0.0.0.0' $true } '*not localhost*'
    Must-Fail { Assert-IPv4 '255.255.255.255' } '*broadcast*'
    Must-Fail { Assert-IPv4 '224.0.0.1' } '*multicast*'

    # Host entry contract: internal placeholder or the public launcher's concrete host.
    $placeholder = "X4LAN_CONFIG = {`n    host = `"SET_HOST_IP_DURING_INSTALL`",`n    port = 45841,`n}`n"
    Check ((Set-ConfigHost $placeholder '192.168.1.25') -ceq $placeholder.Replace('SET_HOST_IP_DURING_INSTALL','192.168.1.25')) 'Placeholder not replaced'
    Check ((Set-ConfigHost 'host = "127.0.0.1"' '127.0.0.1') -ceq 'host = "127.0.0.1"') 'Public concrete host changed'
    Check ((Set-ConfigHost "    host = `"10.0.0.2`",`r`n" '10.0.0.2') -ceq "    host = `"10.0.0.2`",`r`n") 'CRLF concrete host changed'
    Must-Fail { Set-ConfigHost 'host = "10.0.0.2"' '10.0.0.3' } '*configured for host 10.0.0.2*'
    Must-Fail { Set-ConfigHost 'port = 45841' '10.0.0.3' } '*exactly one host entry*'
    Must-Fail { Set-ConfigHost ("host = `"SET_HOST_IP_DURING_INSTALL`",`nhost = `"SET_HOST_IP_DURING_INSTALL`",`n") '10.0.0.3' } '*exactly one host entry*'

    # Installer integrity with injected failures, using temporary fixtures only.
    $PackageRoot = Join-Path $fixture 'Install package with spaces'
    $GamePath = Join-Path $fixture 'Steam Library/steamapps/common/X4 Foundations'
    $ServerIP = '192.168.1.25'
    $installDocuments = Join-Path $fixture 'Install Documents'
    function Get-DocumentsFolder { return $installDocuments }
    function Get-SteamRoot { return (Join-Path $fixture 'Steam') }
    $sourceMod = Join-Path $PackageRoot 'extension/x4_lan_lab'
    New-Item -ItemType Directory -Path (Join-Path $sourceMod 'ui'), $GamePath -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $sourceMod 'content.xml'), '<content id="x4_lan_lab" />', $Utf8)
    $sourceLua = Join-Path $sourceMod 'ui/config.lua'
    [IO.File]::WriteAllText($sourceLua, $placeholder, $Utf8)
    @{steam_build='23660954';channel='internal';slot='p2';package_id='fixture-install-p2'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PackageRoot 'package.json')
    $installProfile = Join-Path $installDocuments ('Egosoft/X4/' + $ProfileId)
    New-Item -ItemType Directory -Path $installProfile -Force | Out-Null
    $installConfig = Join-Path $installProfile 'config.xml'
    $priorConfig = '<root><language>49</language><autosave>true</autosave><playername>Zo' + [char]0x00EB + '</playername></root>'
    [IO.File]::WriteAllText($installConfig, $priorConfig, $Utf8)
    $installState = Join-Path $PackageRoot 'installation.json'
    [IO.File]::WriteAllText($installState, 'previous installation evidence', $Utf8)
    $extensionsDir = Join-Path $GamePath 'extensions'
    $installedMod = Join-Path $extensionsDir 'x4_lan_lab'
    function Assert-NotInstalled([string]$Label) {
        Check (!(Test-Path -LiteralPath $extensionsDir)) ($Label + ': extensions folder or staging left behind')
        Check ([IO.File]::ReadAllText($installConfig) -ceq $priorConfig) ($Label + ': profile config not restored exactly')
        Check (!(Test-Path -LiteralPath (Join-Path $installProfile 'config.x4lan.tmp'))) ($Label + ': temporary config left behind')
        Check ([IO.File]::ReadAllText($installState) -ceq 'previous installation evidence') ($Label + ': previous state not preserved')
        Check (!(Test-Path -LiteralPath ($installState + '.tmp'))) ($Label + ': temporary state left behind')
    }
    [IO.File]::WriteAllText($sourceLua, 'port = 45841', $Utf8)
    Must-Fail { Install-Package } '*exactly one host entry*'
    Assert-NotInstalled 'Missing host entry'
    [IO.File]::WriteAllText($sourceLua, $placeholder, $Utf8)

    $realCopy = ${function:Copy-ModTree}
    $realPublish = ${function:Publish-StagedMod}
    $realState = ${function:Write-InstallationState}
    ${function:Copy-ModTree} = { param($Source, $Destination)
        New-Item -ItemType Directory -Path $Destination | Out-Null
        [IO.File]::WriteAllText((Join-Path $Destination 'partial.txt'), 'partial copy', $Utf8)
        throw 'Injected copy failure' }
    Must-Fail { Install-Package } '*Injected copy failure*'
    Assert-NotInstalled 'Partial copy'
    ${function:Copy-ModTree} = $realCopy
    ${function:Publish-StagedMod} = { param($Staging, $Target) throw 'Injected publish failure' }
    Must-Fail { Install-Package } '*Injected publish failure*'
    Assert-NotInstalled 'Failure after profile replacement'
    ${function:Publish-StagedMod} = $realPublish
    ${function:Write-InstallationState} = { param($Path, $Text)
        [IO.File]::WriteAllText($Path, $Text, $Utf8)
        throw 'Injected state failure' }
    Must-Fail { Install-Package } '*Injected state failure*'
    Assert-NotInstalled 'Failure after state write'
    ${function:Write-InstallationState} = $realState

    Install-Package # Retry after the injected failures.
    Check ((Get-Content -Raw -LiteralPath (Join-Path $installedMod '.x4lan-package-id')) -ceq 'fixture-install-p2') 'Retry did not install the owned mod'
    Check ((Get-Content -Raw -LiteralPath (Join-Path $installedMod 'ui/config.lua')).Contains('host = "192.168.1.25",')) 'Retry did not configure the host'
    Check (@(Get-ChildItem -LiteralPath $extensionsDir -Force).Count -eq 1) 'Staging folder left beside the mod'
    $installed = Get-Content -Raw -LiteralPath $installState | ConvertFrom-Json
    Check ($installed.server -eq '192.168.1.25' -and $installed.package_id -eq 'fixture-install-p2') 'Retry state wrong'
    [xml]$configured = [IO.File]::ReadAllText($installConfig, $Utf8)
    Check ($configured.root.autosave -eq 'false' -and $configured.root.uisafemode -eq 'false' -and $configured.root.language -eq '49') 'Retry profile settings wrong'
    Check ($configured.root.playername -ceq ('Zo' + [char]0x00EB)) 'BOM-less UTF-8 config value corrupted'
    $configBackups = @(Get-ChildItem -LiteralPath (Join-Path $PackageRoot 'backups') -Recurse -Filter config.xml | Where-Object { [IO.File]::ReadAllText($_.FullName) -ceq $priorConfig })
    Check ($configBackups.Count -ge 1) 'Prior profile config not backed up exactly'

    # Start: a missing safeguard node gives the intended message, not a StrictMode error.
    $launchesBefore = $script:launchCount
    [IO.File]::WriteAllText($installConfig, '<root><uisafemode>false</uisafemode></root>', $Utf8)
    Must-Fail { Start-TestGame } '*must keep autosave=false*'
    Check ($script:launchCount -eq $launchesBefore) 'Unsafe config launched'
    [IO.File]::WriteAllText($installConfig, $configured.OuterXml, $Utf8)

    # Removal archive: same volume beside the package, otherwise outside extensions\.
    Check (Test-SameVolume 'C:\Games\X4' 'c:\Packages\x') 'Same volume not recognised'
    Check (!(Test-SameVolume 'D:\SteamLibrary\X4' 'C:\Packages\x')) 'Different volumes treated as same'
    $realSameVolume = ${function:Test-SameVolume}
    ${function:Test-SameVolume} = { param($First, $Second) $false }
    Remove-Package
    ${function:Test-SameVolume} = $realSameVolume
    Check (!(Test-Path -LiteralPath $installedMod)) 'Cross-volume removal left the mod installed'
    $gameArchive = @(Get-ChildItem -LiteralPath (Join-Path $GamePath 'x4lan-backups') -Directory -Filter 'removed-mod-*')
    Check ($gameArchive.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $gameArchive[0].FullName '.x4lan-package-id'))) 'Cross-volume archive missing in the game folder'
    Check (@(Get-ChildItem -LiteralPath $extensionsDir -Force).Count -eq 0) 'Archive placed inside the scanned extensions folder'
    Install-Package
    Remove-Package
    $packageArchive = @(Get-ChildItem -LiteralPath (Join-Path $PackageRoot 'backups') -Directory -Filter 'removed-mod-*')
    Check ($packageArchive.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $packageArchive[0].FullName '.x4lan-package-id'))) 'Same-volume archive missing beside the package'

    Write-Host 'PASS: cloud tag classification; OneDrive start; links/unknowns/errors blocked; config backup; saved-profile protection; isolated launch; paired updater, backups, checksums, rollback and version guards; uninstalled legacy v5 update.'
    Write-Host 'PASS: IPv4 text validation; host entry contract; install staging, exact rollback and retry after injected failures; UTF-8 profile config; missing safeguard node; removal archive across volumes.'
    Write-Host 'Native Windows metadata reads were simulated. This is not a live Windows/OneDrive test.'
} finally {
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
