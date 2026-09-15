<#
.SYNOPSIS
    Prints the currently focused application as a single line on stdout, then exits.

.DESCRIPTION
    Built for automation. Exactly one line goes to stdout on success; everything
    else (warnings, failures) goes to stderr, so a caller can read stdout raw
    without filtering. Exit code is 0 on success, 1 if no foreground window
    could be determined, 2 if the script is not running in an interactive
    desktop session.

.PARAMETER Format
    name  - just the application name (default)
    line  - tab-separated: timestamp, app, pid, idleSeconds, title
    json  - single-line JSON object
    title - just the window title

.PARAMETER IdleThresholdSeconds
    No keyboard/mouse input for this long reports "[idle]" instead of the
    visible app. Default 120. Set to 0 to always report the window.

.PARAMETER Watch
    Instead of exiting, emit one line per interval forever. Each line is flushed
    immediately, so a parent process can stream it.

.PARAMETER IntervalSeconds
    Interval for -Watch. Default 5.

.EXAMPLE
    powershell -NoProfile -File .\Get-ActiveApp.ps1
    Code

.EXAMPLE
    powershell -NoProfile -File .\Get-ActiveApp.ps1 -Format json
    {"timestamp":"2026-09-14T10:22:03","app":"Code","pid":18244,"idleSeconds":3,"title":"Get-ActiveApp.ps1"}
#>

[CmdletBinding()]
param(
    [ValidateSet('name','line','json','title')]
    [string] $Format = 'name',
    [int]    $IdleThresholdSeconds = 120,
    [switch] $Watch,
    [int]    $IntervalSeconds = 5
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # keep progress bars off the pipe

# ------------------------------------------------------------ win32 api ------

if (-not ('Win32Activity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class Win32Activity
{
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO
    {
        public uint cbSize;
        public uint dwTime;
    }

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool GetLastInputInfo(ref LASTINPUTINFO info);

    public static string GetWindowTitle(IntPtr hWnd)
    {
        StringBuilder sb = new StringBuilder(1024);
        int len = GetWindowTextW(hWnd, sb, sb.Capacity);
        return len > 0 ? sb.ToString() : string.Empty;
    }
}
'@ | Out-Null
}

# --------------------------------------------------------------- helpers -----

function Get-IdleSeconds {
    $info = New-Object Win32Activity+LASTINPUTINFO
    $info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)
    if (-not [Win32Activity]::GetLastInputInfo([ref]$info)) { return 0 }

    # Both counters are 32-bit and wrap roughly every 49.7 days.
    $now  = [uint32]([Environment]::TickCount -band 0xFFFFFFFF)
    $diff = if ($now -ge $info.dwTime) { $now - $info.dwTime }
            else { ([uint32]::MaxValue - $info.dwTime) + $now }
    return [int][math]::Round($diff / 1000)
}

function Get-Snapshot {
    $idle = if ($IdleThresholdSeconds -gt 0) { Get-IdleSeconds } else { 0 }

    $hWnd = [Win32Activity]::GetForegroundWindow()
    if ($hWnd -eq [IntPtr]::Zero) {
        return [pscustomobject]@{
            timestamp   = (Get-Date).ToString('s')
            app         = '[none]'
            pid         = 0
            idleSeconds = $idle
            title       = ''
            ok          = $false
        }
    }

    $title = [Win32Activity]::GetWindowTitle($hWnd)

    $procId = [uint32]0
    [void][Win32Activity]::GetWindowThreadProcessId($hWnd, [ref]$procId)

    $app = '[unknown]'
    if ($procId -ne 0) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            $app = $proc.ProcessName

            # Packaged/UWP apps all run under ApplicationFrameHost, so the
            # process name says nothing useful; the title is the identifier.
            if ($app -eq 'ApplicationFrameHost' -and $title) { $app = $title }

            # Friendly name from the binary, when the process is readable.
            try {
                $desc = $proc.MainModule.FileVersionInfo.FileDescription
                if ($desc) { $app = $desc }
            } catch { }
        }
    }

    if ($IdleThresholdSeconds -gt 0 -and $idle -ge $IdleThresholdSeconds) {
        $app   = '[idle]'
        $title = ''
    }

    return [pscustomobject]@{
        timestamp   = (Get-Date).ToString('s')
        app         = $app
        pid         = [int]$procId
        idleSeconds = $idle
        title       = $title
        ok          = $true
    }
}

function Format-Snapshot {
    param($Snapshot)

    switch ($Format) {
        'name'  { return $Snapshot.app }
        'title' { return $Snapshot.title }
        'line'  {
            # Tab-separated. Tabs and newlines are stripped from the title so a
            # record is always exactly one line with a fixed field count.
            $clean = ($Snapshot.title -replace "[`t`r`n]", ' ')
            return ($Snapshot.timestamp, $Snapshot.app, $Snapshot.pid,
                    $Snapshot.idleSeconds, $clean) -join "`t"
        }
        'json'  {
            $obj = $Snapshot | Select-Object timestamp, app, pid, idleSeconds, title
            return ($obj | ConvertTo-Json -Compress -Depth 2)
        }
    }
}

# ------------------------------------------------------------------ main -----

# GetForegroundWindow only means anything inside an interactive desktop session.
# Scheduled tasks configured to run when the user is *not* logged on land in a
# session with no desktop, and would otherwise silently report "[none]" forever.
if ([Environment]::UserInteractive -eq $false) {
    [Console]::Error.WriteLine('Not running in an interactive session; no foreground window exists.')
    exit 2
}

if ($Watch) {
    while ($true) {
        $snap = Get-Snapshot
        [Console]::Out.WriteLine((Format-Snapshot $snap))
        [Console]::Out.Flush()
        Start-Sleep -Seconds $IntervalSeconds
    }
}

$snap = Get-Snapshot
[Console]::Out.WriteLine((Format-Snapshot $snap))

if (-not $snap.ok) { exit 1 }
exit 0