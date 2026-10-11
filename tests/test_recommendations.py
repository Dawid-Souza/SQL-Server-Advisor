from app.services.discovery import (
    DiscoveredInstance,
    HostInventory,
    clean_powershell_error,
    discover_host,
    parse_sql_browser_response,
)
from app.services.recommendations import build_resource_plan, recommendations_for_instance, recommended_maxdop
from app.services.security import normalize_server_name
from app.models import ScanRequest
from app.services.sqlserver import SqlInstanceFacts, build_endpoint, collect_sql_facts
from subprocess import CompletedProcess
from unittest.mock import patch


def test_resource_plan_preserves_larger_memory_reserve():
    host = HostInventory("DB01", "Windows", "1", 64 * 1024, 42 * 1024, 16)
    instances = [
        DiscoveredInstance("MSSQLSERVER", "MSSQL16.MSSQLSERVER", "MSSQLSERVER", process_memory_mb=12_000),
        DiscoveredInstance("ERP", "MSSQL16.ERP", "MSSQL$ERP", process_memory_mb=4_000),
    ]
    plan = build_resource_plan(host, instances, 25, 4, None)
    assert plan.reserved_memory_mb == 16 * 1024
    assert sum(plan.per_instance_memory_mb) == plan.sql_memory_budget_mb
    assert plan.per_instance_memory_mb[0] > plan.per_instance_memory_mb[1]
    assert plan.reserved_logical_cpus == 4


def test_resource_plan_uses_minimum_reserve_when_larger():
    host = HostInventory("DB01", "Windows", "1", 16 * 1024, 15 * 1024, 8)
    plan = build_resource_plan(host, [DiscoveredInstance("MSSQLSERVER", "x", "MSSQLSERVER")], 10, 4, 2)
    assert plan.reserved_memory_mb == 4 * 1024
    assert plan.sql_cpu_budget == 6


def test_resource_plan_protects_observed_non_sql_usage():
    host = HostInventory("DB01", "Windows", "1", 32 * 1024, 8 * 1024, 8)
    instance = DiscoveredInstance("MSSQLSERVER", "x", "MSSQLSERVER", process_memory_mb=8 * 1024)
    plan = build_resource_plan(host, [instance], 20, 4, None)
    assert plan.observed_non_sql_memory_mb == 16 * 1024
    assert plan.reserved_memory_mb >= 17 * 1024


def test_endpoints_prefer_detected_port():
    default = DiscoveredInstance("MSSQLSERVER", "x", "MSSQLSERVER", tcp_port="1433")
    named = DiscoveredInstance("ERP", "y", "MSSQL$ERP")
    assert build_endpoint("DB01", default) == "tcp:DB01,1433"
    assert build_endpoint("DB01", named) == "DB01\\ERP"


def test_server_name_validation_and_maxdop():
    assert normalize_server_name("db-01.corp.local") == "db-01.corp.local"
    assert recommended_maxdop(16) == 8
    assert recommended_maxdop(2) == 1
    assert recommended_maxdop(32, numa_node_count=2, schedulers_per_numa=20) == 10
    try:
        normalize_server_name("db01; whoami")
    except ValueError:
        pass
    else:
        raise AssertionError("shell metacharacters must be rejected")


def test_connected_instance_gets_explained_scripted_recommendations():
    instance = DiscoveredInstance("ERP", "MSSQL16.ERP", "MSSQL$ERP", service_state="Running")
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01\\ERP",
        values={
            "edition": "Standard Edition",
            "max_server_memory_mb": "2147483647",
            "min_server_memory_mb": "2048",
            "maxdop": "0",
            "cost_threshold": "5",
            "tempdb_data_files": "1",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "64",
            "backup_compression": "0",
            "optimize_ad_hoc": "0",
        },
    )
    items, script, notes = recommendations_for_instance(instance, facts, 16384, 8)
    assert len(items) >= 7
    assert any(item.key == "max_server_memory" and item.priority == "critical" for item in items)
    assert "max server memory (MB)', 16384" in script
    assert notes


def test_tempdb_uses_visible_cpus_and_generates_equivalent_script():
    instance = DiscoveredInstance("DEVBR", "MSSQL15.DEVBR", "MSSQL$DEVBR", service_state="Running")
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01\\DEVBR",
        values={
            "edition": "Standard Edition",
            "max_server_memory_mb": "40960",
            "min_server_memory_mb": "0",
            "maxdop": "6",
            "cost_threshold": "50",
            "tempdb_data_files": "6",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "64",
            "backup_compression": "1",
            "optimize_ad_hoc": "1",
        },
    )

    items, script, _ = recommendations_for_instance(instance, facts, 48_896, 6, 12)

    tempdb = next(item for item in items if item.key == "tempdb")
    assert tempdb.recommended.startswith("8 arquivo(s)")
    assert "DECLARE @tempdbTargetFileCount int = 8;" in script
    assert "ALTER DATABASE [tempdb] ADD FILE" in script
    assert "ALTER DATABASE [tempdb] MODIFY FILE" in script
    assert "FILEGROWTH = '" in script


