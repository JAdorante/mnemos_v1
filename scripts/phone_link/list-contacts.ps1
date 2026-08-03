<#
.SYNOPSIS
    Lists candidate contact names from Phone Link, to ground voice-transcribed
    recipients against real contacts. Opens a new-message compose (which surfaces
    "Suggested contacts") and scrapes the visible contact/thread rows. Read-only
    intent: it types nothing and sends nothing. Best-effort — returns whatever it
    can find; Python-side cleaning strips preview/timestamp noise.
.PARAMETER MaxContacts
    Maximum number of names to return (default: 40).
#>
param(
    [int]$MaxContacts = 40
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$scriptDir\uiautomation-core.ps1"

try {
    $win = Find-PhoneLinkWindow
    if (-not $win) {
        $win = Start-PhoneLink
        if (-not $win) {
            ConvertTo-McpJson @{ error = "Cannot find or launch Phone Link"; contacts = @() }
            return
        }
    }

    # Navigate to the Messages tab.
    $tabNames = @("Messages", "Messaggi", "Nachrichten", "Mensajes")
    foreach ($tabName in $tabNames) {
        $tab = Find-ElementsByName -Root $win -NameContains $tabName -MaxDepth 6 |
               Select-Object -First 1
        if ($tab) {
            Invoke-ElementClick -Element $tab | Out-Null
            Start-Sleep -Seconds 1
            break
        }
    }

    # Open "New message" so the Suggested contacts list renders. If the button
    # isn't found we still scrape the recent-threads view below.
    $newMsgNames = @("New message", "Nuovo messaggio", "Neue Nachricht", "Nuevo mensaje")
    foreach ($btnName in $newMsgNames) {
        $btn = Find-ElementsByName -Root $win -NameContains $btnName -MaxDepth 8 |
               Select-Object -First 1
        if ($btn) {
            Invoke-ElementClick -Element $btn | Out-Null
            Start-Sleep -Seconds 2
            break
        }
    }

    Start-Sleep -Seconds 1

    # Scrape contact/thread rows: ListItem / DataItem carry a contact display name
    # in their Name property (often with a message preview appended — cleaned on
    # the Python side).
    $allElements = Get-AllDescendants -Element $win -MaxDepth 20
    $contacts = @()
    $seen = @{}
    foreach ($el in $allElements) {
        try {
            $name = Normalize-UiText $el.Current.Name
            if (-not $name -or $name.Length -lt 2) { continue }
            $ctrlType = $el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
            if ($ctrlType -in @("ListItem", "DataItem")) {
                $key = $name.ToLower()
                if (-not $seen.ContainsKey($key)) {
                    $seen[$key] = $true
                    $contacts += $name
                }
            }
        } catch { continue }
    }

    # Close the compose we opened so a subsequent send-message.ps1 finds a clean
    # "New message" button rather than an already-open composer.
    try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}"); Start-Sleep -Milliseconds 400 } catch {}

    $contacts = $contacts | Select-Object -First $MaxContacts
    ConvertTo-McpJson @{
        contact_count = $contacts.Count
        contacts      = $contacts
    }
} catch {
    ConvertTo-McpJson @{ error = $_.Exception.Message; contacts = @() }
}
