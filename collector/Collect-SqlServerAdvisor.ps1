#Requires -Version 5.1
<#
.SYNOPSIS
Coleta um diagnóstico somente leitura para o SQL Server Advisor.

.DESCRIPTION
Execute dentro de uma sessão RDP no Windows Server, em um PowerShell aberto
como Administrador. O arquivo JSON gerado não contém senhas.

.EXAMPLE
.\Collect-SqlServerAdvisor.ps1

.EXAMPLE
.\Collect-SqlServerAdvisor.ps1 -UseSqlAuthentication

.EXAMPLE
.\Collect-SqlServerAdvisor.ps1 -DisableEncryption
#>
[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$UseSqlAuthentication,
    [switch]$DisableEncryption
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    throw 'Abra o PowerShell com Executar como administrador e execute o coletor novamente.'
}

if (-not $OutputPath) {
    $desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputPath = Join-Path $desktop "sql-advisor-$($env:COMPUTERNAME)-$timestamp.json"
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    throw "A pasta de destino não existe: $outputDirectory"
}

$os = Get-CimInstance Win32_OperatingSystem
$computerSystem = Get-CimInstance Win32_ComputerSystem
$instanceMap = @{}
foreach ($registryPath in @(
    'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Microsoft SQL Server\Instance Names\SQL'
)) {
    if (Test-Path $registryPath) {
        $registryItem = Get-ItemProperty $registryPath
        foreach ($property in $registryItem.PSObject.Properties) {
            if ($property.Name -notmatch '^PS') {
                $instanceMap[$property.Name] = [string]$property.Value
            }
        }
    }
}

