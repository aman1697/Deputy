<#
.SYNOPSIS
    Prints upcoming Microsoft Teams / Outlook calendar events as JSON on stdout, then exits.

.DESCRIPTION
    Built for automation. Exactly one line of JSON goes to stdout on success;
    everything else (warnings, failures, sign-in instructions) goes to stderr,
    so a caller can read stdout raw without filtering.

    Reads Microsoft Graph, not the local Outlook cache. An earlier version of
    this script used Outlook COM, which cold-started a headless Outlook and read
    its .ost - on a machine where classic Outlook is not kept running that cache
    can be months out of date, and it fails silently rather than loudly. Graph
    returns Exchange Online's authoritative state, so there is no cache to go
    stale and no dependency on Outlook being installed at all.

    Authentication is the OAuth device-code flow against the documented
    "Microsoft Graph Command Line Tools" public client, so no app registration
    is required. Run once with -Login to consent; after that the refresh token
    is used silently. Tokens are cached under %LOCALAPPDATA%\Deputy, encrypted
    with DPAPI so only this Windows user on this machine can read them.

    Every event carries `startsInMinutes`, so a consumer can answer questions
    like "anything in the next three hours" without knowing the current time.

    Deliberately omitted from the output: event bodies, join URLs, and attendee
    names. This output is handed to a language model, so it carries the least
    that still answers the question. -IncludeAttendees opts back in.

.PARAMETER Window
    How far ahead to look, starting from now.
      next3h  - the next three hours
      today   - the rest of today, until local midnight
      next24h - the next 24 hours (default)
      week    - the next seven days

.PARAMETER Format
    json - single-line JSON object (default)
    text - one human-readable line per event

.PARAMETER MaxEvents
    Stop after this many events and set "truncated": true. Default 12, which
    keeps the JSON inside the consumer's input budget (see
    MAX_SPOKEN_INPUT_CHARS). Without this a busy week would be cut off
    mid-object downstream, silently dropping meetings from the answer.

.PARAMETER Login
    Run the interactive device-code sign-in and cache the tokens, then exit.
    Needed once before the script can read anything.

    Opens the verification page in the default browser and puts the code on the
    clipboard, so the only manual step is entering it. One JSON line is written
    to stdout as soon as the code is issued, and a second when sign-in
    finishes, so a caller can show the code without waiting for the whole flow.

.PARAMETER NoBrowser
    With -Login, do not open a browser or touch the clipboard. For headless use,
    where the printed code is all a caller can act on.

.PARAMETER Logout
    Delete the cached tokens and exit.

.PARAMETER IncludeCancelled
    Include cancelled meetings in `events`. They are excluded by default and
    only counted, so a consumer cannot announce a meeting that is not happening.

.PARAMETER IncludeAttendees
    Include attendee display names instead of just a count.

.EXAMPLE
    powershell -NoProfile -File .\get-calendar.ps1 -Login
    To sign in, open https://microsoft.com/devicelogin and enter code F7K2QXNM

.EXAMPLE
    powershell -NoProfile -File .\get-calendar.ps1 -Window next3h
    {"generatedAt":"2026-09-17T16:20:02+05:30","window":"next3h",...}

.EXAMPLE
    powershell -NoProfile -File .\get-calendar.ps1 -Window today -Format text
    21:30  in 45m   AI ML Standup - Internal  (Teams, 10 attendees)

.NOTES
    Exit codes
      0 - success, including when nothing is scheduled
      1 - the Graph request failed (network, throttling, HTTP error)
      2 - Graph returned something this script could not read
      4 - not signed in, or the refresh token no longer works; run -Login
#>

