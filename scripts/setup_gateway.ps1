# ===========================================================================
#  Quota Manager — one-time gateway setup for Windows 10/11
# ---------------------------------------------------------------------------
#  Run as Administrator (right-click -> Run with PowerShell, or):
#      powershell -ExecutionPolicy Bypass -File .\scripts\setup_gateway.ps1
#
#  What it does:
#    1. Enables IP forwarding (IPEnableRouter) so the PC routes for clients.
#    2. Configures Windows Firewall to allow forwarded traffic.
#    3. Reports the PC's current IPv4 addresses so you can set config.yaml.
#    4. Verifies admin privileges and Npcap presence (recommended for ARP).
#
#  What it does NOT do (must be done on the router):
#    - Disable the router's DHCP server.
#    - Reserve/configure the PC's static IP on the router's LAN subnet.
#  After this script, REBOOT the PC once.
# ===========================================================================

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
Write-Host "== Quota Manager gateway setup ==" -ForegroundColor Cyan

# --- 1. Admin check ---------------------------------------------------------
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Run this script as Administrator." -ForegroundColor Red
    exit 1
}

# --- 2. Enable IP forwarding --------------------------------------------------
Write-Host "`n[1/4] Enabling IP forwarding (IPEnableRouter)..." -ForegroundColor Yellow
$ipEnable = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" -Name IPEnableRouter -ErrorAction SilentlyContinue
if ($ipEnable -and $ipEnable.IPEnableRouter -eq 1) {
    Write-Host "  IPEnableRouter already = 1 (OK)" -ForegroundColor Green
} else {
    New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" `
        -Name IPEnableRouter -Value 1 -PropertyType DWord -Force | Out-Null
    Write-Host "  IPEnableRouter set to 1. A reboot is required." -ForegroundColor Green
}

# --- 3. Windows Firewall: allow forwarded (routed) traffic ---------------------
Write-Host "`n[2/4] Configuring Windows Firewall for forwarded traffic..." -ForegroundColor Yellow
# The 'core networking - router advertisement' rules sometimes block routing on
# Windows clients; the most reliable addition is a pair of allow rules.
$ruleNames = @(
    @{ Name = "QuotaManager - Allow IPv4 Routing In";  Direction = "Inbound" },
    @{ Name = "QuotaManager - Allow IPv4 Routing Out"; Direction = "Outbound" }
)
foreach ($r in $ruleNames) {
    $name = $r.Name
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
        Write-Host "  Rule already exists: $name (OK)" -ForegroundColor Green
    } else {
        New-NetFirewallRule -DisplayName $name -Direction $r.Direction  `
            -Action Allow -Protocol Any -Profile Any | Out-Null
        Write-Host "  Created: $name ($($r.Direction))" -ForegroundColor Green
    }
}
# Also raise the private-network profile so the PC is discoverable on the LAN.
Set-NetFirewallProfile -Profile Private -DefaultInboundAction Allow -ErrorAction SilentlyContinue

# --- 4. Report current networking facts ---------------------------------------
Write-Host "`n[3/4] Current network addresses (use these in config.yaml):" -ForegroundColor Yellow
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object InterfaceAlias, IPAddress, PrefixLength, @{N="Mac";E={(Get-NetAdapter -InterfaceIndex $_.InterfaceIndex).MacAddress}} |
    Format-Table -AutoSize

Write-Host "`n[4/4] Checking prerequisites..." -ForegroundColor Yellow
# Npcap (recommended for proxy-ARP via scapy)
if (Get-Service -Name npcap -ErrorAction SilentlyContinue) {
    Write-Host "  Npcap: installed (proxy-ARP available)" -ForegroundColor Green
} else {
    Write-Host "  Npcap: NOT installed. Download from https://npcap.com" -ForegroundColor Yellow
    Write-Host "         (optional: quota still works, download accounting is approximate)" -ForegroundColor Yellow
}
# Admin requirements for pydivert
Write-Host "  Running as Administrator: yes (required by pydivert)" -ForegroundColor Green

Write-Host "`n== NEXT STEPS ==" -ForegroundColor Cyan
Write-Host "  1. On your router: disable its DHCP server, and give this PC a"
Write-Host "     static/reserved IP on the router's LAN subnet."
Write-Host "  2. Edit config.yaml: set dhcp.gateway_ip to THIS PC's static IP,"
Write-Host "     and the subnet, pool_start/end, and bundle size."
Write-Host "  3. REBOOT this PC (IPEnableRouter takes effect on boot)."
Write-Host "  4. Start the app:  .\.venv\Scripts\python.exe run.py"