def test_tempdb_does_not_recommend_removing_existing_files():
    instance = DiscoveredInstance("DEVBR", "MSSQL15.DEVBR", "MSSQL$DEVBR", service_state="Running")
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01\\DEVBR",
        values={
            "tempdb_data_files": "8",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "512",
        },
    )

    items, script, _ = recommendations_for_instance(instance, facts, 16_384, 6, 12)

    tempdb = next(item for item in items if item.key == "tempdb")
    assert tempdb.recommended.startswith("8 arquivo(s)")
    assert "REMOVE FILE" not in script


def test_evidence_turns_ad_hoc_and_backup_checksum_into_actions():
    instance = DiscoveredInstance("ERP", "MSSQL15.ERP", "MSSQL$ERP", service_state="Running")
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01\\ERP",
        values={
            "edition": "Standard Edition",
            "max_server_memory_mb": "16384",
            "min_server_memory_mb": "0",
            "maxdop": "4",
            "cost_threshold": "50",
            "tempdb_data_files": "8",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "512",
            "backup_compression": "1",
            "backup_checksum": "0",
            "optimize_ad_hoc": "0",
            "single_use_adhoc_mb": "600",
            "plan_cache_mb": "1000",
        },
    )

    items, script, _ = recommendations_for_instance(instance, facts, 16_384, 4, 8)

    ad_hoc = next(item for item in items if item.key == "ad_hoc")
    checksum = next(item for item in items if item.key == "backup_checksum")
    assert ad_hoc.change_required is True
    assert "60%" in ad_hoc.current
    assert checksum.change_required is True
    assert "optimize for ad hoc workloads', 1" in script
    assert "backup checksum default', 1" in script


def test_database_findings_have_categories_and_equivalent_scripts():
    instance = DiscoveredInstance("ERP", "MSSQL15.ERP", "MSSQL$ERP", service_state="Running")
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01\\ERP",
        values={
            "max_server_memory_mb": "16384",
            "min_server_memory_mb": "0",
            "maxdop": "4",
            "cost_threshold": "50",
            "tempdb_data_files": "8",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "512",
            "backup_compression": "1",
            "backup_checksum": "1",
            "db_auto_close_count": "1",
            "db_auto_close_names": "[Legado]",
            "db_page_verify_count": "2",
            "db_page_verify_names": "[ERP], [DW]",
            "db_query_store_off_count": "2",
            "db_query_store_off_names": "[ERP], [DW]",
            "db_percent_growth_count": "3",
        },
    )

    items, script, _ = recommendations_for_instance(instance, facts, 16_384, 4, 8)

    page_verify = next(item for item in items if item.key == "database_page_verify")
    query_store = next(item for item in items if item.key == "database_query_store")
    assert page_verify.category == "Backup e integridade"
    assert "[ERP]" in page_verify.current
    assert query_store.change_required is True
    assert "SET PAGE_VERIFY CHECKSUM" in script
    assert "SET QUERY_STORE = ON" in script
    assert "FILEGROWTH = '" in script


def test_dangerous_server_options_are_detected_and_disabled_in_script():
    instance = DiscoveredInstance("ERP", "MSSQL15.ERP", "MSSQL$ERP", service_state="Running")
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01\\ERP",
        values={
            "max_server_memory_mb": "16384",
            "min_server_memory_mb": "0",
            "maxdop": "4",
            "cost_threshold": "50",
            "tempdb_data_files": "8",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "512",
            "backup_compression": "1",
            "backup_checksum": "1",
            "xp_cmdshell": "1",
            "priority_boost": "1",
        },
    )

    items, script, _ = recommendations_for_instance(instance, facts, 16_384, 4, 8)

    xp_cmdshell = next(item for item in items if item.key == "xp_cmdshell")
    assert xp_cmdshell.priority == "critical"
    assert xp_cmdshell.change_required is True
    assert "xp_cmdshell', 0" in script
    assert "priority boost', 0" in script


