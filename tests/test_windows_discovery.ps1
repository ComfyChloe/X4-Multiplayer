# Run with Windows PowerShell 5.1 or PowerShell 7; temporary fixtures only.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$sourceRoot = Split-Path -Parent $PSScriptRoot
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $sourceRoot 'distribution/windows_setup.ps1'), [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) { Invoke-Expression $statement.Extent.Text }
}
function Check($Condition, [string]$Message) { if (!$Condition) { throw $Message } }
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('x4lan-discovery-' + [Guid]::NewGuid().ToString('N'))
$priorProgramFiles = $env:ProgramFiles
$priorProgramFiles86 = ${env:ProgramFiles(x86)}
try {
    $steam = Join-Path $fixture 'Steam with spaces'
    $game = Join-Path $steam 'steamapps/common/X4 Foundations'
    New-Item -ItemType Directory -Path $game -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $steam 'steam.exe'), [byte[]]@())
    [IO.File]::WriteAllBytes((Join-Path $game 'X4.exe'), [byte[]]@())
    [IO.File]::WriteAllText((Join-Path $steam 'steamapps/appmanifest_392160.acf'), '"installdir" "X4 Foundations"')
    $vdf = Join-Path $steam 'steamapps/libraryfolders.vdf'
    [IO.File]::WriteAllText($vdf, '"path" "X4MissingDrive789:\Gone"')
    function Read-Host { throw 'Unexpected prompt during successful discovery' }
    Check ((Find-Game $steam) -eq $game) 'Unavailable VDF drive prevented discovery of the valid library'
    # Stale registry candidate must also be skipped before provider path joining.
    function Get-ItemProperty { [pscustomobject]@{SteamPath='X4MissingDrive789:\OldSteam'} }
    $env:ProgramFiles = Join-Path $fixture 'Programs'
    ${env:ProgramFiles(x86)} = 'X4MissingDrive789:\Programs'
    $fallback = Join-Path $env:ProgramFiles 'Steam'
    New-Item -ItemType Directory -Path $fallback -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $fallback 'steam.exe'), [byte[]]@())
    Check ((Get-SteamRoot) -eq $fallback) 'ProgramFiles fallback after stale registry was not found'
    Remove-Item -LiteralPath (Join-Path $fallback 'steam.exe')
    function Read-Host { return ('"' + $steam + '"') }
    Check ((Get-SteamRoot) -eq $steam) 'Manual Steam fallback was not accepted'
    function Read-Host { return 'X4MissingDrive789:\None' }
    $refused = $false
    try { Get-SteamRoot | Out-Null } catch {
        Check ($_.Exception.Message -like '*Steam was not found in that folder*') 'Unexpected manual-path error'
        $refused = $true
    }
    Check $refused 'Missing manual Steam path was accepted'
    Write-Host 'PASS: unavailable VDF drive, stale registry, ProgramFiles fallback, manual Steam path and refusal.'
    Write-Host 'Filesystem/PowerShell fixtures only; no native Windows dialog or X4 acceptance.'
} finally {
    $env:ProgramFiles = $priorProgramFiles
    ${env:ProgramFiles(x86)} = $priorProgramFiles86
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
