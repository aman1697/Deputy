<#
.SYNOPSIS
    Installs a winget package by ID, headlessly. Prints one line of JSON on stdout.

.DESCRIPTION
    Built for automation. Exactly one line of JSON goes to stdout; winget's own
    chatter goes to stderr, so a caller can read stdout raw.

    This script takes a package ID, never a command. The ID is pattern-checked
    and then verified to exist with `winget show` before anything is installed,
    so a made-up or mistyped ID fails at the verify step instead of installing
    whatever happened to match. The install command itself is fixed here and
    cannot be influenced by the caller beyond that one ID.

    User scope is tried first, because it does not need elevation. If winget
    reports the package cannot be installed per-user, the machine-scope install
    is attempted, which may prompt for elevation.

.PARAMETER PackageId
    The winget package identifier, e.g. Microsoft.VisualStudioCode.

.PARAMETER Verify
    Check that the package exists and exit without installing.

.EXAMPLE
    powershell -NoProfile -File .\install-app.ps1 -PackageId Microsoft.VisualStudioCode
    {"packageId":"Microsoft.VisualStudioCode","verified":true,"installed":true,...}

.NOTES
    Exit codes
      0 - installed (or, with -Verify, the package exists)
      7 - winget is not available on this machine
      8 - no package with that ID exists
      9 - the install ran and failed
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateLength(1, 100)]
    # Mirrors the caller-side check. Anything with a space, quote, semicolon or
    # pipe in it is not a package ID and is refused before winget sees it.
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+-]*$')]
    [string] $PackageId,

    [switch] $Verify
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Write-Payload {
    param([hashtable] $Fields)
    [Console]::Out.WriteLine(([pscustomobject]$Fields | ConvertTo-Json -Compress -Depth 4))
}

function Get-Tail {
    <# winget is verbose; keep just enough to explain a failure. #>
    param([string[]] $Lines, [int] $Keep = 6)

    $clean = @($Lines | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
    if ($clean.Count -le $Keep) { return ($clean -join ' | ') }
    return (($clean | Select-Object -Last $Keep) -join ' | ')
}

# ------------------------------------------------------------------ main -----

$winget = Get-Command winget -ErrorAction SilentlyContinue
if (-not $winget) {
    [Console]::Error.WriteLine('winget is not available on this machine.')
    Write-Payload @{
        packageId = $PackageId
        verified  = $false
        installed = $false
        reason    = 'winget_not_available'
    }
    exit 7
}

# --- verify the package exists before installing anything --------------------

$showOutput = & winget show --id $PackageId --exact --accept-source-agreements 2>&1
$showExit   = $LASTEXITCODE

if ($showExit -ne 0) {
    [Console]::Error.WriteLine("No winget package with ID '$PackageId'.")
    Write-Payload @{
        packageId = $PackageId
        verified  = $false
        installed = $false
        reason    = 'package_not_found'
        detail    = (Get-Tail $showOutput)
    }
    exit 8
}

if ($Verify) {
    Write-Payload @{ packageId = $PackageId; verified = $true; installed = $false }
    exit 0
}

# --- install -----------------------------------------------------------------

$common = @(
    'install', '--id', $PackageId, '--exact',
    '--silent',
    '--accept-package-agreements',
    '--accept-source-agreements',
    '--disable-interactivity'
)

# User scope first: it does not need elevation, so it works without a UAC
# prompt nobody is present to click.
$installOutput = & winget @common --scope user 2>&1
$installExit   = $LASTEXITCODE
$scope         = 'user'

if ($installExit -ne 0) {
    [Console]::Error.WriteLine('User-scope install failed; retrying at machine scope.')
    $installOutput = & winget @common 2>&1
    $installExit   = $LASTEXITCODE
    $scope         = 'machine'
}

if ($installExit -ne 0) {
    [Console]::Error.WriteLine("winget install failed with exit code $installExit.")
    Write-Payload @{
        packageId = $PackageId
        verified  = $true
        installed = $false
        reason    = 'install_failed'
        exitCode  = $installExit
        detail    = (Get-Tail $installOutput)
    }
    exit 9
}

Write-Payload @{
    packageId = $PackageId
    verified  = $true
    installed = $true
    scope     = $scope
}
exit 0