[CmdletBinding(DefaultParameterSetName = 'Read')]
param(
    [Parameter(ParameterSetName = 'Read')]
    [ValidateSet('next3h','today','next24h','week')]
    [string] $Window = 'next24h',

    [Parameter(ParameterSetName = 'Read')]
    [ValidateSet('json','text')]
    [string] $Format = 'json',

    [Parameter(ParameterSetName = 'Read')]
    [ValidateRange(1, 200)]
    [int]    $MaxEvents = 12,

    [Parameter(ParameterSetName = 'Read')]
    [switch] $IncludeCancelled,

    [Parameter(ParameterSetName = 'Read')]
    [switch] $IncludeAttendees,

    [Parameter(ParameterSetName = 'Login', Mandatory = $true)]
    [switch] $Login,

    [Parameter(ParameterSetName = 'Login')]
    [switch] $NoBrowser,

    [Parameter(ParameterSetName = 'Logout', Mandatory = $true)]
    [switch] $Logout
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # keep progress bars off the pipe

# ------------------------------------------------------------------- config ---

# The documented public client for script and CLI use. Overridable so a tenant
# that blocks it, or a team deployment that wants its own scopes, can register
# an app and point at that instead.
$CLIENT_ID = if ($env:DEPUTY_GRAPH_CLIENT_ID) { $env:DEPUTY_GRAPH_CLIENT_ID }
             else { '14d82eec-204b-4c2f-b7e8-296a70dab67e' }

# "organizations" is work/school accounts only; this is a corporate mailbox.
$TENANT = if ($env:DEPUTY_GRAPH_TENANT) { $env:DEPUTY_GRAPH_TENANT } else { 'organizations' }

# offline_access is what buys a refresh token; without it every run would need
# an interactive sign-in.
$SCOPES = 'https://graph.microsoft.com/Calendars.Read offline_access'

$AUTHORITY  = "https://login.microsoftonline.com/$TENANT/oauth2/v2.0"
$GRAPH_BASE = 'https://graph.microsoft.com/v1.0'

$CACHE_DIR  = Join-Path $env:LOCALAPPDATA 'Deputy'
$CACHE_FILE = Join-Path $CACHE_DIR 'graph-token.xml'

# Refresh a little early rather than racing the expiry mid-request.
$EXPIRY_SKEW_SECONDS = 120

# --------------------------------------------------------------- token cache --

function Protect-Secret {
    param([string] $Value)
    # DPAPI, user+machine scoped: the file is useless to another account.
    return (ConvertTo-SecureString -String $Value -AsPlainText -Force |
            ConvertFrom-SecureString)
}

function Unprotect-Secret {
    param([string] $Encrypted)
    $secure = ConvertTo-SecureString -String $Encrypted
    $bstr   = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Save-TokenCache {
    param([string] $AccessToken, [string] $RefreshToken, [int] $ExpiresIn)

    if (-not (Test-Path $CACHE_DIR)) {
        New-Item -ItemType Directory -Path $CACHE_DIR -Force | Out-Null
    }

    @{
        accessToken  = (Protect-Secret $AccessToken)
        refreshToken = (Protect-Secret $RefreshToken)
        expiresAt    = (Get-Date).AddSeconds($ExpiresIn).ToString('o')
        clientId     = $CLIENT_ID
        tenant       = $TENANT
    } | Export-Clixml -Path $CACHE_FILE -Force
}

function Read-TokenCache {
    if (-not (Test-Path $CACHE_FILE)) { return $null }
    try {
        return Import-Clixml -Path $CACHE_FILE
    } catch {
        [Console]::Error.WriteLine("Token cache is unreadable, ignoring it: $($_.Exception.Message)")
        return $null
    }
}

# ------------------------------------------------------------------- oauth ----

function Read-OAuthError {
    <# Non-2xx replies carry the useful part in the body, not the status line. #>
    param($ErrorRecord)

    $detail = $ErrorRecord.ErrorDetails.Message
    if (-not $detail) { return $null }
    try { return ($detail | ConvertFrom-Json) } catch { return $null }
}

function Write-LoginStage {
    <# One JSON line per stage, so a caller can react without parsing prose. #>
    param([hashtable] $Fields)

    [Console]::Out.WriteLine(([pscustomobject]$Fields | ConvertTo-Json -Compress -Depth 3))
    [Console]::Out.Flush()
}

function Open-VerificationPage {
    param([string] $Uri, [string] $UserCode)

    # Best-effort conveniences: a failure here costs the user a copy-paste, so
    # neither is worth failing the sign-in over.
    try {
        Set-Clipboard -Value $UserCode
        [Console]::Error.WriteLine('Code copied to clipboard.')
    } catch {
        [Console]::Error.WriteLine("Could not reach the clipboard: $($_.Exception.Message)")
    }

    try {
        Start-Process $Uri
        [Console]::Error.WriteLine("Opened $Uri in your browser.")
    } catch {
        [Console]::Error.WriteLine("Could not open a browser, go to $Uri manually: $($_.Exception.Message)")
    }
}

function Invoke-DeviceCodeLogin {
    $request = Invoke-RestMethod -Method Post -Uri "$AUTHORITY/devicecode" -Body @{
        client_id = $CLIENT_ID
        scope     = $SCOPES
    }

    # The human-facing instruction is a diagnostic, not data: stderr, so a
    # caller reading stdout still gets clean JSON as promised.
    [Console]::Error.WriteLine($request.message)

    if (-not $NoBrowser) {
        Open-VerificationPage -Uri $request.verification_uri -UserCode $request.user_code
    }

    # Emitted before polling starts, so a caller can display the code straight
    # away rather than blocking for the length of the whole flow.
    Write-LoginStage @{
        stage           = 'pending'
        userCode        = [string]$request.user_code
        verificationUri = [string]$request.verification_uri
        expiresInSeconds = [int]$request.expires_in
        browserOpened   = (-not $NoBrowser)
    }

    $interval = [int]$request.interval
    if ($interval -lt 1) { $interval = 5 }
    $deadline = (Get-Date).AddSeconds([int]$request.expires_in)

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $interval

        # Deliberately not a switch with `continue`: inside a switch, `continue`
        # exits the switch rather than the enclosing loop, so execution would
        # fall through and try to cache a token that was never issued.
        $token = $null
        try {
            $token = Invoke-RestMethod -Method Post -Uri "$AUTHORITY/token" -Body @{
                grant_type  = 'urn:ietf:params:oauth:grant-type:device_code'
                client_id   = $CLIENT_ID
                device_code = $request.device_code
            }
        } catch {
            $oauth = Read-OAuthError $_
            if ($oauth.error -eq 'authorization_pending') {
                # not approved yet; keep waiting
            } elseif ($oauth.error -eq 'slow_down') {
                $interval += 5
            } else {
                $reason = if ($oauth) { "$($oauth.error): $($oauth.error_description)" }
                          else { $_.Exception.Message }
                [Console]::Error.WriteLine("Sign-in failed. $reason")
                Write-LoginStage @{ stage = 'failed'; reason = $reason }
                return $false
            }
        }

        if ($token -and $token.access_token) {
            Save-TokenCache -AccessToken $token.access_token `
                            -RefreshToken $token.refresh_token `
                            -ExpiresIn ([int]$token.expires_in)
            [Console]::Error.WriteLine('Signed in. Tokens cached; future runs will not prompt.')
            Write-LoginStage @{ stage = 'signed-in' }
            return $true
        }
    }

    [Console]::Error.WriteLine('Sign-in timed out before it was approved.')
    Write-LoginStage @{ stage = 'failed'; reason = 'timed out before approval' }
    return $false
}

function Get-AccessToken {
    <# Returns a token silently, or $null when an interactive -Login is needed. #>
    $cache = Read-TokenCache
    if (-not $cache) { return $null }

    if ($cache.clientId -ne $CLIENT_ID -or $cache.tenant -ne $TENANT) {
        [Console]::Error.WriteLine('Cached token was issued for a different client or tenant; sign in again.')
        return $null
    }

    $expiresAt = [datetime]::Parse($cache.expiresAt)
    if ($expiresAt.AddSeconds(-$EXPIRY_SKEW_SECONDS) -gt (Get-Date)) {
        return (Unprotect-Secret $cache.accessToken)
    }

    try {
        $token = Invoke-RestMethod -Method Post -Uri "$AUTHORITY/token" -Body @{
            grant_type    = 'refresh_token'
            client_id     = $CLIENT_ID
            refresh_token = (Unprotect-Secret $cache.refreshToken)
            scope         = $SCOPES
        }
    } catch {
        $oauth  = Read-OAuthError $_
        $reason = if ($oauth) { "$($oauth.error): $($oauth.error_description)" } else { $_.Exception.Message }
        [Console]::Error.WriteLine("Token refresh failed. $reason")
        return $null
    }

    # A refresh may or may not hand back a new refresh token; keep the old one
    # when it does not, or the next run would have nothing to refresh with.
    $refresh = if ($token.refresh_token) { $token.refresh_token }
               else { Unprotect-Secret $cache.refreshToken }

    Save-TokenCache -AccessToken $token.access_token `
                    -RefreshToken $refresh `
                    -ExpiresIn ([int]$token.expires_in)

    return $token.access_token
}

# --------------------------------------------------------------- helpers ------

function Get-WindowEnd {
    param([datetime] $From, [string] $Name)

    switch ($Name) {
        'next3h'  { return $From.AddHours(3) }
        'next24h' { return $From.AddHours(24) }
        'week'    { return $From.AddDays(7) }
        'today'   { return $From.Date.AddDays(1).AddSeconds(-1) }
    }
}

function Get-CalendarView {
    param([string] $AccessToken, [datetime] $From, [datetime] $To)

    # calendarView expands recurring series server-side, which is the whole
    # reason not to hand-roll recurrence handling.
    $query = @(
        "startDateTime=$($From.ToString('yyyy-MM-ddTHH:mm:ss'))"
        "endDateTime=$($To.ToString('yyyy-MM-ddTHH:mm:ss'))"
        '$select=subject,start,end,isCancelled,isAllDay,isOnlineMeeting,onlineMeetingProvider,organizer,attendees,type,responseStatus'
        '$orderby=start/dateTime'
        '$top=50'
    ) -join '&'

    # Ask for local wall-clock times so no timezone conversion is needed here.
    $headers = @{
        Authorization = "Bearer $AccessToken"
        Prefer        = 'outlook.timezone="' + (Get-TimeZone).Id + '"'
    }

    return Invoke-RestMethod -Method Get -Uri "$GRAPH_BASE/me/calendarView?$query" -Headers $headers
}

function Get-AttendeeNames {
    param($Event)

    if (-not $Event.attendees) { return @() }
    return @(
        $Event.attendees |
            ForEach-Object { $_.emailAddress.name } |
            Where-Object { $_ }
    )
}

function ConvertTo-EventRecord {
    param($Event, [datetime] $Now)

    $start = [datetime]::Parse($Event.start.dateTime)
    $end   = [datetime]::Parse($Event.end.dateTime)

    $record = [ordered]@{
        subject         = [string]$Event.subject
        start           = $start.ToString('yyyy-MM-ddTHH:mm:ss')
        end             = $end.ToString('yyyy-MM-ddTHH:mm:ss')
        startsInMinutes = [int][math]::Round(($start - $Now).TotalMinutes)
        durationMinutes = [int][math]::Round(($end - $start).TotalMinutes)
        inProgress      = ($start -le $Now -and $end -gt $Now)
        # Graph states this outright, rather than the location-string guessing
        # the COM version had to do.
        isTeamsMeeting  = ([bool]$Event.isOnlineMeeting -and
                           $Event.onlineMeetingProvider -eq 'teamsForBusiness')
        allDay          = [bool]$Event.isAllDay
        recurring       = ($Event.type -eq 'occurrence' -or $Event.type -eq 'exception')
        cancelled       = [bool]$Event.isCancelled
        response        = [string]$Event.responseStatus.response
        organizer       = [string]$Event.organizer.emailAddress.name
        attendeeCount   = (Get-AttendeeNames -Event $Event).Count
    }

    if ($IncludeAttendees) {
        $record.attendees = (Get-AttendeeNames -Event $Event)
    }

    if (-not $record.response) { $record.response = 'unknown' }

    return [pscustomobject]$record
}

function Format-Text {
    param($Payload)

    if ($Payload.events.Count -eq 0) {
        return "no events in window '$($Payload.window)'"
    }

    $lines = foreach ($event in $Payload.events) {
        $when = ([datetime]$event.start).ToString('HH:mm')
        $rel  = if ($event.inProgress) { 'now' }
                elseif ($event.startsInMinutes -lt 60) { "in $($event.startsInMinutes)m" }
                elseif ($event.startsInMinutes -lt 2880) { "in {0:N1}h" -f ($event.startsInMinutes / 60) }
                else { "in {0:N1}d" -f ($event.startsInMinutes / 1440) }
        $tags = @()
        if ($event.isTeamsMeeting) { $tags += 'Teams' }
        if ($event.cancelled)      { $tags += 'CANCELLED' }
        $tags += "$($event.attendeeCount) attendees"

        "{0}  {1,-8} {2}  ({3})" -f $when, $rel, $event.subject, ($tags -join ', ')
    }
    return ($lines -join [Environment]::NewLine)
}

# ------------------------------------------------------------------ main ------

if ($Logout) {
    if (Test-Path $CACHE_FILE) {
        Remove-Item -Path $CACHE_FILE -Force
        [Console]::Error.WriteLine('Cached tokens deleted.')
    } else {
        [Console]::Error.WriteLine('No cached tokens to delete.')
    }
    exit 0
}

if ($Login) {
    if (Invoke-DeviceCodeLogin) { exit 0 } else { exit 4 }
}

$token = Get-AccessToken
if (-not $token) {
    [Console]::Error.WriteLine(
        'Not signed in to Microsoft Graph. Run this script once with -Login to consent.')
    exit 4
}

$now       = Get-Date
$windowEnd = Get-WindowEnd -From $now -Name $Window

try {
    $response = Get-CalendarView -AccessToken $token -From $now -To $windowEnd
} catch {
    $detail = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
    [Console]::Error.WriteLine("Graph request failed: $detail")
    exit 1
}

if ($null -eq $response.PSObject.Properties['value']) {
    [Console]::Error.WriteLine('Graph response had no "value" collection.')
    exit 2
}

$events         = @()
$cancelledCount = 0
$truncated      = $false

foreach ($item in @($response.value)) {
    if ([bool]$item.isCancelled) {
        $cancelledCount++
        if (-not $IncludeCancelled) { continue }
    }

    if ($events.Count -ge $MaxEvents) {
        $truncated = $true
        break
    }

    $events += (ConvertTo-EventRecord -Event $item -Now $now)
}

$payload = [pscustomobject][ordered]@{
    generatedAt    = $now.ToString('yyyy-MM-ddTHH:mm:ssK')
    dataSource     = 'microsoft-graph'
    window         = $Window
    windowEnd      = $windowEnd.ToString('yyyy-MM-ddTHH:mm:ss')
    eventCount     = $events.Count
    cancelledCount = $cancelledCount
    truncated      = $truncated
    events         = @($events)
}

if ($Format -eq 'text') {
    [Console]::Out.WriteLine((Format-Text -Payload $payload))
} else {
    [Console]::Out.WriteLine(($payload | ConvertTo-Json -Compress -Depth 5))
}

exit 0
