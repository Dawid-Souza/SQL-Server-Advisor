from __future__ import annotations

import base64
import json
import os
import platform
import re
import socket
import subprocess
import ctypes
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


class DiscoveryError(RuntimeError):
    pass


@dataclass
class DiscoveredInstance:
    name: str
    instance_id: str
    service_name: str
    service_state: str = "Desconhecido"
    start_mode: str | None = None
    tcp_port: str | None = None
    dynamic_port: str | None = None
    process_memory_mb: int = 0


@dataclass
class HostInventory:
    computer_name: str
    os_caption: str
    os_version: str
    total_memory_mb: int
    free_memory_mb: int
    logical_cpus: int
    instances: list[DiscoveredInstance] = field(default_factory=list)
    discovery_method: str = "windows-local"
    warnings: list[str] = field(default_factory=list)


def is_local_target(target: str) -> bool:
    normalized = target.lower().rstrip(".")
    names = {
        ".",
        "localhost",
        "127.0.0.1",
        "::1",
        socket.gethostname().lower(),
        socket.getfqdn().lower(),
    }
    return normalized in names


def _powershell_executable() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    pwsh = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if pwsh.exists():
        return str(pwsh)
    return str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _run_powershell(script: str, environment: dict[str, str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [_powershell_executable(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
        capture_output=True,
        timeout=timeout_seconds,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        _decode_powershell_stream(completed.stdout),
        _decode_powershell_stream(completed.stderr),
    )


def _decode_powershell_stream(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            code_page = ctypes.windll.kernel32.GetOEMCP()
            return value.decode(f"cp{code_page}", errors="replace")
        return value.decode(errors="replace")


def clean_powershell_error(raw: str) -> str:
    """Turn PowerShell CLIXML into a short, readable diagnostic."""
    text = raw.strip()
    if "#< CLIXML" in text:
        xml_text = text.split("#< CLIXML", 1)[1].strip()
        try:
            root = ET.fromstring(xml_text)
            messages = [
                node.text or ""
                for node in root.iter()
                if node.tag.endswith("S") and node.attrib.get("S") == "Error"
            ]
            if messages:
                text = "\n".join(messages)
        except ET.ParseError:
            text = xml_text
    text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda match: chr(int(match.group(1), 16)), text)
    useful_lines: list[str] = []
    for line in text.replace("\r", "\n").splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"Line |", "OpenError:"}:
            continue
        if stripped.startswith("|"):
            stripped = stripped[1:].strip()
            if not stripped or set(stripped) <= {"~"}:
                continue
        if stripped.startswith(("+ ", "CategoryInfo", "FullyQualifiedErrorId")):
            continue
        if re.match(r"^\d+\s*\|", stripped):
            continue
        if stripped not in useful_lines:
            useful_lines.append(stripped)
    message = " ".join(useful_lines)
    lowered = message.lower()
    if ("erro de seguran" in lowered and "desconhecido" in lowered) or "unknown security error" in lowered:
        return "O Windows recusou a autenticação remota via WinRM/Kerberos."
    if "sessão de logon especificada não existe" in lowered or "specified logon session does not exist" in lowered:
        return (
            "O WinRM não conseguiu criar uma sessão com a credencial Windows informada. "
            "Confirme DOMÍNIO\\usuário ou SERVIDOR\\usuário; fora do domínio, use WinRM HTTPS."
        )
    if "acesso negado" in lowered or "access is denied" in lowered:
        return "A conta que executa o painel não tem acesso ao inventário remoto do Windows."
    if ("servidor rpc" in lowered and "dispon" in lowered) or "rpc server is unavailable" in lowered:
        return "O inventário via RPC/DCOM está bloqueado ou indisponível."
    if "server certificate" in lowered or "certificado do servidor" in lowered or "ssl certificate" in lowered:
        return (
            "O certificado do WinRM HTTPS não foi aceito. Use no painel o nome DNS presente no certificado "
            "e confirme que a autoridade emissora é confiável nesta máquina."
        )
    if "cannot connect to the destination" in lowered or "não pode se conectar ao destino" in lowered:
        return "O WinRM não respondeu no servidor. Confirme o listener e a regra de firewall da porta selecionada."
    return message[:700] or "Falha remota sem detalhes."


_DCOM_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$target = $env:DTA_TARGET_HOST
$credential = $null
if ($env:DTA_WINDOWS_USERNAME -and $env:DTA_WINDOWS_PASSWORD) {
    $securePassword = ConvertTo-SecureString $env:DTA_WINDOWS_PASSWORD -AsPlainText -Force
    $credential = [System.Management.Automation.PSCredential]::new($env:DTA_WINDOWS_USERNAME, $securePassword)
}
$session = $null
try {
    $sessionOption = New-CimSessionOption -Protocol Dcom
    if ($credential) {
        $session = New-CimSession -ComputerName $target -SessionOption $sessionOption -Credential $credential -ErrorAction Stop
    } else {
        $session = New-CimSession -ComputerName $target -SessionOption $sessionOption -ErrorAction Stop
    }
    $os = Get-CimInstance Win32_OperatingSystem -CimSession $session -ErrorAction Stop
    $cs = Get-CimInstance Win32_ComputerSystem -CimSession $session -ErrorAction Stop
    $sqlServices = @(Get-CimInstance Win32_Service -CimSession $session -ErrorAction Stop | Where-Object {
        $_.Name -eq 'MSSQLSERVER' -or $_.Name -like 'MSSQL$*'
    })

    $registryBases = @()
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
        try {
            $registryBases += [Microsoft.Win32.RegistryKey]::OpenRemoteBaseKey(
                [Microsoft.Win32.RegistryHive]::LocalMachine, $target, $view
            )
        } catch { }
    }
    $instanceMap = @{}
    foreach ($base in $registryBases) {
        $key = $base.OpenSubKey('SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL')
        if ($key) {
            foreach ($name in $key.GetValueNames()) { $instanceMap[$name] = [string]$key.GetValue($name) }
            $key.Dispose()
        }
    }
    foreach ($service in $sqlServices) {
        $inferredName = if ($service.Name -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { $service.Name.Substring(6) }
        if (-not $instanceMap.ContainsKey($inferredName)) { $instanceMap[$inferredName] = $inferredName }
    }

    $instances = @()
    foreach ($entry in $instanceMap.GetEnumerator() | Sort-Object Name) {
        $instanceName = [string]$entry.Name
        $instanceId = [string]$entry.Value
        $serviceName = if ($instanceName -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { 'MSSQL$' + $instanceName }
        $service = $sqlServices | Where-Object Name -eq $serviceName | Select-Object -First 1
        $tcpPort = $null
        $dynamicPort = $null
        foreach ($base in $registryBases) {
            $tcpKey = $base.OpenSubKey("SOFTWARE\Microsoft\Microsoft SQL Server\$instanceId\MSSQLServer\SuperSocketNetLib\Tcp\IPAll")
            if ($tcpKey) {
                $tcpPort = [string]$tcpKey.GetValue('TcpPort')
                $dynamicPort = [string]$tcpKey.GetValue('TcpDynamicPorts')
                $tcpKey.Dispose()
                break
            }
        }
        $processMemoryMb = 0
        if ($service -and $service.ProcessId -gt 0) {
            $process = Get-CimInstance Win32_Process -CimSession $session -Filter ("ProcessId=" + $service.ProcessId) -ErrorAction SilentlyContinue
            if ($process) { $processMemoryMb = [int][math]::Round([double]$process.WorkingSetSize / 1MB) }
        }
        $instances += [ordered]@{
            name = $instanceName
            instance_id = $instanceId
            service_name = $serviceName
            service_state = if ($service) { [string]$service.State } else { 'Not Found' }
            start_mode = if ($service) { [string]$service.StartMode } else { $null }
            tcp_port = $tcpPort
            dynamic_port = $dynamicPort
            process_memory_mb = $processMemoryMb
        }
    }
    foreach ($base in $registryBases) { $base.Dispose() }
    [ordered]@{
        computer_name = [string]$cs.Name
        os_caption = [string]$os.Caption
        os_version = [string]$os.Version
        total_memory_mb = [int][math]::Round([double]$cs.TotalPhysicalMemory / 1MB)
        free_memory_mb = [int][math]::Round([double]$os.FreePhysicalMemory / 1KB)
        logical_cpus = [int]$cs.NumberOfLogicalProcessors
        instances = @($instances)
    } | ConvertTo-Json -Depth 7 -Compress
} finally {
    if ($session) { Remove-CimSession $session -ErrorAction SilentlyContinue }
}
'''


def _discover_with_sql_browser(target: str, prior_errors: list[str]) -> HostInventory:
    instances: list[DiscoveredInstance] = []
    browser_error: str | None = None
    try:
        addresses = socket.getaddrinfo(target, 1434, type=socket.SOCK_DGRAM)
        family, socktype, proto, _, address = addresses[0]
        with socket.socket(family, socktype, proto) as client:
            client.settimeout(1.8)
            client.sendto(b"\x03", address)
            response, _ = client.recvfrom(65535)
        instances = parse_sql_browser_response(response)
    except (OSError, TimeoutError) as exc:
        browser_error = str(exc)

    if not instances:
        instances = [
            DiscoveredInstance(
                name="MSSQLSERVER",
                instance_id="MSSQLSERVER",
                service_name="MSSQLSERVER",
                service_state="A verificar",
                tcp_port="1433",
            )
        ]
    warnings = [
        "O inventário administrativo do Windows não está acessível; a análise continuou pelo SQL Server.",
        "Estado do serviço, modo de inicialização e instâncias com SQL Browser desativado podem não aparecer.",
    ]
    if browser_error:
        warnings.append("O SQL Browser não respondeu; a porta padrão 1433 será testada.")
    warnings.extend(prior_errors)
    return HostInventory(
        computer_name=target,
        os_caption="Windows Server (inventário parcial)",
        os_version="Não disponível sem acesso administrativo",
        total_memory_mb=0,
        free_memory_mb=0,
        logical_cpus=1,
        instances=instances,
        discovery_method="sql-browser" if browser_error is None else "sql-default-port",
        warnings=warnings,
    )


def parse_sql_browser_response(response: bytes) -> list[DiscoveredInstance]:
    payload = response[3:] if response[:1] == b"\x05" and len(response) > 3 else response
    text = payload.decode("ascii", errors="replace").strip("\x00;")
    instances: list[DiscoveredInstance] = []
    for record in re.split(r";;+", text):
        fields = [field.strip() for field in record.split(";")]
        values = {fields[index]: fields[index + 1] for index in range(0, len(fields) - 1, 2)}
        name = values.get("InstanceName")
        if not name:
            continue
        port = values.get("tcp")
        instances.append(
            DiscoveredInstance(
                name=name,
                instance_id=name,
                service_name="MSSQLSERVER" if name.upper() == "MSSQLSERVER" else f"MSSQL${name}",
                service_state="A verificar",
                tcp_port=port if port and port.isdigit() else None,
            )
        )
    return instances


def discover_host(
    target: str,
    timeout_seconds: int = 35,
    windows_username: str | None = None,
    windows_password: str | None = None,
    winrm_use_ssl: bool = False,
) -> HostInventory:
    script = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$collector = {
    $ErrorActionPreference = 'Stop'
    $os = $null
    $cs = $null
    try { $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop } catch { }
    try { $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop } catch { }
    $logicalCpuCount = if ($cs) { [int]$cs.NumberOfLogicalProcessors } else { [Environment]::ProcessorCount }
    if ($logicalCpuCount -le 0) {
        $logicalCpuCount = [Environment]::ProcessorCount
    }
    $windowsInfo = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue

    $instanceMap = @{}
    $registryPaths = @(
        'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Microsoft SQL Server\Instance Names\SQL'
    )
    foreach ($registryPath in $registryPaths) {
        if (Test-Path $registryPath) {
            $item = Get-ItemProperty $registryPath
            foreach ($property in $item.PSObject.Properties) {
                if ($property.Name -notmatch '^PS') {
                    $instanceMap[$property.Name] = [string]$property.Value
                }
            }
        }
    }

    $sqlServices = @()
    try {
        $sqlServices = @(Get-CimInstance Win32_Service -ErrorAction Stop | Where-Object {
            $_.Name -eq 'MSSQLSERVER' -or $_.Name -like 'MSSQL$*'
        })
    } catch {
        $sqlServices = @(Get-Service -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -eq 'MSSQLSERVER' -or $_.Name -like 'MSSQL$*'
        } | ForEach-Object {
            $serviceRegistry = Get-ItemProperty ("HKLM:\SYSTEM\CurrentControlSet\Services\" + $_.Name) -ErrorAction SilentlyContinue
            $startModes = @{ 2 = 'Auto'; 3 = 'Manual'; 4 = 'Disabled' }
            [pscustomobject]@{
                Name = $_.Name
                State = [string]$_.Status
                StartMode = $startModes[[int]$serviceRegistry.Start]
                ProcessId = 0
            }
        })
    }
    foreach ($service in $sqlServices) {
        $inferredName = if ($service.Name -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { $service.Name.Substring(6) }
        if (-not $instanceMap.ContainsKey($inferredName)) {
            $instanceMap[$inferredName] = $inferredName
        }
    }

    $instances = @()
    foreach ($entry in $instanceMap.GetEnumerator() | Sort-Object Name) {
        $instanceName = [string]$entry.Name
        $instanceId = [string]$entry.Value
        $serviceName = if ($instanceName -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { 'MSSQL$' + $instanceName }
        $service = $sqlServices | Where-Object Name -eq $serviceName | Select-Object -First 1
        $tcpPort = $null
        $dynamicPort = $null
        $tcpPaths = @(
            "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\$instanceId\MSSQLServer\SuperSocketNetLib\Tcp\IPAll",
            "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Microsoft SQL Server\$instanceId\MSSQLServer\SuperSocketNetLib\Tcp\IPAll"
        )
        foreach ($tcpPath in $tcpPaths) {
            if (Test-Path $tcpPath) {
                $tcp = Get-ItemProperty $tcpPath
                if ($tcp.TcpPort) { $tcpPort = [string]$tcp.TcpPort }
                if ($tcp.TcpDynamicPorts) { $dynamicPort = [string]$tcp.TcpDynamicPorts }
                break
            }
        }
        $processMemoryMb = 0
        if ($service -and $service.ProcessId -gt 0) {
            $process = Get-Process -Id $service.ProcessId -ErrorAction SilentlyContinue
            if ($process) { $processMemoryMb = [int][math]::Round($process.WorkingSet64 / 1MB) }
        }
        $instances += [ordered]@{
            name = $instanceName
            instance_id = $instanceId
            service_name = $serviceName
            service_state = if ($service) { [string]$service.State } else { 'Not Found' }
            start_mode = if ($service) { [string]$service.StartMode } else { $null }
            tcp_port = $tcpPort
            dynamic_port = $dynamicPort
            process_memory_mb = $processMemoryMb
        }
    }

    [ordered]@{
        computer_name = [string]$env:COMPUTERNAME
        os_caption = if ($os) { [string]$os.Caption } elseif ($windowsInfo.ProductName) { [string]$windowsInfo.ProductName } else { 'Windows' }
        os_version = if ($os) { [string]$os.Version } elseif ($windowsInfo.DisplayVersion) { [string]$windowsInfo.DisplayVersion } else { [Environment]::OSVersion.Version.ToString() }
        total_memory_mb = if ($cs) { [int][math]::Round([double]$cs.TotalPhysicalMemory / 1MB) } else { 0 }
        free_memory_mb = if ($os) { [int][math]::Round([double]$os.FreePhysicalMemory / 1KB) } else { 0 }
        logical_cpus = $logicalCpuCount
        instances = @($instances)
    }
}

if ($env:DTA_REMOTE_TARGET -eq '1') {
    $invokeParameters = @{
        ComputerName = $env:DTA_TARGET_HOST
        ScriptBlock = $collector
    }
    if ($env:DTA_WINRM_USE_SSL -eq '1') {
        $invokeParameters.UseSSL = $true
        $invokeParameters.Port = 5986
    }
    if ($env:DTA_WINDOWS_USERNAME -and $env:DTA_WINDOWS_PASSWORD) {
        $securePassword = ConvertTo-SecureString $env:DTA_WINDOWS_PASSWORD -AsPlainText -Force
        $credential = [System.Management.Automation.PSCredential]::new($env:DTA_WINDOWS_USERNAME, $securePassword)
        $invokeParameters.Credential = $credential
        $invokeParameters.Authentication = 'Negotiate'
    }
    $result = Invoke-Command @invokeParameters
} else {
    $result = & $collector
}
$result | ConvertTo-Json -Depth 7 -Compress
'''
    environment = os.environ.copy()
    environment["DTA_TARGET_HOST"] = target
    environment["DTA_WINRM_USE_SSL"] = "1" if winrm_use_ssl else "0"
    local_target = is_local_target(target)
    environment["DTA_REMOTE_TARGET"] = "0" if local_target else "1"
    if windows_username and windows_password and not local_target:
        environment["DTA_WINDOWS_USERNAME"] = windows_username
        environment["DTA_WINDOWS_PASSWORD"] = windows_password
    try:
        completed = _run_powershell(script, environment, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if local_target:
            raise DiscoveryError(f"Falha ao executar a descoberta local do Windows: {exc}") from exc
        return _discover_with_sql_browser(target, ["A tentativa de inventário via WinRM excedeu o tempo limite."])

    if completed.returncode != 0:
        winrm_error = clean_powershell_error(completed.stderr or completed.stdout or "Falha sem detalhes")
        if local_target:
            raise DiscoveryError(winrm_error)
        try:
            dcom_completed = _run_powershell(_DCOM_SCRIPT, environment, min(timeout_seconds, 20))
        except (OSError, subprocess.TimeoutExpired):
            return _discover_with_sql_browser(
                target,
                [f"WinRM: {winrm_error}", "RPC/DCOM: a tentativa excedeu o tempo limite."],
            )
        if dcom_completed.returncode == 0:
            try:
                payload = _load_json_payload(dcom_completed.stdout)
            except (json.JSONDecodeError, ValueError):
                return _discover_with_sql_browser(
                    target,
                    [f"WinRM: {winrm_error}", "RPC/DCOM retornou um inventário inválido."],
                )
            return _inventory_from_payload(
                payload,
                target,
                discovery_method="windows-dcom",
                warnings=[f"WinRM indisponível; o inventário foi concluído via RPC/DCOM. {winrm_error}"],
            )
        dcom_error = clean_powershell_error(dcom_completed.stderr or dcom_completed.stdout or "Falha sem detalhes")
        return _discover_with_sql_browser(target, [f"WinRM: {winrm_error}", f"RPC/DCOM: {dcom_error}"])

    try:
        payload = _load_json_payload(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DiscoveryError("A descoberta retornou dados inválidos do Windows Server.") from exc
    return _inventory_from_payload(
        payload,
        target,
        discovery_method="windows-local" if local_target else "windows-winrm",
    )


def _load_json_payload(raw: str) -> dict:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON não encontrado")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Objeto JSON esperado")
    return payload


def _inventory_from_payload(
    payload: dict,
    target: str,
    discovery_method: str,
    warnings: list[str] | None = None,
) -> HostInventory:

    raw_instances = payload.get("instances") or []
    if isinstance(raw_instances, dict):
        raw_instances = [raw_instances]
    total_memory_mb = max(0, int(payload.get("total_memory_mb") or 0))
    free_memory_mb = max(0, int(payload.get("free_memory_mb") or 0))
    logical_cpus = max(1, int(payload.get("logical_cpus") or 1))
    os_caption = str(payload.get("os_caption") or "Windows Server")
    os_version = str(payload.get("os_version") or "Não informado")
    computer_name = str(payload.get("computer_name") or target)
    if is_local_target(target):
        local_total, local_free = _local_memory_stats()
        total_memory_mb = local_total or total_memory_mb
        free_memory_mb = local_free or free_memory_mb
        logical_cpus = os.cpu_count() or logical_cpus
        computer_name = os.environ.get("COMPUTERNAME", socket.gethostname())
        if os_caption in {"Windows", "Windows Server"}:
            os_caption = platform.platform()
    return HostInventory(
        computer_name=computer_name,
        os_caption=os_caption,
        os_version=os_version,
        total_memory_mb=total_memory_mb,
        free_memory_mb=free_memory_mb,
        logical_cpus=logical_cpus,
        instances=[
            DiscoveredInstance(
                name=str(item.get("name") or "MSSQLSERVER"),
                instance_id=str(item.get("instance_id") or ""),
                service_name=str(item.get("service_name") or ""),
                service_state=str(item.get("service_state") or "Desconhecido"),
                start_mode=item.get("start_mode"),
                tcp_port=str(item["tcp_port"]) if item.get("tcp_port") else None,
                dynamic_port=str(item["dynamic_port"]) if item.get("dynamic_port") else None,
                process_memory_mb=max(0, int(item.get("process_memory_mb") or 0)),
            )
            for item in raw_instances
        ],
        discovery_method=discovery_method,
        warnings=warnings or [],
    )


def _local_memory_stats() -> tuple[int, int]:
    if os.name != "nt":
        return 0, 0

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.length = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0, 0
    return round(status.total_physical / 1024 / 1024), round(status.available_physical / 1024 / 1024)
