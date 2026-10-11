from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.models import HostSummary, ImportSnapshotRequest, InstanceReport, ScanRequest, ScanResponse
from app.services.discovery import DiscoveredInstance, DiscoveryError, HostInventory, discover_host
from app.services.recommendations import build_resource_plan, recommendations_for_instance
from app.services.security import safe_public_error
from app.services.sqlserver import SqlInstanceFacts, collect_sql_facts


BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(
    title="SQL Server Advisor",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def private_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "default_server": os.environ.get("COMPUTERNAME", socket.gethostname()),
            "default_windows_username": (
                f"{os.environ.get('USERDOMAIN')}\\{os.environ.get('USERNAME')}"
                if os.environ.get("USERDOMAIN") and os.environ.get("USERNAME")
                else os.environ.get("USERNAME", "")
            ),
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/collector/Collect-SqlServerAdvisor.ps1", response_class=FileResponse)
async def download_collector() -> FileResponse:
    collector_path = BASE_DIR.parent / "collector" / "Collect-SqlServerAdvisor.ps1"
    return FileResponse(
        collector_path,
        media_type="text/plain; charset=utf-8",
        filename="Collect-SqlServerAdvisor.ps1",
    )


@app.get("/collector/Configure-SqlAdvisorWinRMHttps.ps1", response_class=FileResponse)
async def download_winrm_configurator() -> FileResponse:
    configurator_path = BASE_DIR.parent / "collector" / "Configure-SqlAdvisorWinRMHttps.ps1"
    return FileResponse(
        configurator_path,
        media_type="text/plain; charset=utf-8",
        filename="Configure-SqlAdvisorWinRMHttps.ps1",
    )


@app.post("/api/import", response_model=ScanResponse)
async def import_snapshot(payload: ImportSnapshotRequest) -> ScanResponse:
    snapshot = payload.snapshot
    instances = [
        DiscoveredInstance(
            name=item.name,
            instance_id=item.instance_id,
            service_name=item.service_name,
            service_state=item.service_state,
            start_mode=item.start_mode,
            tcp_port=item.tcp_port,
            dynamic_port=item.dynamic_port,
            process_memory_mb=item.process_memory_mb,
        )
        for item in snapshot.instances
    ]
    facts = [
        SqlInstanceFacts(
            connected=item.facts.connected,
            endpoint=item.facts.endpoint,
            error=item.facts.error,
            values=item.facts.values,
        )
        for item in snapshot.instances
    ]
    inventory = HostInventory(
        computer_name=snapshot.host.computer_name,
        os_caption=snapshot.host.os_caption,
        os_version=snapshot.host.os_version,
        total_memory_mb=snapshot.host.total_memory_mb,
        free_memory_mb=snapshot.host.free_memory_mb,
        logical_cpus=snapshot.host.logical_cpus,
        instances=instances,
        discovery_method="rdp-snapshot",
        warnings=list(snapshot.warnings)
        + [f"Diagnóstico coletado dentro da sessão RDP em {snapshot.generated_at}."],
    )
    return _analyze_inventory(
        inventory,
        facts,
        payload.reserve_memory_percent,
        payload.reserve_memory_gb,
        payload.reserve_logical_cpus,
        scanned_at=snapshot.generated_at,
    )


@app.post("/api/scan", response_model=ScanResponse)
async def scan(payload: ScanRequest) -> ScanResponse:
    try:
        inventory = await asyncio.to_thread(
            discover_host,
            payload.server,
            35,
            payload.windows_username,
            payload.windows_password,
            payload.winrm_use_ssl,
        )
    except DiscoveryError as exc:
        detail = safe_public_error(str(exc), payload.password)
        detail = safe_public_error(detail, payload.windows_password)
        raise HTTPException(status_code=422, detail=detail) from exc

    instances = inventory.instances
    async def collect(instance) -> SqlInstanceFacts:
        if instance.service_state.lower() in {"stopped", "not found", "disabled"}:
            return SqlInstanceFacts(endpoint="", error="O serviço da instância está parado.")
        return await asyncio.to_thread(
            collect_sql_facts,
            payload.server,
            instance,
            payload.authentication,
            payload.username,
            payload.password,
            payload.login_timeout_seconds,
            payload.encrypt_connection,
            payload.trust_server_certificate,
            payload.windows_username,
            payload.windows_password,
            payload.winrm_use_ssl,
        )
    facts = await asyncio.gather(*(collect(instance) for instance in instances))
    return _analyze_inventory(
        inventory,
        facts,
        payload.reserve_memory_percent,
        payload.reserve_memory_gb,
        payload.reserve_logical_cpus,
        secrets=(payload.password, payload.windows_password),
    )


def _analyze_inventory(
    inventory: HostInventory,
    facts: list[SqlInstanceFacts],
    reserve_memory_percent: int,
    reserve_memory_gb: int,
    reserve_logical_cpus: int | None,
    secrets: tuple[str | None, ...] = (),
    scanned_at: str | None = None,
) -> ScanResponse:
    instances = inventory.instances
    if len(instances) != len(facts):
        raise HTTPException(status_code=422, detail="O diagnóstico contém uma quantidade inconsistente de instâncias.")

    for instance, sql_facts in zip(instances, facts, strict=True):
        if not sql_facts.connected:
            continue
        sql_memory = sql_facts.integer("sql_process_memory_mb", 0)
        if sql_memory > 0:
            instance.process_memory_mb = sql_memory
        actual_server_name = sql_facts.values.get("server_name", "")
        if "\\" in actual_server_name and instance.instance_id == "MSSQLSERVER":
            instance.name = actual_server_name.rsplit("\\", 1)[1]
            instance.service_name = f"MSSQL${instance.name}"
        if inventory.total_memory_mb <= 0:
            inventory.total_memory_mb = sql_facts.integer("host_total_memory_mb", 0)
        if inventory.free_memory_mb <= 0:
            inventory.free_memory_mb = sql_facts.integer("host_free_memory_mb", 0)
        if inventory.logical_cpus <= 1:
            inventory.logical_cpus = max(1, sql_facts.integer("host_logical_cpus", 1))
        machine_name = sql_facts.values.get("host_machine_name")
        if machine_name and inventory.discovery_method.startswith("sql"):
            inventory.computer_name = machine_name

    if inventory.total_memory_mb <= 0:
        sql_errors = [fact.error for fact in facts if fact.error]
        detail = (
            "O Windows não autorizou o inventário remoto e não foi possível obter o hardware pelas DMVs do SQL Server. "
            "Use autenticação SQL com um login autorizado e conceda VIEW SERVER STATE "
            "(VIEW SERVER PERFORMANCE STATE no SQL Server 2022+), ou libere o acesso remoto do Windows para a conta do painel."
        )
        if sql_errors:
            sql_detail = sql_errors[0]
            for secret in secrets:
                sql_detail = safe_public_error(sql_detail, secret)
            detail += f" Primeira tentativa SQL: {sql_detail}"
        raise HTTPException(status_code=422, detail=detail)

    if inventory.discovery_method.startswith("sql"):
        inventory.os_caption = "Windows Server (hardware lido pelo SQL Server)"
        inventory.os_version = "Inventário administrativo do Windows não autorizado"

    plan = build_resource_plan(
        inventory,
        instances,
        reserve_memory_percent,
        reserve_memory_gb,
        reserve_logical_cpus,
    )

    cpu_shares = _allocate_cpu(plan.sql_cpu_budget, len(instances))
    reports: list[InstanceReport] = []
    for index, (instance, sql_facts) in enumerate(zip(instances, facts, strict=True)):
        memory = plan.per_instance_memory_mb[index]
        items, script, notes = recommendations_for_instance(
            instance,
            sql_facts,
            memory,
            cpu_shares[index],
            inventory.logical_cpus,
        )
        endpoint = sql_facts.endpoint or (
            inventory.computer_name
            if instance.name.upper() == "MSSQLSERVER"
            else f"{inventory.computer_name}\\{instance.name}"
        )
        service_state = instance.service_state
        if service_state.lower() == "a verificar":
            service_state = "Running" if sql_facts.connected else "Não verificado"
        reports.append(
            InstanceReport(
                instance_name=instance.name,
                endpoint=endpoint,
                service_state=service_state,
                service_start_mode=instance.start_mode,
                version=sql_facts.values.get("product_version"),
                edition=sql_facts.values.get("edition"),
                product_level=sql_facts.values.get("product_level"),
                connected=sql_facts.connected,
                connection_error=sql_facts.error,
                recommendations=items,
                script=script,
                notes=notes,
            )
        )

    warnings: list[str] = list(inventory.warnings)
    if plan.sql_memory_budget_mb < len(instances) * 2048:
        warnings.append("O orçamento de memória deixa menos de 2 GB por instância; considere consolidar ou ampliar o servidor.")
    if inventory.free_memory_mb > 0 and inventory.free_memory_mb < plan.reserved_memory_mb // 2:
        warnings.append("A memória livre atual está abaixo de metade da reserva planejada; investigue pressão de memória antes de aplicar mudanças.")
    if any(not report.connected and report.service_state.lower() in {"running", "não verificado"} for report in reports):
        warnings.append("Uma ou mais instâncias em execução não aceitaram a conexão SQL; as recomendações delas ficaram incompletas.")

    return ScanResponse(
        scanned_at=scanned_at or datetime.now().astimezone().isoformat(),
        host=HostSummary(
            computer_name=inventory.computer_name,
            os_caption=inventory.os_caption,
            os_version=inventory.os_version,
            total_memory_mb=inventory.total_memory_mb,
            free_memory_mb=inventory.free_memory_mb,
            logical_cpus=inventory.logical_cpus,
            sql_memory_budget_mb=plan.sql_memory_budget_mb,
            reserved_memory_mb=plan.reserved_memory_mb,
            observed_non_sql_memory_mb=plan.observed_non_sql_memory_mb,
            sql_cpu_budget=plan.sql_cpu_budget,
            reserved_logical_cpus=plan.reserved_logical_cpus,
        ),
        instances=reports,
        warnings=warnings,
        methodology=_methodology(reserve_memory_percent, reserve_memory_gb),
    )


def _allocate_cpu(sql_budget: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base, remainder = divmod(max(1, sql_budget), count)
    return [max(1, base + (1 if index < remainder else 0)) for index in range(count)]


def _methodology(reserve_memory_percent: int, reserve_memory_gb: int) -> list[str]:
    return [
        f"Reserva do Windows: o maior valor entre {reserve_memory_percent}% da RAM e {reserve_memory_gb} GB.",
        "Se o consumo não-SQL observado mais 5% de folga for maior, ele prevalece para proteger os serviços já ativos.",
        "O restante da memória é dividido entre as instâncias conforme o consumo observado, com arredondamento conservador.",
        "Por padrão, 20% dos processadores lógicos (mínimo de um) ficam fora da parcela usada para dimensionar o SQL Server.",
        "As regras de instância e banco comparam a configuração coletada com práticas documentadas pela Microsoft e só marcam ação quando existe uma não conformidade observável.",
        "Os limites de 7 dias para backup completo e 24 horas para log são alertas conservadores; substitua-os pelos RPO/RTO oficiais do ambiente.",
        "As sugestões são baselines seguras, não mudanças automáticas; valide-as com uma janela representativa de monitoramento.",
    ]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=9000, reload=False)
