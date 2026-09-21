<#
.SYNOPSIS
    Starts an installed application by name, or reports that it is not installed.

.DESCRIPTION
    Built for automation. Exactly one line of JSON goes to stdout; everything
    else goes to stderr, so a caller can read stdout raw without filtering.

    The requested name is never treated as a command. It is matched against an
    inventory of what is actually installed - Start Menu entries, registry App
    Paths, and executables on PATH - and only a resolved inventory entry is ever
    launched. So a name this script does not recognise cannot execute anything;
    it reports "not installed" instead.

    Matching prefers an exact name, then a prefix, then a substring. When
    several entries tie, the shortest name wins, on the grounds that "Blender"
    is more likely what was meant than "Blender Nightly (Debug)".

.PARAMETER Name
    The application to start, as a person would say it: "blender", "vs code",
    "notepad". Case-insensitive.

.PARAMETER Check
    Resolve and report only; do not start anything. Useful for asking "is this
    installed?" without side effects.

.EXAMPLE
    powershell -NoProfile -File .\launch-app.ps1 -Name notepad
    {"requested":"notepad","matched":"Notepad","launched":true,...}

.NOTES
    Exit codes
      0 - the app was found (and started, unless -Check)
      5 - no installed app matched the name
      6 - the app was found but could not be started
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateLength(1, 60)]
    [string] $Name,

    [switch] $Check
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# How many near-misses to report back. Enough for a caller to say "did you mean
# one of these", not so many that the payload balloons.
$MAX_CANDIDATES = 5

# On a miss the whole inventory is returned for a caller to resolve against.
# Capped so a machine with thousands of executables on PATH cannot produce an
# unbounded payload.
$MAX_INVENTORY_REPORTED = 400

# --------------------------------------------------------------- inventory ----

function Get-StartMenuApps {
    <# Covers Store apps and most Start Menu entries, with launchable AppIDs. #>
    try {
        return @(Get-StartApps | ForEach-Object {
            [pscustomobject]@{
                name   = [string]$_.Name
                target = "shell:AppsFolder\$($_.AppID)"
                source = 'start-menu'
            }
        })
    } catch {
        [Console]::Error.WriteLine("Get-StartApps unavailable: $($_.Exception.Message)")
        return @()
    }
}

function Get-AppPathsApps {
    <# Registered executables: what the Run dialog resolves. #>
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'
    )

    $apps = @()
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($key in (Get-ChildItem $root -ErrorAction SilentlyContinue)) {
            $exe = Split-Path $key.PSChildName -Leaf
            $apps += [pscustomobject]@{
                name   = [System.IO.Path]::GetFileNameWithoutExtension($exe)
                target = $key.PSChildName
                source = 'app-paths'
            }
        }
    }
    return $apps
}

function Get-PathApps {
    <# Console tools and system binaries: notepad, ping, and friends live here
       rather than in the Start Menu. #>
    $apps = @()
    foreach ($dir in ($env:PATH -split ';')) {
        if ([string]::IsNullOrWhiteSpace($dir) -or -not (Test-Path $dir)) { continue }
        try {
            foreach ($exe in (Get-ChildItem -Path $dir -Filter '*.exe' -File -ErrorAction SilentlyContinue)) {
                $apps += [pscustomobject]@{
                    name   = [System.IO.Path]::GetFileNameWithoutExtension($exe.Name)
                    target = $exe.FullName
                    source = 'path'
                }
            }
        } catch { continue }
    }
    return $apps
}

function Get-InventoryApps {
    $apps = @()
    # Order matters: the first entry for a name wins, and Start Menu names are
    # friendlier than bare executable names.
    $apps += Get-StartMenuApps
    $apps += Get-AppPathsApps
    $apps += Get-PathApps

    # De-duplicate on name, keeping the first (Start Menu entries come first and
    # carry friendlier names than bare executables).
    $seen = @{}
    $unique = @()
    foreach ($app in $apps) {
        if ([string]::IsNullOrWhiteSpace($app.name)) { continue }
        $key = $app.name.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $unique += $app
    }
    return $unique
}

# ---------------------------------------------------------------- matching ----

function Select-App {
    param($Apps, [string] $Query)

    $needle = $Query.Trim().ToLowerInvariant()

    # Also try the name with spaces removed, so "vs code" reaches "VS Code" and
    # "VSCode" alike.
    $squashed = ($needle -replace '\s+', '')

    $exact = @($Apps | Where-Object {
        $n = $_.name.ToLowerInvariant()
        $n -eq $needle -or ($n -replace '\s+', '') -eq $squashed
    })
    if ($exact.Count -gt 0) {
        return ($exact | Sort-Object { $_.name.Length } | Select-Object -First 1)
    }

    $prefix = @($Apps | Where-Object { $_.name.ToLowerInvariant().StartsWith($needle) })
    if ($prefix.Count -gt 0) {
        return ($prefix | Sort-Object { $_.name.Length } | Select-Object -First 1)
    }

    $contains = @($Apps | Where-Object { $_.name.ToLowerInvariant().Contains($needle) })
    if ($contains.Count -gt 0) {
        return ($contains | Sort-Object { $_.name.Length } | Select-Object -First 1)
    }

    return $null
}

function Get-Candidates {
    param($Apps, [string] $Query)

    # Shown only when nothing matched, to help a caller suggest alternatives.
    $needle = $Query.Trim().ToLowerInvariant()
    $head   = if ($needle.Length -ge 3) { $needle.Substring(0, 3) } else { $needle }

    $names = $Apps |
        Where-Object { $_.name.ToLowerInvariant().Contains($head) } |
        Sort-Object { $_.name.Length } |
        Select-Object -First $MAX_CANDIDATES |
        ForEach-Object { [string]$_.name }

    # Cast, because ConvertTo-Json renders a bare empty array as {} and unwraps
    # a single element into a scalar. Both break a caller expecting a list.
    return [string[]]@($names)
}

# ------------------------------------------------------------------- main -----

$inventory = Get-InventoryApps
$match     = Select-App -Apps $inventory -Query $Name

if ($null -eq $match) {
    # The full list rides along so a caller can resolve names this script's
    # literal matching cannot - "vs code" is never a substring of "Visual
    # Studio Code", and no amount of string matching fixes that.
    $allNames = [string[]]@(
        $inventory | Sort-Object { $_.name } | Select-Object -First $MAX_INVENTORY_REPORTED |
            ForEach-Object { [string]$_.name }
    )

    $payload = [pscustomobject][ordered]@{
        requested     = $Name
        matched       = $null
        installed     = $false
        launched      = $false
        candidates    = (Get-Candidates -Apps $inventory -Query $Name)
        inventorySize = $inventory.Count
        inventory     = $allNames
    }
    [Console]::Out.WriteLine(($payload | ConvertTo-Json -Compress -Depth 4))
    exit 5
}

$launched = $false
$failure  = $null

if (-not $Check) {
    try {
        # The target came from the inventory, never from the caller's string.
        Start-Process -FilePath $match.target | Out-Null
        $launched = $true
    } catch {
        $failure = $_.Exception.Message
        [Console]::Error.WriteLine("Could not start '$($match.name)': $failure")
    }
}

$payload = [pscustomobject][ordered]@{
    requested = $Name
    matched   = $match.name
    installed = $true
    launched  = $launched
    source    = $match.source
    checkOnly = [bool]$Check
    failure   = $failure
}

[Console]::Out.WriteLine(($payload | ConvertTo-Json -Compress -Depth 4))

if ($failure) { exit 6 }
exit 0
