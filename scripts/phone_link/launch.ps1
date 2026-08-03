<#
.SYNOPSIS
  Launch or focus the Phone Link (Your Phone) Windows app.
#>
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$scriptDir\uiautomation-core.ps1"

try {
    $win = Find-PhoneLinkWindow
    if (-not $win) {
        $win = Start-PhoneLink
    }
    if ($win) {
        try { $win.SetFocus() } catch { }
        @{ ok = $true; launched = $true } | ConvertTo-Json -Compress
    } else {
        @{ ok = $false; error = "Could not find or launch Phone Link" } | ConvertTo-Json -Compress
    }
} catch {
    @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