$sqlServices = @(Get-CimInstance Win32_Service | Where-Object {
    $_.Name -eq 'MSSQLSERVER' -or $_.Name -like 'MSSQL$*'
})
foreach ($service in $sqlServices) {
    $inferredName = if ($service.Name -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { $service.Name.Substring(6) }
    if (-not $instanceMap.ContainsKey($inferredName)) {
        $instanceMap[$inferredName] = $inferredName
    }
}

$sqlcmdCommand = Get-Command sqlcmd -ErrorAction SilentlyContinue
if (-not $sqlcmdCommand) {
    $sqlcmdCommand = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\*\Tools\Binn\SQLCMD.EXE' -File -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
}
$sqlcmdPath = if ($sqlcmdCommand) {
    if ($sqlcmdCommand.Source) { $sqlcmdCommand.Source } else { $sqlcmdCommand.FullName }
} else {
    $null
}

$sqlCredential = $null
if ($UseSqlAuthentication) {
    $sqlCredential = Get-Credential -Message 'Informe o login do SQL Server (não a conta do Windows)'
}

$query = @'
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
    SELECT 'host_free_memory_mb', CONVERT(nvarchar(4000), available_physical_memory_kb / 1024) FROM sys.dm_os_sys_memory;
    SELECT 'sql_process_memory_mb', CONVERT(nvarchar(4000), physical_memory_in_use_kb / 1024) FROM sys.dm_os_process_memory;
    SELECT 'locked_pages_mb', CONVERT(nvarchar(4000), locked_page_allocations_kb / 1024) FROM sys.dm_os_process_memory;
    SELECT 'numa_node_count', CONVERT(nvarchar(4000), COUNT(DISTINCT parent_node_id)) FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE' AND scheduler_id < 1048576;
    SELECT 'max_schedulers_per_numa', CONVERT(nvarchar(4000), MAX(scheduler_count)) FROM (SELECT parent_node_id, COUNT(*) AS scheduler_count FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE' AND scheduler_id < 1048576 GROUP BY parent_node_id) AS numa;
    SELECT 'single_use_adhoc_mb', CONVERT(nvarchar(4000), COALESCE(SUM(CONVERT(bigint, size_in_bytes)) / 1048576, 0)) FROM sys.dm_exec_cached_plans WHERE objtype = 'Adhoc' AND usecounts = 1;
    SELECT 'plan_cache_mb', CONVERT(nvarchar(4000), COALESCE(SUM(CONVERT(bigint, size_in_bytes)) / 1048576, 0)) FROM sys.dm_exec_cached_plans;
    SELECT TOP (1) 'ifi_enabled', CONVERT(nvarchar(4000), instant_file_initialization_enabled) FROM sys.dm_server_services WHERE servicename LIKE 'SQL Server (%' OR servicename = 'SQL Server';
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
    SELECT 'db_full_backup_overdue_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases AS d OUTER APPLY (SELECT MAX(backup_finish_date) AS last_backup FROM msdb.dbo.backupset WHERE database_name = d.name AND type = 'D' AND is_copy_only = 0) AS b WHERE d.database_id > 4 AND d.state_desc = 'ONLINE' AND d.source_database_id IS NULL AND (b.last_backup IS NULL OR b.last_backup < DATEADD(day, -7, GETDATE()));
    SELECT 'db_log_backup_overdue_count', CONVERT(nvarchar(4000), COUNT(*)) FROM sys.databases AS d OUTER APPLY (SELECT MAX(backup_finish_date) AS last_backup FROM msdb.dbo.backupset WHERE database_name = d.name AND type = 'L') AS b WHERE d.database_id > 4 AND d.state_desc = 'ONLINE' AND d.source_database_id IS NULL AND d.recovery_model_desc IN ('FULL', 'BULK_LOGGED') AND (b.last_backup IS NULL OR b.last_backup < DATEADD(hour, -24, GETDATE()));
    SELECT 'suspect_pages_count', CONVERT(nvarchar(4000), COUNT(*)) FROM msdb.dbo.suspect_pages WHERE event_type IN (1, 2, 3);
END TRY
BEGIN CATCH
    SELECT 'operational_metrics_error', CONVERT(nvarchar(4000), ERROR_MESSAGE());
END CATCH;
'@

$instances = @()
$warnings = @()
foreach ($entry in $instanceMap.GetEnumerator() | Sort-Object Name) {
    $instanceName = [string]$entry.Name
    $instanceId = [string]$entry.Value
    $serviceName = if ($instanceName -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { 'MSSQL$' + $instanceName }
    $service = $sqlServices | Where-Object Name -eq $serviceName | Select-Object -First 1
    $tcpPort = $null
    $dynamicPort = $null
    foreach ($tcpPath in @(
        "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\$instanceId\MSSQLServer\SuperSocketNetLib\Tcp\IPAll",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Microsoft SQL Server\$instanceId\MSSQLServer\SuperSocketNetLib\Tcp\IPAll"
    )) {
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

    $publicEndpoint = if ($tcpPort -and [int]$tcpPort -gt 0) {
        "tcp:$($env:COMPUTERNAME),$tcpPort"
    } elseif ($dynamicPort -and [int]$dynamicPort -gt 0) {
        "tcp:$($env:COMPUTERNAME),$dynamicPort"
    } elseif ($instanceName -eq 'MSSQLSERVER') {
        $env:COMPUTERNAME
    } else {
        "$($env:COMPUTERNAME)\$instanceName"
    }
    $localEndpoint = if ($tcpPort -and [int]$tcpPort -gt 0) {
        "tcp:127.0.0.1,$tcpPort"
    } elseif ($dynamicPort -and [int]$dynamicPort -gt 0) {
        "tcp:127.0.0.1,$dynamicPort"
    } elseif ($instanceName -eq 'MSSQLSERVER') {
        '.'
    } else {
        ".\$instanceName"
    }

    $facts = [ordered]@{ connected = $false; endpoint = $publicEndpoint; error = $null; values = [ordered]@{} }
    if (-not $service -or $service.State -ne 'Running') {
        $facts.error = 'O serviço da instância está parado.'
    } elseif (-not $sqlcmdPath) {
        $facts.error = 'sqlcmd não foi encontrado no servidor.'
    } else {
        $arguments = @('-S', $localEndpoint, '-d', 'master', '-l', '12', '-b', '-I', '-w', '65535', '-h', '-1', '-s', '|', '-Q', $query)
        if ($sqlCredential) {
            $arguments += @('-U', $sqlCredential.UserName)
            $env:SQLCMDPASSWORD = $sqlCredential.GetNetworkCredential().Password
        } else {
            $arguments += '-E'
        }
        if (-not $DisableEncryption) { $arguments += @('-N', '-C') }
        try {
            $sqlOutput = & $sqlcmdPath @arguments 2>&1 | Out-String
            $sqlExitCode = $LASTEXITCODE
        } finally {
            Remove-Item Env:\SQLCMDPASSWORD -ErrorAction SilentlyContinue
        }
        if ($sqlExitCode -ne 0) {
            $facts.error = ($sqlOutput.Trim() -replace "`r?`n", ' ')
            $warnings += "A instância $instanceName não aceitou a consulta SQL."
        } else {
            foreach ($line in ($sqlOutput -split "`r?`n")) {
                $separator = $line.IndexOf('|')
                if ($separator -gt 0) {
                    $key = $line.Substring(0, $separator).Trim()
                    $value = $line.Substring($separator + 1).Trim()
                    if ($key) { $facts.values[$key] = $value }
                }
            }
            $facts.connected = $facts.values.Count -gt 0
            if (-not $facts.connected) { $facts.error = 'SQL Server respondeu sem as configurações esperadas.' }
        }
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
        facts = $facts
    }
}

$snapshot = [ordered]@{
    schema_version = 1
    generated_at = [DateTimeOffset]::Now.ToString('o')
    host = [ordered]@{
        computer_name = [string]$env:COMPUTERNAME
        os_caption = [string]$os.Caption
        os_version = [string]$os.Version
        total_memory_mb = [int][math]::Round([double]$computerSystem.TotalPhysicalMemory / 1MB)
        free_memory_mb = [int][math]::Round([double]$os.FreePhysicalMemory / 1KB)
        logical_cpus = [int]$computerSystem.NumberOfLogicalProcessors
    }
    instances = @($instances)
    warnings = @($warnings)
}

$json = $snapshot | ConvertTo-Json -Depth 9
$utf8WithoutBom = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText($OutputPath, $json, $utf8WithoutBom)
Write-Host "Diagnóstico criado: $OutputPath" -ForegroundColor Green
Write-Host 'Volte ao SQL Server Advisor e importe esse arquivo.' -ForegroundColor Cyan
