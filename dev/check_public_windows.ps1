param([Parameter(Mandatory=$true)][string]$PackagePath)
# Actual packaged functions against temporary files; Windows environment doubles.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$PackagePath = (Resolve-Path -LiteralPath $PackagePath).Path
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PackagePath 'setup_windows.ps1'), [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('x4-public-windows-' + [Guid]::NewGuid().ToString('N'))
$documents = Join-Path $fixture 'Documents'
$ProfileId = '45842'
$Utf8 = New-Object Text.UTF8Encoding($false)
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        $text = $statement.Extent.Text
        $text = $text.Replace("[Environment]::GetFolderPath('MyDocuments')", ("'" + $documents.Replace("'","''") + "'"))
        Invoke-Expression $text
    }
}
function Check($condition, $message) { if (!$condition) { throw $message } }
function Must-Fail([scriptblock]$run, [string]$pattern) {
    try { & $run } catch { if ($_.Exception.Message -notlike $pattern) { throw }; return }
    throw ('Expected rejection: ' + $pattern)
}
function Get-SteamRoot { return (Join-Path $fixture 'Steam') }
function Assert-Closed {}
function Assert-Game([string]$Path, [string]$Build) { Check ($Build -eq '23660954') 'Wrong game build' }
function Start-Process($FilePath, $ArgumentList) { $script:launched = $ArgumentList }
try {
    Check ((Get-Content -Raw -LiteralPath (Join-Path $PackagePath 'setup_windows.ps1')).Contains('$ProfileId = ''45842''')) 'Packaged public profile missing'
    Check ((Assert-IPv4 '127.0.0.1' $true) -eq '127.0.0.1') 'Host loopback refused'
    Must-Fail { Assert-IPv4 '127.0.0.1' } '*not localhost*'
    Must-Fail { Assert-IPv4 '0.0.0.0' $true } '*not localhost*'
    $normal = Join-Path $documents 'Egosoft/X4/12345678/save/normal.xml.gz'
    New-Item -ItemType Directory -Path (Split-Path -Parent $normal) -Force | Out-Null
    [IO.File]::WriteAllText($normal,'normal save unchanged',$Utf8)
    $before = (Get-Item -LiteralPath $normal).LastWriteTimeUtc.Ticks
    foreach ($role in @('p1','p2')) {
        $PackageRoot = Join-Path $fixture ('package with spaces-' + $role)
        $GamePath = Join-Path $fixture ('Game-' + $role)
        New-Item -ItemType Directory -Path $GamePath -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $PackageRoot 'extension/x4_lan_lab/ui') -Force | Out-Null
        $ServerIP = if ($role -eq 'p1') { '127.0.0.1' } else { '192.168.1.25' }
        $metadata = @{channel='public';slot=$role;version='6.0.1';package_id=('public-fixture-'+$role);steam_build='23660954'}
        $metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PackageRoot 'package.json')
        [IO.File]::WriteAllText((Join-Path $PackageRoot 'VERSION.txt'),'6.0.1',$Utf8)
        [IO.File]::WriteAllText((Join-Path $PackageRoot 'extension/x4_lan_lab/ui/config.lua'),('host = "'+$ServerIP+'"'),$Utf8)
        Install-Package
        $state = Read-State
        Check ($state.profile.EndsWith('45842')) 'Wrong public profile'
        Check ($state.server -eq $ServerIP) 'Wrong server address'
        [xml]$config = Get-Content -Raw -LiteralPath (Join-Path $state.profile 'config.xml')
        Check ($config.root.autosave -eq 'false' -and $config.root.res_width -eq '1920') 'Unsafe/wrong settings'
        Start-TestGame
        Check ($script:launched -contains '45842') 'Wrong launch profile'
        $marker = Join-Path $GamePath 'extensions/x4_lan_lab/.x4lan-package-id'
        [IO.File]::WriteAllText($marker,'foreign-owner',$Utf8)
        Must-Fail { Remove-Package } '*cannot be matched*'
        [IO.File]::WriteAllText($marker,$metadata.package_id,$Utf8)
        Remove-Package
        Check (!(Test-Path -LiteralPath (Split-Path -Parent $marker))) 'Owned mod was not archived'
        $testSave = Join-Path $state.profile 'save/existing.xml.gz'
        New-Item -ItemType Directory -Path (Split-Path -Parent $testSave) -Force | Out-Null
        [IO.File]::WriteAllText($testSave,'retain existing public save',$Utf8)
        Must-Fail { Install-Package } '*test profile contains saves*'
        Check ([IO.File]::ReadAllText($testSave) -eq 'retain existing public save') 'Public save changed'
        Remove-Item -LiteralPath $testSave # This test-created fixture only.
    }
    Check ([IO.File]::ReadAllText($normal) -eq 'normal save unchanged') 'Normal fixture save changed'
    Check ((Get-Item -LiteralPath $normal).LastWriteTimeUtc.Ticks -eq $before) 'Normal fixture save timestamp changed'
    Write-Host 'PASS: packaged Windows HOST/GUEST install, host loopback, profile 45842, fresh-world launch, ownership removal, saved-profile refusal and normal-save preservation.'
    Write-Host 'PowerShell on Linux with Windows environment doubles; not native Windows/OneDrive QA.'
} finally {
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
