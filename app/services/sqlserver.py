from __future__ import annotations

import os
import shutil
import subprocess
import ctypes
import base64
from dataclasses import dataclass, field

from app.services.discovery import (
    DiscoveredInstance,
    _load_json_payload,
    _run_powershell,
    clean_powershell_error,
    is_local_target,
)
from app.services.security import safe_public_error


@dataclass
class SqlInstanceFacts:
    connected: bool = False
    values: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    endpoint: str = ""

    def integer(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.values.get(key, default)))
        except (TypeError, ValueError):
            return default


def build_endpoint(host: str, instance: DiscoveredInstance) -> str:
    port = instance.tcp_port or instance.dynamic_port
    if port and port.isdigit() and int(port) > 0:
        return f"tcp:{host},{port}"
    if instance.name.upper() == "MSSQLSERVER":
        return host
    return f"{host}\\{instance.name}"


def _find_sqlcmd() -> str | None:
    found = shutil.which("sqlcmd")
    if found:
        return found
    common = r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\SQLCMD.EXE"
    return common if os.path.exists(common) else None


_BASE_QUERY = r'''
SET NOCOUNT ON;
SELECT 'server_name', CONVERT(nvarchar(4000), SERVERPROPERTY('ServerName'))
UNION ALL SELECT 'product_version', CONVERT(nvarchar(4000), SERVERPROPERTY('ProductVersion'))
UNION ALL SELECT 'product_level', CONVERT(nvarchar(4000), SERVERPROPERTY('ProductLevel'))
UNION ALL SELECT 'edition', CONVERT(nvarchar(4000), SERVERPROPERTY('Edition'))
UNION ALL SELECT 'engine_edition', CONVERT(nvarchar(4000), SERVERPROPERTY('EngineEdition'))
UNION ALL SELECT 'is_clustered', CONVERT(nvarchar(4000), SERVERPROPERTY('IsClustered'))
UNION ALL SELECT 'max_server_memory_mb', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'max server memory (MB)'))
UNION ALL SELECT 'min_server_memory_mb', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'min server memory (MB)'))
UNION ALL SELECT 'maxdop', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'max degree of parallelism'))
UNION ALL SELECT 'cost_threshold', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'cost threshold for parallelism'))
UNION ALL SELECT 'optimize_ad_hoc', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'optimize for ad hoc workloads'))
UNION ALL SELECT 'backup_compression', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'backup compression default'))
UNION ALL SELECT 'backup_checksum', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'backup checksum default'))
UNION ALL SELECT 'xp_cmdshell', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'xp_cmdshell'))
UNION ALL SELECT 'ole_automation', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'Ole Automation Procedures'))
UNION ALL SELECT 'ad_hoc_distributed_queries', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'Ad Hoc Distributed Queries'))
UNION ALL SELECT 'cross_db_ownership_chaining', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'cross db ownership chaining'))
UNION ALL SELECT 'priority_boost', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'priority boost'))
UNION ALL SELECT 'lightweight_pooling', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'lightweight pooling'))
UNION ALL SELECT 'max_worker_threads', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'max worker threads'))
UNION ALL SELECT 'network_packet_size', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'network packet size (B)'))
UNION ALL SELECT 'recovery_interval', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'recovery interval (min)'))
UNION ALL SELECT 'default_trace_enabled', CONVERT(nvarchar(4000), (SELECT value_in_use FROM sys.configurations WHERE name = 'default trace enabled'))
UNION ALL SELECT 'tempdb_data_files', CONVERT(nvarchar(4000), (SELECT COUNT(*) FROM tempdb.sys.database_files WHERE type = 0))
UNION ALL SELECT 'tempdb_min_growth_mb', CONVERT(nvarchar(4000), (SELECT MIN(CASE WHEN is_percent_growth = 1 THEN 0 ELSE growth * 8 / 1024 END) FROM tempdb.sys.database_files WHERE type = 0))
UNION ALL SELECT 'tempdb_equal_sizes', CONVERT(nvarchar(4000), (SELECT CASE WHEN MIN(size) = MAX(size) THEN 1 ELSE 0 END FROM tempdb.sys.database_files WHERE type = 0));
BEGIN TRY
    SELECT 'host_machine_name', CONVERT(nvarchar(4000), SERVERPROPERTY('MachineName'))
    UNION ALL SELECT 'host_total_memory_mb', CONVERT(nvarchar(4000), (SELECT physical_memory_kb / 1024 FROM sys.dm_os_sys_info))
    UNION ALL SELECT 'host_logical_cpus', CONVERT(nvarchar(4000), (SELECT cpu_count FROM sys.dm_os_sys_info));
    SELECT 'host_free_memory_mb', CONVERT(nvarchar(4000), available_physical_memory_kb / 1024)
    FROM sys.dm_os_sys_memory;
    SELECT 'sql_process_memory_mb', CONVERT(nvarchar(4000), physical_memory_in_use_kb / 1024)
    FROM sys.dm_os_process_memory;
    SELECT 'locked_pages_mb', CONVERT(nvarchar(4000), locked_page_allocations_kb / 1024)
    FROM sys.dm_os_process_memory;
    SELECT 'numa_node_count', CONVERT(nvarchar(4000), COUNT(DISTINCT parent_node_id))
    FROM sys.dm_os_schedulers
    WHERE status = 'VISIBLE ONLINE' AND scheduler_id < 1048576;
    SELECT 'max_schedulers_per_numa', CONVERT(nvarchar(4000), MAX(scheduler_count))
    FROM (
        SELECT parent_node_id, COUNT(*) AS scheduler_count
        FROM sys.dm_os_schedulers
        WHERE status = 'VISIBLE ONLINE' AND scheduler_id < 1048576
        GROUP BY parent_node_id
    ) AS numa;
    SELECT 'single_use_adhoc_mb', CONVERT(nvarchar(4000), COALESCE(SUM(CONVERT(bigint, size_in_bytes)) / 1048576, 0))
    FROM sys.dm_exec_cached_plans
    WHERE objtype = 'Adhoc' AND usecounts = 1;
    SELECT 'plan_cache_mb', CONVERT(nvarchar(4000), COALESCE(SUM(CONVERT(bigint, size_in_bytes)) / 1048576, 0))
    FROM sys.dm_exec_cached_plans;
    SELECT TOP (1) 'ifi_enabled', CONVERT(nvarchar(4000), instant_file_initialization_enabled)
    FROM sys.dm_server_services
    WHERE servicename LIKE 'SQL Server (%' OR servicename = 'SQL Server';
END TRY
BEGIN CATCH
    SELECT 'host_metrics_error', CONVERT(nvarchar(4000), ERROR_MESSAGE());
END CATCH;
BEGIN TRY
    SELECT 'database_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND source_database_id IS NULL
    UNION ALL SELECT 'db_not_online_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc <> 'ONLINE'
    UNION ALL SELECT 'db_auto_close_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_close_on = 1
    UNION ALL SELECT 'db_auto_shrink_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_shrink_on = 1
    UNION ALL SELECT 'db_page_verify_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND page_verify_option_desc <> 'CHECKSUM'
    UNION ALL SELECT 'db_auto_create_stats_off_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_create_stats_on = 0
    UNION ALL SELECT 'db_auto_update_stats_off_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_update_stats_on = 0
    UNION ALL SELECT 'db_query_store_off_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_query_store_on = 0
    UNION ALL SELECT 'db_trustworthy_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_trustworthy_on = 1
    UNION ALL SELECT 'db_chaining_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_db_chaining_on = 1
    UNION ALL SELECT 'db_percent_growth_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.master_files WHERE database_id > 4 AND is_percent_growth = 1
    UNION ALL SELECT 'db_small_growth_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.master_files WHERE database_id > 4 AND is_percent_growth = 0 AND growth > 0 AND ((type = 0 AND growth * 8 / 1024 < 64) OR (type = 1 AND growth * 8 / 1024 < 64));

    SELECT 'db_not_online_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc <> 'ONLINE' ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_auto_close_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_close_on = 1 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_auto_shrink_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_shrink_on = 1 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_page_verify_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND page_verify_option_desc <> 'CHECKSUM' ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_auto_create_stats_off_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_create_stats_on = 0 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_auto_update_stats_off_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_auto_update_stats_on = 0 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_query_store_off_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_query_store_on = 0 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_trustworthy_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_trustworthy_on = 1 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''))
    UNION ALL SELECT 'db_chaining_names', CONVERT(nvarchar(4000), STUFF((SELECT N', ' + QUOTENAME(name) FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_db_chaining_on = 1 ORDER BY name FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, N''));
END TRY
BEGIN CATCH
    SELECT 'database_metrics_error', CONVERT(nvarchar(4000), ERROR_MESSAGE());
END CATCH;
BEGIN TRY
    SELECT 'db_full_backup_overdue_count', CONVERT(nvarchar(4000), COUNT(*))
    FROM sys.databases AS d
    OUTER APPLY (
        SELECT MAX(backup_finish_date) AS last_backup
        FROM msdb.dbo.backupset
        WHERE database_name = d.name AND type = 'D' AND is_copy_only = 0
    ) AS b
    WHERE d.database_id > 4 AND d.state_desc = 'ONLINE' AND d.source_database_id IS NULL
      AND (b.last_backup IS NULL OR b.last_backup < DATEADD(day, -7, GETDATE()));
    SELECT 'db_log_backup_overdue_count', CONVERT(nvarchar(4000), COUNT(*))
    FROM sys.databases AS d
    OUTER APPLY (
        SELECT MAX(backup_finish_date) AS last_backup
        FROM msdb.dbo.backupset
        WHERE database_name = d.name AND type = 'L'
    ) AS b
    WHERE d.database_id > 4 AND d.state_desc = 'ONLINE' AND d.source_database_id IS NULL
      AND d.recovery_model_desc IN ('FULL', 'BULK_LOGGED')
      AND (b.last_backup IS NULL OR b.last_backup < DATEADD(hour, -24, GETDATE()));
    SELECT 'suspect_pages_count', CONVERT(nvarchar(4000), COUNT(*))
    FROM msdb.dbo.suspect_pages
    WHERE event_type IN (1, 2, 3);
END TRY
BEGIN CATCH
    SELECT 'operational_metrics_error', CONVERT(nvarchar(4000), ERROR_MESSAGE());
END CATCH;
'''


