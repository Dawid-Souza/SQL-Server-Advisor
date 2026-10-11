#Requires -Version 5.1
<#
.SYNOPSIS
Configura, uma única vez, o WinRM HTTPS usado pelo SQL Server Advisor.

.DESCRIPTION
Execute dentro de uma sessão RDP, em um Windows PowerShell aberto como
Administrador. É necessário um certificado de computador válido para
Autenticação de Servidor, com o DNS do servidor no CN ou SAN.

.EXAMPLE
.\Configure-SqlAdvisorWinRMHttps.ps1 -CertificateThumbprint '0123456789ABCDEF0123456789ABCDEF01234567'

.EXAMPLE
.\Configure-SqlAdvisorWinRMHttps.ps1 -CertificateThumbprint '0123456789ABCDEF0123456789ABCDEF01234567' -AllowedRemoteAddress '10.20.30.40'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9A-Fa-f ]{40,80}$')]
    [string]$CertificateThumbprint,

    [string[]]$AllowedRemoteAddress = @('LocalSubnet')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Abra o Windows PowerShell com Executar como administrador.'
}

$normalizedThumbprint = $CertificateThumbprint -replace '\s', ''
$certificate = Get-Item -LiteralPath "Cert:\LocalMachine\My\$normalizedThumbprint" -ErrorAction SilentlyContinue
if (-not $certificate) {
    throw "Certificado não encontrado em Cert:\LocalMachine\My: $normalizedThumbprint"
}
if (-not $certificate.HasPrivateKey) {
    throw 'O certificado selecionado não possui chave privada.'
}
if ($certificate.NotBefore -gt (Get-Date) -or $certificate.NotAfter -le (Get-Date)) {
    throw 'O certificado selecionado não está dentro do período de validade.'
}
$serverAuthenticationOid = '1.3.6.1.5.5.7.3.1'
if ($certificate.EnhancedKeyUsageList.ObjectId.Value -notcontains $serverAuthenticationOid) {
    throw 'O certificado precisa permitir Autenticação de Servidor.'
}

Enable-PSRemoting -Force

$httpsListeners = @(Get-ChildItem WSMan:\localhost\Listener | Where-Object {
    $_.Keys -contains 'Transport=HTTPS'
})
if ($httpsListeners.Count -eq 0) {
    New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address '*' -CertificateThumbPrint $normalizedThumbprint -Force | Out-Null
    Write-Host 'Listener WinRM HTTPS criado na porta 5986.' -ForegroundColor Green
} else {
    Write-Host 'Já existe um listener WinRM HTTPS; ele foi preservado.' -ForegroundColor Yellow
    Write-Host 'Confirme o certificado com: winrm enumerate winrm/config/listener' -ForegroundColor Yellow
}

$firewallDisplayName = 'SQL Server Advisor - WinRM HTTPS 5986'
$firewallRule = Get-NetFirewallRule -DisplayName $firewallDisplayName -ErrorAction SilentlyContinue
if (-not $firewallRule) {
    New-NetFirewallRule `
        -DisplayName $firewallDisplayName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 5986 `
        -Profile Domain,Private `
        -RemoteAddress $AllowedRemoteAddress | Out-Null
    Write-Host "Regra de firewall criada para: $($AllowedRemoteAddress -join ', ')" -ForegroundColor Green
} else {
    Write-Host 'A regra de firewall do SQL Server Advisor já existe e foi preservada.' -ForegroundColor Yellow
}

Test-WSMan -ComputerName $env:COMPUTERNAME -UseSSL | Out-Null
Write-Host 'WinRM HTTPS respondeu localmente.' -ForegroundColor Green
Write-Host 'No painel, use o nome DNS presente no certificado, informe a credencial remota e selecione WinRM HTTPS (5986).' -ForegroundColor Cyan
