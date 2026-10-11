import asyncio
from unittest.mock import patch

from app.main import import_snapshot, scan
from app.models import ImportSnapshotRequest, ScanRequest
from app.services.discovery import DiscoveredInstance, HostInventory
from app.services.sqlserver import SqlInstanceFacts


def test_sql_fallback_enriches_missing_windows_hardware():
    inventory = HostInventory(
        computer_name="DB01",
        os_caption="Windows Server (inventário parcial)",
        os_version="indisponível",
        total_memory_mb=0,
        free_memory_mb=0,
        logical_cpus=1,
        instances=[
            DiscoveredInstance(
                "ERP",
                "ERP",
                "MSSQL$ERP",
                service_state="A verificar",
                tcp_port="1433",
            )
        ],
        discovery_method="sql-browser",
        warnings=["fallback ativo"],
    )
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="tcp:DB01,1433",
        values={
            "server_name": "DB01\\ERP",
            "host_machine_name": "DB01",
            "host_total_memory_mb": "65536",
            "host_free_memory_mb": "16384",
            "host_logical_cpus": "16",
            "sql_process_memory_mb": "24576",
            "edition": "Standard Edition",
            "product_version": "16.0",
            "product_level": "CU",
            "max_server_memory_mb": "50000",
            "min_server_memory_mb": "0",
            "maxdop": "0",
            "cost_threshold": "5",
            "tempdb_data_files": "8",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "512",
            "backup_compression": "1",
            "optimize_ad_hoc": "0",
        },
    )
    request = ScanRequest(server="DB01", authentication="windows")
    with patch("app.main.discover_host", return_value=inventory), patch(
        "app.main.collect_sql_facts", return_value=facts
    ):
        response = asyncio.run(scan(request))

    assert response.host.total_memory_mb == 65536
    assert response.host.logical_cpus == 16
    assert response.instances[0].connected is True
    assert response.instances[0].service_state == "Running"
    assert "fallback ativo" in response.warnings


def test_admin_windows_credential_reaches_inventory_and_sql_without_response_leak():
    inventory = HostInventory(
        "DB01",
        "Windows Server",
        "2022",
        32768,
        16384,
        8,
        [DiscoveredInstance("MSSQLSERVER", "MSSQL16", "MSSQLSERVER", service_state="Running")],
    )
    facts = SqlInstanceFacts(
        connected=True,
        endpoint="DB01",
        values={
            "server_name": "DB01",
            "max_server_memory_mb": "20000",
            "min_server_memory_mb": "0",
            "maxdop": "4",
            "cost_threshold": "50",
            "tempdb_data_files": "4",
            "tempdb_equal_sizes": "1",
            "tempdb_min_growth_mb": "512",
            "backup_compression": "1",
            "optimize_ad_hoc": "0",
        },
    )
    request = ScanRequest(
        server="DB01",
        windows_authentication="credentials",
        windows_username="DOMINIO\\dba",
        windows_password="senha-temporaria",
        authentication="windows",
    )
    with patch("app.main.discover_host", return_value=inventory) as discover, patch(
        "app.main.collect_sql_facts", return_value=facts
    ) as sql:
        response = asyncio.run(scan(request))

    assert discover.call_args.args[-3:] == ("DOMINIO\\dba", "senha-temporaria", False)
    assert sql.call_args.args[-3:] == ("DOMINIO\\dba", "senha-temporaria", False)
    assert "senha-temporaria" not in response.model_dump_json()


def test_rdp_snapshot_is_analyzed_without_remote_or_local_admin_calls():
    payload = ImportSnapshotRequest.model_validate(
        {
            "snapshot": {
                "schema_version": 1,
                "generated_at": "2026-09-11T10:30:00-03:00",
                "host": {
                    "computer_name": "DB-RDP-01",
                    "os_caption": "Microsoft Windows Server 2022 Standard",
                    "os_version": "10.0.20348",
                    "total_memory_mb": 65536,
                    "free_memory_mb": 16384,
                    "logical_cpus": 16,
                },
                "instances": [
                    {
                        "name": "ERP",
                        "instance_id": "MSSQL16.ERP",
                        "service_name": "MSSQL$ERP",
                        "service_state": "Running",
                        "start_mode": "Auto",
                        "tcp_port": "51433",
                        "process_memory_mb": 24576,
                        "facts": {
                            "connected": True,
                            "endpoint": "tcp:DB-RDP-01,51433",
                            "values": {
                                "edition": "Standard Edition",
                                "product_version": "16.0",
                                "max_server_memory_mb": "50000",
                                "min_server_memory_mb": "0",
                                "maxdop": "0",
                                "cost_threshold": "5",
                                "tempdb_data_files": "4",
                                "tempdb_equal_sizes": "1",
                                "tempdb_min_growth_mb": "512",
                                "backup_compression": "1",
                                "optimize_ad_hoc": "0",
                            },
                        },
                    }
                ],
                "warnings": [],
            }
        }
    )

    with patch("app.main.discover_host", side_effect=AssertionError("não deve haver acesso remoto")):
        response = asyncio.run(import_snapshot(payload))

    assert response.scanned_at == "2026-09-11T10:30:00-03:00"
    assert response.host.computer_name == "DB-RDP-01"
    assert response.host.reserved_memory_mb >= 16 * 1024
    assert response.instances[0].endpoint == "tcp:DB-RDP-01,51433"
    assert response.instances[0].recommendations
    assert any("sessão RDP" in warning for warning in response.warnings)


def test_rdp_snapshot_rejects_unknown_schema_version():
    try:
        ImportSnapshotRequest.model_validate(
            {
                "snapshot": {
                    "schema_version": 2,
                    "generated_at": "2026-09-11T10:30:00-03:00",
                    "host": {
                        "computer_name": "DB01",
                        "total_memory_mb": 32768,
                        "free_memory_mb": 10000,
                        "logical_cpus": 8,
                    },
                    "instances": [],
                }
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("versões desconhecidas do diagnóstico devem ser rejeitadas")