_REMOTE_SQLCMD_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$securePassword = ConvertTo-SecureString $env:DTA_WINDOWS_PASSWORD -AsPlainText -Force
$credential = [System.Management.Automation.PSCredential]::new($env:DTA_WINDOWS_USERNAME, $securePassword)
$query = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:DTA_SQL_QUERY_B64))
$remoteEndpoint = $env:DTA_SQL_REMOTE_ENDPOINT
$encryptConnection = $env:DTA_SQL_ENCRYPT -eq '1'
$trustCertificate = $env:DTA_SQL_TRUST_CERT -eq '1'
$sqlAuthentication = $env:DTA_SQL_AUTHENTICATION
$sqlUsername = $env:DTA_SQL_USERNAME
$sqlPassword = $env:DTA_SQL_PASSWORD
$invokeParameters = @{
    ComputerName = $env:DTA_TARGET_HOST
    Credential = $credential
    Authentication = 'Negotiate'
    ArgumentList = @($remoteEndpoint, $query, $encryptConnection, $trustCertificate, $sqlAuthentication, $sqlUsername, $sqlPassword)
    ScriptBlock = {
    param($endpoint, $sqlQuery, $encrypt, $trust, $authentication, $username, $password)
    $sqlcmdCommand = Get-Command sqlcmd -ErrorAction SilentlyContinue
    if (-not $sqlcmdCommand) {
        $sqlcmdCommand = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\*\Tools\Binn\SQLCMD.EXE' -File -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1
    }
    if (-not $sqlcmdCommand) { throw 'sqlcmd não foi encontrado no Windows Server.' }
    $sqlcmdPath = if ($sqlcmdCommand.Source) { $sqlcmdCommand.Source } else { $sqlcmdCommand.FullName }
    $arguments = @('-S', $endpoint, '-d', 'master', '-l', '12', '-b', '-I', '-w', '65535', '-h', '-1', '-s', '|', '-Q', $sqlQuery)
    if ($authentication -eq 'sql') {
        $arguments += @('-U', $username)
        $env:SQLCMDPASSWORD = $password
    } else {
        $arguments += '-E'
    }
    if ($encrypt) { $arguments += '-N' }
    if ($encrypt -and $trust) { $arguments += '-C' }
    try {
        $sqlOutput = & $sqlcmdPath @arguments 2>&1 | Out-String
        [ordered]@{ exit_code = $LASTEXITCODE; output = $sqlOutput }
    } finally {
        Remove-Item Env:\SQLCMDPASSWORD -ErrorAction SilentlyContinue
    }
    }
}
if ($env:DTA_WINRM_USE_SSL -eq '1') {
    $invokeParameters.UseSSL = $true
    $invokeParameters.Port = 5986
}
$result = Invoke-Command @invokeParameters
$result | ConvertTo-Json -Depth 5 -Compress
'''


def _parse_pairs(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "|" not in line or line.startswith("Sqlcmd:"):
            continue
        key, value = line.split("|", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def _decode_windows_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            return value.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", errors="replace")
        return value.decode(errors="replace")


def _clean_sqlcmd_error(detail: str, authentication: str, secret: str | None) -> str:
    sanitized = safe_public_error(detail, secret)
    lowered = sanitized.lower()
    if "login failed" in lowered or "falha de logon" in lowered or "falha de login" in lowered:
        return "O SQL Server recusou o usuário ou a senha informados."
    if authentication == "windows" and (
        "sspi" in lowered or "credenciais não disponíveis" in lowered or "security package" in lowered
    ):
        return (
            "A autenticação integrada do Windows não foi aceita pelo SQL Server. "
            "Selecione “Usuário do SQL Server” e informe um login autorizado."
        )
    if "encryption not supported" in lowered or "ssl provider" in lowered or "certificate" in lowered:
        return (
            "A negociação TLS/certificado com o SQL Server falhou. Revise o suporte TLS do servidor; "
            "em rede confiável, a opção de criptografia pode ser desmarcada para diagnóstico."
        )
    if "server is not found" in lowered or "servidor não foi encontrado" in lowered:
        return "A instância SQL foi anunciada, mas a conexão TCP não foi aceita na porta detectada."
    return sanitized


def _collect_sql_facts_with_remote_windows_credential(
    host: str,
    instance: DiscoveredInstance,
    authentication: str,
    username: str | None,
    password: str | None,
    windows_username: str,
    windows_password: str,
    login_timeout_seconds: int,
    encrypt_connection: bool,
    trust_server_certificate: bool,
    winrm_use_ssl: bool,
) -> SqlInstanceFacts:
    public_endpoint = build_endpoint(host, instance)
    port = instance.tcp_port or instance.dynamic_port
    if port and port.isdigit() and int(port) > 0:
        remote_endpoint = f"tcp:127.0.0.1,{port}"
    elif instance.name.upper() == "MSSQLSERVER":
        remote_endpoint = "."
    else:
        remote_endpoint = f".\\{instance.name}"
    environment = os.environ.copy()
    environment["DTA_TARGET_HOST"] = host
    environment["DTA_WINDOWS_USERNAME"] = windows_username
    environment["DTA_WINDOWS_PASSWORD"] = windows_password
    environment["DTA_SQL_REMOTE_ENDPOINT"] = remote_endpoint
    environment["DTA_SQL_QUERY_B64"] = base64.b64encode(_BASE_QUERY.encode("utf-8")).decode("ascii")
    environment["DTA_SQL_ENCRYPT"] = "1" if encrypt_connection else "0"
    environment["DTA_SQL_TRUST_CERT"] = "1" if trust_server_certificate else "0"
    environment["DTA_SQL_AUTHENTICATION"] = authentication
    environment["DTA_SQL_USERNAME"] = username or ""
    environment["DTA_SQL_PASSWORD"] = password or ""
    environment["DTA_WINRM_USE_SSL"] = "1" if winrm_use_ssl else "0"
    try:
        completed = _run_powershell(_REMOTE_SQLCMD_SCRIPT, environment, login_timeout_seconds + 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = safe_public_error(str(exc), windows_password)
        return SqlInstanceFacts(endpoint=public_endpoint, error=safe_public_error(detail, password))
    if completed.returncode != 0:
        error = clean_powershell_error(completed.stderr or completed.stdout)
        error = safe_public_error(error, windows_password)
        error = safe_public_error(error, password)
        return SqlInstanceFacts(
            endpoint=public_endpoint,
            error=f"A consulta SQL com a credencial administrativa remota falhou. {error}",
        )
    try:
        payload = _load_json_payload(completed.stdout)
    except (ValueError, TypeError):
        return SqlInstanceFacts(endpoint=public_endpoint, error="A consulta SQL remota retornou uma resposta inválida.")
    output = str(payload.get("output") or "")
    if int(payload.get("exit_code") or 0) != 0:
        return SqlInstanceFacts(
            endpoint=public_endpoint,
            error=_clean_sqlcmd_error(output, authentication, password),
        )
    values = _parse_pairs(output)
    if not values:
        return SqlInstanceFacts(endpoint=public_endpoint, error="SQL Server respondeu sem as configurações esperadas.")
    return SqlInstanceFacts(connected=True, values=values, endpoint=public_endpoint)


def collect_sql_facts(
    host: str,
    instance: DiscoveredInstance,
    authentication: str,
    username: str | None,
    password: str | None,
    login_timeout_seconds: int,
    encrypt_connection: bool,
    trust_server_certificate: bool,
    windows_username: str | None = None,
    windows_password: str | None = None,
    winrm_use_ssl: bool = False,
) -> SqlInstanceFacts:
    endpoint = build_endpoint(host, instance)
    # A credencial administrativa do Windows só é necessária para transportar
    # a autenticação integrada até o servidor. Com login SQL, consultar o
    # endpoint diretamente evita tornar a coleta dependente do WinRM —
    # especialmente importante quando o inventário já caiu no fallback pelas
    # DMVs do SQL Server.
    if (
        authentication == "windows"
        and windows_username
        and windows_password
        and not is_local_target(host)
    ):
        return _collect_sql_facts_with_remote_windows_credential(
            host,
            instance,
            authentication,
            username,
            password,
            windows_username,
            windows_password,
            login_timeout_seconds,
            encrypt_connection,
            trust_server_certificate,
            winrm_use_ssl,
        )
    sqlcmd = _find_sqlcmd()
    if not sqlcmd:
        return SqlInstanceFacts(
            endpoint=endpoint,
            error="sqlcmd não foi encontrado. Instale Microsoft Command Line Utilities for SQL Server.",
        )
    command = [
        sqlcmd,
        "-S",
        endpoint,
        "-d",
        "master",
        "-l",
        str(login_timeout_seconds),
        "-b",
        "-I",
        "-w",
        "65535",
        "-h",
        "-1",
        "-s",
        "|",
        "-Q",
        _BASE_QUERY,
    ]
    environment = os.environ.copy()
    if authentication == "windows":
        command.append("-E")
    else:
        command.extend(["-U", username or ""])
        environment["SQLCMDPASSWORD"] = password or ""
    if encrypt_connection:
        command.append("-N")
    if encrypt_connection and trust_server_certificate:
        command.append("-C")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=login_timeout_seconds + 15,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SqlInstanceFacts(endpoint=endpoint, error=safe_public_error(str(exc), password))
    stdout = _decode_windows_output(completed.stdout)
    stderr = _decode_windows_output(completed.stderr)
    if completed.returncode != 0:
        detail = stderr or stdout or "Falha de conexão sem detalhes."
        return SqlInstanceFacts(endpoint=endpoint, error=_clean_sqlcmd_error(detail, authentication, password))
    values = _parse_pairs(stdout)
    if not values:
        return SqlInstanceFacts(endpoint=endpoint, error="SQL Server respondeu, mas não retornou as configurações esperadas.")
    return SqlInstanceFacts(connected=True, values=values, endpoint=endpoint)