def test_sql_browser_response_discovers_named_instance_and_port():
    payload = b"ServerName;DB01;InstanceName;ERP;IsClustered;No;Version;16.0;tcp;51433;;"
    response = b"\x05" + len(payload).to_bytes(2, "little") + payload
    instances = parse_sql_browser_response(response)
    assert len(instances) == 1
    assert instances[0].name == "ERP"
    assert instances[0].tcp_port == "51433"
    assert instances[0].service_state == "A verificar"


def test_clixml_error_is_converted_to_readable_message():
    raw = """#< CLIXML
<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04"><S S="Error">Erro de seguran_x00E7_a desconhecido._x000D__x000A_</S></Objs>"""
    assert clean_powershell_error(raw) == "O Windows recusou a autenticação remota via WinRM/Kerberos."


def test_missing_logon_session_error_is_converted_to_actionable_message():
    raw = (
        "Connecting to remote server DB01 failed with the following error message: "
        "A sessão de logon especificada não existe. Talvez ela já tenha sido finalizada."
    )
    message = clean_powershell_error(raw)
    assert "WinRM não conseguiu criar uma sessão" in message
    assert "WinRM HTTPS" in message


def test_explicit_windows_credential_is_required_as_a_pair():
    request = ScanRequest(
        server="DB01",
        windows_authentication="credentials",
        windows_username="DOMINIO\\dba",
        windows_password="segredo",
    )
    assert request.windows_username == "DOMINIO\\dba"
    try:
        ScanRequest(
            server="DB01",
            windows_authentication="credentials",
            windows_username="DOMINIO\\dba",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a senha administrativa deve ser obrigatória")


def test_remote_credentials_are_not_added_to_process_arguments():
    instance = DiscoveredInstance("ERP", "ERP", "MSSQL$ERP", service_state="Running", tcp_port="1433")
    completed = CompletedProcess(
        args=["powershell"],
        returncode=0,
        stdout='{"exit_code":0,"output":"server_name|DB01\\\\ERP\\nhost_total_memory_mb|65536"}',
        stderr="",
    )
    with patch("app.services.sqlserver._run_powershell", return_value=completed) as runner:
        facts = collect_sql_facts(
            "DB01",
            instance,
            "windows",
            None,
            None,
            8,
            True,
            True,
            "DOMINIO\\dba",
            "senha-secreta",
            True,
        )
    script, environment, _ = runner.call_args.args
    assert "senha-secreta" not in script
    assert environment["DTA_WINDOWS_PASSWORD"] == "senha-secreta"
    assert environment["DTA_SQL_PASSWORD"] == ""
    assert environment["DTA_SQL_AUTHENTICATION"] == "windows"
    assert environment["DTA_WINRM_USE_SSL"] == "1"
    assert facts.connected is True


def test_sql_authentication_does_not_depend_on_remote_windows_credential():
    instance = DiscoveredInstance("ERP", "ERP", "MSSQL$ERP", service_state="Running", tcp_port="1433")
    completed = CompletedProcess(
        args=["sqlcmd"],
        returncode=0,
        stdout=b"server_name|DB01\\ERP\nhost_total_memory_mb|65536\n",
        stderr=b"",
    )
    with patch("app.services.sqlserver._find_sqlcmd", return_value="sqlcmd"), patch(
        "app.services.sqlserver.subprocess.run", return_value=completed
    ) as runner, patch(
        "app.services.sqlserver._run_powershell",
        side_effect=AssertionError("login SQL não deve depender do WinRM"),
    ):
        facts = collect_sql_facts(
            "DB01",
            instance,
            "sql",
            "diagnostico",
            "senha-sql",
            8,
            True,
            True,
            "DB01\\Administrator",
            "senha-windows",
            True,
        )

    command = runner.call_args.args[0]
    environment = runner.call_args.kwargs["env"]
    assert "senha-sql" not in command
    assert "senha-windows" not in command
    assert environment["SQLCMDPASSWORD"] == "senha-sql"
    assert facts.connected is True


def test_winrm_https_is_selected_without_changing_local_machine_configuration():
    completed = CompletedProcess(
        args=["powershell"],
        returncode=0,
        stdout=(
            '{"computer_name":"DB01","os_caption":"Windows Server","os_version":"2022",'
            '"total_memory_mb":32768,"free_memory_mb":12000,"logical_cpus":8,"instances":[]}'
        ),
        stderr="",
    )
    with patch("app.services.discovery._run_powershell", return_value=completed) as runner:
        inventory = discover_host("DB01", 35, "DB01\\Administrator", "segredo", True)

    script, environment, _ = runner.call_args.args
    assert environment["DTA_WINRM_USE_SSL"] == "1"
    assert environment["DTA_WINDOWS_PASSWORD"] == "segredo"
    assert "segredo" not in script
    assert inventory.computer_name == "DB01"
