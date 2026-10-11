param(
    [ValidateSet('Install','Start','Test','Diagnose','Remove','Version')][string]$Action = 'Install',
    [string]$GamePath = '',
    [string]$ServerIP = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$PackageRoot = $PSScriptRoot
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$ProfileId = '45841'

function Get-PackageVersion {
    $path = Join-Path $PackageRoot 'VERSION.txt'
    if (Test-Path -LiteralPath $path) { return (Get-Content -Raw -LiteralPath $path).Trim() }
    return 'legacy (no VERSION.txt)'
}
function Read-State {
    $path = Join-Path $PackageRoot 'installation.json'
    if (!(Test-Path -LiteralPath $path)) { throw 'Run 1-INSTALL.cmd first.' }
    return Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
}
function Assert-Closed {
    if (Get-Process -Name X4 -ErrorAction SilentlyContinue) { throw 'X4 is still running. Close it first.' }
}
function Get-SteamRoot {
    try {
        $p = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam').SteamPath
        if (Test-Path -LiteralPath (Join-Path $p 'steam.exe')) { return $p }
    } catch {}
    $p = Join-Path ${env:ProgramFiles(x86)} 'Steam'
    if (Test-Path -LiteralPath (Join-Path $p 'steam.exe')) { return $p }
    throw 'Steam was not found. Install Steam and sign in first.'
}
function Find-Game([string]$SteamRoot) {
    $libraries = @($SteamRoot)
    $vdf = Join-Path $SteamRoot 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $vdf) {
        foreach ($m in [regex]::Matches((Get-Content -Raw -LiteralPath $vdf), '"path"\s+"([^"]+)"')) {
            $libraries += $m.Groups[1].Value.Replace('\\', '\')
        }
    }
    $found = @()
    foreach ($lib in ($libraries | Select-Object -Unique)) {
        $manifest = Join-Path $lib 'steamapps\appmanifest_392160.acf'
        if (!(Test-Path -LiteralPath $manifest)) { continue }
        $m = [regex]::Match((Get-Content -Raw -LiteralPath $manifest), '"installdir"\s+"([^"]+)"')
        if ($m.Success) {
            $candidate = Join-Path (Join-Path $lib 'steamapps\common') $m.Groups[1].Value
            if (Test-Path -LiteralPath (Join-Path $candidate 'X4.exe')) { $found += $candidate }
        }
    }
    $found = @($found | Select-Object -Unique)
    if ($found.Count -eq 1) { return $found[0] }
    if ($found.Count -gt 1) { $found | ForEach-Object { Write-Host $_ } }
    return (Read-Host 'Enter the X4 game folder (Steam > Properties > Installed Files > Browse)').Trim('"')
}
function Assert-Game([string]$Path, [string]$Build) {
    if (!(Test-Path -LiteralPath (Join-Path $Path 'X4.exe'))) { throw 'X4.exe was not found in the selected folder.' }
    if (!(Test-Path -LiteralPath (Join-Path $Path 'lua51_64.dll'))) { throw 'lua51_64.dll is missing. This package requires the 64-bit Steam game.' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $Path 'version.dat')).Trim() -ne '900') { throw 'This package requires X4 9.00.' }
    $steamApps = Split-Path -Parent (Split-Path -Parent $Path)
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $steamApps 'appmanifest_392160.acf')
    $m = [regex]::Match($manifest, '"buildid"\s+"(\d+)"')
    if (!$m.Success -or $m.Groups[1].Value -ne $Build) { throw ('Steam build does not match this package. Expected: ' + $Build + '. Do not test with different builds.') }
}
function Assert-IPv4([string]$Address, [bool]$AllowLoopback = $false) {
    if ($Address -notmatch '^(\d{1,3}\.){3}\d{1,3}$') { throw 'Enter an IPv4 address such as 192.168.1.25 or the host ZeroTier IPv4.' }
    foreach ($part in $Address.Split('.')) { if ([int]$part -gt 255) { throw 'Invalid IPv4 address.' } }
    $ip = [System.Net.IPAddress]::Parse($Address)
    if ((!$AllowLoopback -and [System.Net.IPAddress]::IsLoopback($ip)) -or $Address -eq '0.0.0.0') { throw 'Enter the host LAN or ZeroTier IP, not localhost.' }
    return $ip.ToString()
}
function Initialize-ProfileMetadata {
    if ('X4Lan.ProfileMetadata' -as [type]) { return }
    # Open the item itself, without following a link or reading cloud file contents.
    # Windows PowerShell 5.1 does not expose every reparse tag through Get-Item.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace X4Lan {
    public static class ProfileMetadata {
        [StructLayout(LayoutKind.Sequential)]
        private struct AttributeTagInfo {
            public uint FileAttributes;
            public uint ReparseTag;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access,
            uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetFileInformationByHandleEx(SafeFileHandle handle,
            int infoClass, out AttributeTagInfo info, uint size);
        public static uint ReadTag(string path) {
            // OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT; share R/W/delete.
            using (SafeFileHandle handle = CreateFileW(path, 0, 7, IntPtr.Zero,
                    3, 0x02200000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                AttributeTagInfo info;
                if (!GetFileInformationByHandleEx(handle, 9, out info, 8))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return info.ReparseTag;
            }
        }
        public static bool IsCloudTag(uint tag) {
            // IO_REPARSE_TAG_CLOUD and CLOUD_1..F, plus the legacy ONEDRIVE tag.
            // Do not accept symbolic links, junctions or unknown reparse providers.
            return (tag & 0xFFFF0FFFu) == 0x9000001Au || tag == 0x80000021u;
        }
    }
}
'@
}
function Get-ProfileReparseTag([string]$Path) {
    Initialize-ProfileMetadata
    return [X4Lan.ProfileMetadata]::ReadTag($Path)
}
function Assert-SafeProfileItem([string]$Path) {
    if (!(Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (!($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return }
    try {
        $tag = Get-ProfileReparseTag $item.FullName
    } catch {
        throw ('Cannot verify the test profile item: ' + $Path + '. ' + $_.Exception.Message)
    }
    Initialize-ProfileMetadata
    if ([X4Lan.ProfileMetadata]::IsCloudTag($tag)) { return }
    throw ('Refusing to modify a linked or unsupported test profile item (tag 0x{0:X8}): {1}' -f $tag, $Path)
}
function Assert-EmptyTestProfile([string]$Profile) {
    if ((Split-Path -Leaf $Profile) -ne $ProfileId) { throw 'Unexpected test profile path.' }
    Assert-SafeProfileItem $Profile
    $saves = Join-Path $Profile 'save'
    if (Test-Path -LiteralPath $saves) {
        if (Get-ChildItem -LiteralPath $saves -Filter '*.xml*' -File) { throw 'The test profile contains saves. Back them up and use an empty test profile. Nothing will be deleted.' }
    }
}
function Set-TestGraphics([xml]$Config) {
    # X4 9.00 low preset plus explicit test display settings; no main profile edits.
    $settings = [ordered]@{
        res_width='1920'; res_height='1080'; fullscreen='true'; borderless='false'
        gfxquality='1'; shaderquality='0'; shadows='1'; softshadows='false'
        ssao='0'; glow='0'; uiglow='0'; uiglowintensity='0.75'
        lodfactor='0.15'; effectdistance='0.15'; distortion='false'
        disableradar='false'; radarquality='0'; ssr='false'; ssrquality='low'
        texturequality='low'; chromaticaberration='false'; volumetric='0'
        pom='low'; envmapprobes='none'; frameratelimit='true'; frameratetarget='60'
        upmode='none'; dlss='false'; dlssmode='off'; fsr3g='off'
    }
    $changed = $false
    foreach ($key in $settings.Keys) {
        $node = $Config.DocumentElement.SelectSingleNode($key)
        if ($null -eq $node) { $node = $Config.CreateElement($key); $Config.DocumentElement.AppendChild($node) | Out-Null }
        if ($node.InnerText -ne $settings[$key]) { $node.InnerText = $settings[$key]; $changed = $true }
    }
    return $changed
}
function Install-Package {
    Assert-Closed
    $pkg = Get-Content -Raw -LiteralPath (Join-Path $PackageRoot 'package.json') | ConvertFrom-Json
    $steam = Get-SteamRoot
    $game = $GamePath
    if (!$game) { $game = Find-Game $steam }
    $game = (Resolve-Path -LiteralPath $game).Path
    Assert-Game $game $pkg.steam_build
    $address = $ServerIP
    if (!$address) { $address = Read-Host 'Host IP (LAN or ZeroTier; shown in the server window)' }
    $localHost = $pkg.PSObject.Properties['channel'] -and $pkg.channel -eq 'public' -and $pkg.slot -eq 'p1'
    $address = Assert-IPv4 $address.Trim() ([bool]$localHost)
    $documents = [Environment]::GetFolderPath('MyDocuments')
    if (!$documents) { throw 'The Windows Documents folder could not be located.' }
    $profile = Join-Path $documents ('Egosoft\X4\' + $ProfileId)
    Assert-EmptyTestProfile $profile
    $target = Join-Path $game 'extensions\x4_lan_lab'
    if (Test-Path -LiteralPath $target) { throw ('The mod folder already exists. Remove it with the original package first: ' + $target) }
    $configPath = Join-Path $profile 'config.xml'
    if (Test-Path -LiteralPath $configPath) {
        Assert-SafeProfileItem $configPath
        [xml]$config = Get-Content -Raw -LiteralPath $configPath
    } else { [xml]$config = '<root />' }
    if ($config.DocumentElement.Name -ne 'root') { throw 'Unknown config.xml format.' }
    $backup = Join-Path $PackageRoot ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    if (Test-Path -LiteralPath $configPath) { Copy-Item -LiteralPath $configPath -Destination (Join-Path $backup 'config.xml') }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $PackageRoot 'extension\x4_lan_lab') -Destination $target -Recurse
    try {
        $lua = Join-Path $target 'ui\config.lua'
        $text = (Get-Content -Raw -LiteralPath $lua).Replace('SET_HOST_IP_DURING_INSTALL', $address)
        [IO.File]::WriteAllText($lua, $text, $Utf8)
        New-Item -ItemType Directory -Force -Path $profile | Out-Null
        foreach ($key in @('autosave','uisafemode')) {
            $node = $config.DocumentElement.SelectSingleNode($key)
            if ($null -eq $node) { $node = $config.CreateElement($key); $config.DocumentElement.AppendChild($node) | Out-Null }
            $node.InnerText = 'false'
        }
        Set-TestGraphics $config | Out-Null
        $tempConfig = Join-Path $profile 'config.x4lan.tmp'
        Assert-SafeProfileItem $tempConfig
        $config.Save($tempConfig)
        Move-Item -LiteralPath $tempConfig -Destination $configPath -Force
        [IO.File]::WriteAllText((Join-Path $target '.x4lan-package-id'), $pkg.package_id, $Utf8)
        $state = @{ game=$game; profile=$profile; steam=(Join-Path $steam 'steam.exe'); server=$address; package_id=$pkg.package_id; version=(Get-PackageVersion) }
        [IO.File]::WriteAllText((Join-Path $PackageRoot 'installation.json'), ($state | ConvertTo-Json), $Utf8)
    } catch {
        Remove-Item -LiteralPath $target -Recurse -Force # Only the new directory just copied.
        throw
    }
    Write-Host 'Installed. Existing player profiles were not modified.'
    Write-Host ('Test profile: ' + $profile)
    Write-Host 'Next: 2-TEST-CONNECTION.cmd, then 3-START-X4-TEST.cmd.'
}
function Start-TestGame {
    Assert-Closed
    $state = Read-State
    $pkg = Get-Content -Raw -LiteralPath (Join-Path $PackageRoot 'package.json') | ConvertFrom-Json
    Assert-Game $state.game $pkg.steam_build
    Assert-EmptyTestProfile $state.profile
    $configPath = Join-Path $state.profile 'config.xml'
    Assert-SafeProfileItem $configPath
    [xml]$config = Get-Content -Raw -LiteralPath $configPath
    if ($config.DocumentElement.Name -ne 'root') { throw 'Unknown config.xml format.' }
    if ($config.root.autosave -ne 'false' -or $config.root.uisafemode -ne 'false') { throw 'The test profile must keep autosave=false and uisafemode=false. See INSTALL.html.' }
    if (Set-TestGraphics $config) {
        $backup = Join-Path $PackageRoot ('backups\graphics-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Copy-Item -LiteralPath $configPath -Destination (Join-Path $backup 'config.xml')
        $tempConfig = Join-Path $state.profile 'config.x4lan.tmp'
        Assert-SafeProfileItem $tempConfig
        $config.Save($tempConfig)
        Move-Item -LiteralPath $tempConfig -Destination $configPath -Force
    }
    Write-Host 'Test graphics: 1920x1080 exclusive fullscreen, Low preset, 60 FPS limit.'
    $log = Join-Path $state.profile 'x4lan-debug.txt'
    if (Test-Path -LiteralPath $log) {
        $logs = Join-Path $PackageRoot 'logs'; New-Item -ItemType Directory -Force -Path $logs | Out-Null
        Copy-Item -LiteralPath $log -Destination (Join-Path $logs ('x4-previous-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt'))
    }
    Start-Process -FilePath $state.steam -ArgumentList @('-applaunch','392160','-personalfolderid',$ProfileId,'-module','custom_creative','-skipintro','-debug','scripts','-logfile','x4lan-debug.txt','-nocputhrottle','-noworkshopsync')
    Write-Host 'Steam is starting a new creative sandbox. Do not load an existing game.'
}
function Test-ConnectionToRelay {
    $state = Read-State
    $cfg = Get-Content -Raw -LiteralPath (Join-Path $PackageRoot 'session.json') | ConvertFrom-Json
    $nonce = [Guid]::NewGuid().ToString('N')
    $udp = New-Object System.Net.Sockets.UdpClient
    $udp.Client.ReceiveTimeout = 2500
    try {
        $udp.Connect($state.server, [int]$cfg.port)
        $message = [Text.Encoding]::ASCII.GetBytes(('PING|1|' + $cfg.token + '|' + $nonce))
        $ok = $false
        for ($i=0; $i -lt 3; $i++) {
            $udp.Send($message,$message.Length) | Out-Null
            try {
                $sender = New-Object System.Net.IPEndPoint([Net.IPAddress]::Any,0)
                $reply = [Text.Encoding]::ASCII.GetString($udp.Receive([ref]$sender))
                if ($reply -eq ('PONG|1|' + $nonce + '|' + $cfg.build + '|' + $cfg.bubble)) { $ok=$true; break }
            } catch [System.Net.Sockets.SocketException] { }
        }
        if (!$ok) { throw 'No matching server reply. Check the server window, host LAN/ZeroTier IP, matching package pair and Linux firewall. See INSTALL.html.' }
        Write-Host 'CONNECTION OK. The correct server and matching package replied. This check does not occupy a player slot.'
    } finally { $udp.Dispose() }
}
function Write-Diagnose {
    $state = Read-State
    $logs = Join-Path $PackageRoot 'logs'; New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $source = Join-Path $state.profile 'x4lan-debug.txt'
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $logs 'x4-debug.txt') -Force
        $lines = @(Get-Content -LiteralPath $source | Where-Object { $_ -match '\[X4LAN\]' })
        [IO.File]::WriteAllLines((Join-Path $logs 'x4-mod-diagnostics.txt'), [string[]]$lines, $Utf8)
        $lines | Select-Object -Last 12 | ForEach-Object { Write-Host $_ }
    } else { Write-Host 'No game log found yet.' }
    Write-Host ('Diagnostics folder: ' + $logs)
}
function Remove-Package {
    Assert-Closed
    $state = Read-State
    $target = Join-Path $state.game 'extensions\x4_lan_lab'
    $marker = Join-Path $target '.x4lan-package-id'
    if (!(Test-Path -LiteralPath $marker) -or (Get-Content -Raw -LiteralPath $marker) -ne $state.package_id) { throw 'The installed mod cannot be matched to this package. No files were removed.' }
    $destination = Join-Path $PackageRoot ('backups\removed-mod-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Move-Item -LiteralPath $target -Destination $destination
    Write-Host 'Test mod removed from X4 and archived in the package folder. The test profile was retained; no saves were deleted.'
}
try {
    Write-Host ('X4 LAN Lab ' + (Get-PackageVersion))
    switch ($Action) {
        'Version' { }
        'Install' { Install-Package }
        'Start' { Start-TestGame }
        'Test' { Test-ConnectionToRelay }
        'Diagnose' { Write-Diagnose }
        'Remove' { Remove-Package }
    }
    exit 0
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
