from __future__ import annotations

import math
from dataclasses import dataclass

from app.models import RecommendationItem
from app.services.discovery import DiscoveredInstance, HostInventory
from app.services.sqlserver import SqlInstanceFacts


@dataclass(frozen=True)
class ResourcePlan:
    reserved_memory_mb: int
    observed_non_sql_memory_mb: int
    sql_memory_budget_mb: int
    reserved_logical_cpus: int
    sql_cpu_budget: int
    per_instance_memory_mb: list[int]


def _round_down(value: int, unit: int = 256) -> int:
    return max(unit, value // unit * unit)


def build_resource_plan(
    host: HostInventory,
    instances: list[DiscoveredInstance],
    reserve_memory_percent: int,
    reserve_memory_gb: int,
    reserve_logical_cpus: int | None,
) -> ResourcePlan:
    total_mb = max(host.total_memory_mb, 2048)
    requested_reserve = max(reserve_memory_gb * 1024, math.ceil(total_mb * reserve_memory_percent / 100))
    sql_working_set_mb = sum(item.process_memory_mb for item in instances)
    observed_non_sql_mb = max(0, total_mb - host.free_memory_mb - sql_working_set_mb)
    active_services_reserve = observed_non_sql_mb + max(1024, math.ceil(total_mb * 0.05))
    requested_reserve = max(requested_reserve, active_services_reserve)
    # Retain a usable SQL share even on undersized lab hosts without exceeding physical RAM.
    minimum_sql_mb = min(2048, max(512, total_mb // 3))
    reserved_memory_mb = min(requested_reserve, max(512, total_mb - minimum_sql_mb))
    sql_budget = _round_down(max(256, total_mb - reserved_memory_mb))

    cpu_reserve = reserve_logical_cpus
    if cpu_reserve is None:
        cpu_reserve = max(1, math.ceil(host.logical_cpus * 0.20))
    cpu_reserve = min(cpu_reserve, max(1, host.logical_cpus - 1))
    sql_cpu_budget = max(1, host.logical_cpus - cpu_reserve)

    count = max(1, len(instances))
    weights = [max(1024, item.process_memory_mb) for item in instances] or [1]
    weight_sum = sum(weights)
    allocations: list[int] = []
    remaining = sql_budget
    for index, weight in enumerate(weights):
        if index == count - 1:
            allocation = remaining
        else:
            allocation = _round_down(int(sql_budget * weight / weight_sum))
            allocation = min(allocation, max(256, remaining - (count - index - 1) * 256))
        allocation = max(256, allocation)
        allocations.append(allocation)
        remaining -= allocation

    return ResourcePlan(
        reserved_memory_mb=reserved_memory_mb,
        observed_non_sql_memory_mb=observed_non_sql_mb,
        sql_memory_budget_mb=sql_budget,
        reserved_logical_cpus=cpu_reserve,
        sql_cpu_budget=sql_cpu_budget,
        per_instance_memory_mb=allocations,
    )


def recommended_maxdop(
    cpu_share: int,
    numa_node_count: int = 1,
    schedulers_per_numa: int | None = None,
) -> int:
    visible_per_node = max(1, schedulers_per_numa or cpu_share)
    if numa_node_count <= 1:
        numa_cap = visible_per_node if visible_per_node <= 8 else 8
    elif visible_per_node <= 16:
        numa_cap = visible_per_node
    else:
        numa_cap = min(16, max(1, visible_per_node // 2))
    target = min(max(1, cpu_share), numa_cap)
    return 1 if target <= 2 else target


def recommendations_for_instance(
    instance: DiscoveredInstance,
    facts: SqlInstanceFacts,
    memory_mb: int,
    cpu_share: int,
    host_logical_cpus: int | None = None,
) -> tuple[list[RecommendationItem], str | None, list[str]]:
    if not facts.connected:
        return [], None, [
            "A instância foi descoberta no Windows, mas as configurações internas não puderam ser lidas.",
            "Conceda CONNECT SQL e VIEW SERVER STATE à conta de diagnóstico para completar a análise.",
        ]

    items: list[RecommendationItem] = []
    current_memory = facts.integer("max_server_memory_mb", 2147483647)
    memory_changed = abs(current_memory - memory_mb) >= 256
    items.append(
        RecommendationItem(
            key="max_server_memory",
            category="Memória e CPU",
            label="Memória máxima do SQL Server",
            current="Sem limite prático" if current_memory >= 2147483647 else f"{current_memory:,} MB".replace(",", "."),
            recommended=f"{memory_mb:,} MB".replace(",", "."),
            rationale="Limita o buffer pool desta instância à sua parcela do orçamento, preservando a reserva do Windows e dos demais serviços. Reavalie após medir o pico real da carga.",
            priority="critical" if current_memory >= 2147483647 else "high",
            change_required=memory_changed,
            reference="https://learn.microsoft.com/sql/database-engine/configure-windows/server-memory-server-configuration-options",
        )
    )

    current_min = facts.integer("min_server_memory_mb", 0)
    items.append(
        RecommendationItem(
            key="min_server_memory",
            category="Memória e CPU",
            label="Memória mínima do SQL Server",
            current=f"{current_min:,} MB".replace(",", "."),
            recommended="0 MB",
            rationale="Manter zero permite que a instância devolva memória sob pressão; isso é importante quando o servidor hospeda outros serviços ou várias instâncias.",
            priority="medium" if current_min else "info",
            change_required=current_min != 0,
            reference="https://learn.microsoft.com/sql/database-engine/configure-windows/server-memory-server-configuration-options",
        )
    )

    numa_nodes = facts.integer("numa_node_count", 1)
    schedulers_per_numa = facts.integer("max_schedulers_per_numa", cpu_share)
    maxdop = recommended_maxdop(cpu_share, numa_nodes, schedulers_per_numa)
    current_maxdop = facts.integer("maxdop", 0)
    items.append(
        RecommendationItem(
            key="maxdop",
            category="Memória e CPU",
            label="MAXDOP",
            current="0 (todos os CPUs disponíveis)" if current_maxdop == 0 else str(current_maxdop),
            recommended=str(maxdop),
            rationale=f"Usa no máximo {maxdop} processadores por operador, considerando a parcela estimada de {cpu_share} CPU(s) e a topologia observada de {numa_nodes} nó(s) NUMA. MAXDOP não limita o uso global de CPU.",
            priority="high" if current_maxdop == 0 or current_maxdop > maxdop else "medium",
            change_required=current_maxdop != maxdop,
            reference="https://learn.microsoft.com/sql/database-engine/configure-windows/configure-the-max-degree-of-parallelism-server-configuration-option",
        )
    )

    threshold = facts.integer("cost_threshold", 5)
    items.append(
        RecommendationItem(
            key="cost_threshold",
            category="Memória e CPU",
            label="Custo mínimo para paralelismo",
            current=str(threshold),
            recommended="50 (ponto de partida)",
            rationale="Evita gastar vários processadores em consultas pequenas. O valor deve ser refinado com Query Store e tempos de espera após um ciclo representativo de carga.",
            priority="high" if threshold <= 5 else "medium",
            change_required=threshold != 50,
        )
    )

    tempdb_current = facts.integer("tempdb_data_files", 1)
    # MAXDOP is a per-operator limit, not CPU affinity. The instance can still
    # schedule work on every logical CPU visible to SQL Server, so sizing
    # tempdb from the estimated CPU share could incorrectly recommend removing
    # healthy files (for example, 8 -> 6 on a 12-CPU host).
    visible_cpus = host_logical_cpus if host_logical_cpus is not None else cpu_share
    initial_tempdb_target = min(8, max(1, visible_cpus))
    # Removing tempdb files is disruptive and should be justified by measured
    # workload evidence. Preserve an existing count above the initial target.
    tempdb_target = max(tempdb_current, initial_tempdb_target)
    equal_sizes = facts.integer("tempdb_equal_sizes", 0) == 1
    items.append(
        RecommendationItem(
            key="tempdb",
            category="tempdb e arquivos",
            label="Arquivos de dados do tempdb",
            current=f"{tempdb_current} arquivo(s)" + (", tamanhos iguais" if equal_sizes else ", tamanhos diferentes"),
            recommended=f"{tempdb_target} arquivo(s), mesmo tamanho e crescimento fixo",
            rationale="Usa até 8 arquivos como baseline conforme os processadores lógicos visíveis ao SQL Server. Arquivos existentes não são removidos automaticamente; aumente além disso somente com evidência de contenção PAGELATCH no tempdb.",
            priority="high" if tempdb_current != tempdb_target or not equal_sizes else "info",
            change_required=tempdb_current != tempdb_target or not equal_sizes,
            reference="https://learn.microsoft.com/sql/relational-databases/databases/tempdb-database",
        )
    )

    growth = facts.integer("tempdb_min_growth_mb", 0)
    items.append(
        RecommendationItem(
            key="tempdb_growth",
            category="tempdb e arquivos",
            label="Crescimento do tempdb",
            current="Percentual ou não identificado" if growth <= 0 else f"{growth} MB",
            recommended="512 MB fixos por arquivo",
            rationale="Crescimentos fixos e iguais tornam pausas previsíveis e evitam que os arquivos percam a proporcionalidade.",
            priority="medium",
            change_required=growth != 512,
            reference="https://learn.microsoft.com/sql/relational-databases/databases/tempdb-database",
        )
    )

    backup = facts.integer("backup_compression", 0)
    edition = facts.values.get("edition", "")
    backup_supported = "Express" not in edition
    items.append(
        RecommendationItem(
            key="backup_compression",
            category="Backup e integridade",
            label="Compressão de backup padrão",
            current="Ativada" if backup else "Desativada",
            recommended="Ativada" if backup_supported else "Verificar suporte da edição",
            rationale="Reduz I/O, duração e espaço dos backups, com algum custo de CPU. Agende os backups fora do pico e valide o throughput do servidor.",
            priority="medium" if backup_supported and not backup else "info",
            change_required=backup_supported and not bool(backup),
        )
    )

    ad_hoc = facts.integer("optimize_ad_hoc", 0)
    single_use_mb = facts.integer("single_use_adhoc_mb", 0)
    plan_cache_mb = facts.integer("plan_cache_mb", 0)
    single_use_pct = round(single_use_mb * 100 / plan_cache_mb) if plan_cache_mb > 0 else 0
    ad_hoc_recommended = not bool(ad_hoc) and single_use_mb >= 256 and single_use_pct >= 20
    items.append(
        RecommendationItem(
            key="ad_hoc",
            category="Planos e diagnóstico",
            label="Otimizar cargas ad hoc",
            current=("Ativado" if ad_hoc else "Desativado")
            + (f" · {single_use_mb} MB de uso único ({single_use_pct}% do cache)" if plan_cache_mb else ""),
            recommended="Ativar" if ad_hoc_recommended else "Manter e acompanhar o cache de planos",
            rationale="A decisão usa a proporção medida de planos ad hoc de uso único; ativar sem essa evidência pode esconder planos úteis para diagnóstico.",
            priority="medium" if ad_hoc_recommended else "info",
            change_required=ad_hoc_recommended,
            reference="https://learn.microsoft.com/sql/database-engine/configure-windows/optimize-for-ad-hoc-workloads-server-configuration-option",
        )
    )

    backup_checksum_known = "backup_checksum" in facts.values
    backup_checksum = facts.integer("backup_checksum", 0)
    items.append(
        RecommendationItem(
            key="backup_checksum",
            category="Backup e integridade",
            label="Checksum padrão dos backups",
            current=("Ativado" if backup_checksum else "Desativado") if backup_checksum_known else "Não foi possível verificar",
            recommended="Ativado",
            rationale="Faz o BACKUP verificar checksums de página e gravar checksum na mídia, aumentando a capacidade de detectar corrupção durante backup e validação.",
            priority="high" if backup_checksum_known and not backup_checksum else "info",
            change_required=backup_checksum_known and not bool(backup_checksum),
            reference="https://learn.microsoft.com/sql/database-engine/configure-windows/backup-checksum-default-server-configuration-option",
        )
    )

    safety_options = [
        (
            "xp_cmdshell",
            "xp_cmdshell",
            "Execução de comandos do sistema operacional",
            "Amplia muito a superfície de ataque; mantenha desativado e habilite apenas de forma temporária quando houver requisito controlado.",
            "critical",
        ),
        (
            "ole_automation",
            "Ole Automation Procedures",
            "Automação OLE",
            "Executa objetos COM dentro do processo do SQL Server e aumenta riscos de segurança e estabilidade.",
            "high",
        ),
        (
            "ad_hoc_distributed_queries",
            "Ad Hoc Distributed Queries",
            "Consultas distribuídas ad hoc",
            "Permite acesso eventual a provedores externos; deixe desativado quando não houver uma dependência documentada.",
            "high",
        ),
        (
            "cross_db_ownership_chaining",
            "cross db ownership chaining",
            "Encadeamento de propriedade entre bancos",
            "Habilitar globalmente amplia permissões entre bancos; prefira assinatura de módulos ou configuração explícita por banco.",
            "critical",
        ),
        (
            "priority_boost",
            "priority boost",
            "Priority boost",
            "Alterar a prioridade do processo pode prejudicar componentes do Windows e não é recomendado.",
            "high",
        ),
        (
            "lightweight_pooling",
            "lightweight pooling",
            "Fiber mode (lightweight pooling)",
            "O modo de fibras raramente ajuda cargas modernas e pode impedir recursos importantes do mecanismo.",
            "high",
        ),
    ]
    for key, _, label, rationale, enabled_priority in safety_options:
        known = key in facts.values
        enabled = facts.integer(key, 0) != 0
        items.append(
            RecommendationItem(
                key=key,
                category="Segurança e superfície",
                label=label,
                current=("Ativado" if enabled else "Desativado") if known else "Não foi possível verificar",
                recommended="Desativado",
                rationale=rationale,
                priority=enabled_priority if known and enabled else "info",
                change_required=known and enabled,
                reference="https://learn.microsoft.com/sql/database-engine/configure-windows/server-configuration-options-sql-server",
            )
        )

    default_options = [
        (
            "max_worker_threads",
            "Máximo de worker threads",
            0,
            "0 (dimensionamento automático)",
            "O mecanismo calcula o total de workers conforme CPU e arquitetura; valor manual pode causar exaustão ou desperdício.",
        ),
        (
            "network_packet_size",
            "Tamanho do pacote de rede",
            4096,
            "4096 bytes",
            "O valor padrão é adequado para a maioria das cargas; alterações globais podem aumentar memória e fragmentação sem ganho mensurável.",
        ),
        (
            "recovery_interval",
            "Intervalo de recuperação",
            0,
            "0 (automático)",
            "O checkpoint indireto e o dimensionamento automático são preferíveis a um intervalo global fixo sem medição de recuperação.",
        ),
    ]
    for key, label, expected, recommended, rationale in default_options:
        known = key in facts.values
        current_value = facts.integer(key, expected)
        changed = known and current_value != expected
        items.append(
            RecommendationItem(
                key=key,
                category="Configuração da instância",
                label=label,
                current=str(current_value) if known else "Não foi possível verificar",
                recommended=recommended,
                rationale=rationale,
                priority="medium" if changed else "info",
                change_required=changed,
                reference="https://learn.microsoft.com/sql/database-engine/configure-windows/server-configuration-options-sql-server",
            )
        )

    default_trace_known = "default_trace_enabled" in facts.values
    default_trace = facts.integer("default_trace_enabled", 1)
    items.append(
        RecommendationItem(
            key="default_trace_enabled",
            category="Planos e diagnóstico",
            label="Default trace",
            current=("Ativado" if default_trace else "Desativado") if default_trace_known else "Não foi possível verificar",
            recommended="Ativado enquanto suportado pela versão",
            rationale="Mantém um histórico leve de eventos administrativos útil para auditoria operacional em versões que ainda oferecem o recurso.",
            priority="medium" if default_trace_known and not default_trace else "info",
            change_required=default_trace_known and not bool(default_trace),
        )
    )

    ifi_value = facts.values.get("ifi_enabled", "").strip().lower()
    ifi_known = ifi_value in {"y", "n", "yes", "no", "1", "0", "true", "false"}
    ifi_enabled = ifi_value in {"y", "yes", "1", "true"}
    items.append(
        RecommendationItem(
            key="instant_file_initialization",
            category="tempdb e arquivos",
            label="Instant File Initialization",
            current=("Ativado" if ifi_enabled else "Desativado") if ifi_known else "Não foi possível verificar",
            recommended="Ativado para a conta do serviço SQL",
            rationale="Evita zerar fisicamente o espaço durante criação e crescimento de arquivos de dados, reduzindo pausas. Arquivos de log continuam seguindo suas regras próprias.",
            priority="high" if ifi_known and not ifi_enabled else "info",
            change_required=ifi_known and not ifi_enabled,
            reference="https://learn.microsoft.com/sql/relational-databases/databases/database-instant-file-initialization",
        )
    )

    locked_pages_mb = facts.integer("locked_pages_mb", 0)
    total_memory_for_lpim = facts.integer("host_total_memory_mb", 0)
    lpim_relevant = total_memory_for_lpim >= 16 * 1024 and "Express" not in edition
    items.append(
        RecommendationItem(
            key="lock_pages_in_memory",
            category="Memória e CPU",
            label="Lock Pages in Memory",
            current=f"{locked_pages_mb:,} MB bloqueados".replace(",", ".") if locked_pages_mb else "Sem páginas bloqueadas detectadas",
            recommended="Conceder à conta do serviço após validar max server memory" if lpim_relevant else "Não necessário para esta edição/capacidade",
            rationale="Reduz paginação do buffer pool pelo Windows, mas exige limite de memória bem definido e concessão do direito no sistema operacional.",
            priority="medium" if lpim_relevant and locked_pages_mb == 0 else "info",
            change_required=lpim_relevant and locked_pages_mb == 0,
            reference="https://learn.microsoft.com/sql/database-engine/configure-windows/enable-the-lock-pages-in-memory-option-windows",
        )
    )

    def database_state(key: str, names_key: str) -> tuple[int | None, str]:
        if key not in facts.values:
            return None, "Não foi possível verificar"
        count = facts.integer(key, 0)
        names = facts.values.get(names_key, "").strip()
        return count, (f"{count} banco(s): {names}" if names else f"{count} banco(s)")

    database_rules = [
        ("db_not_online_count", "db_not_online_names", "database_state", "Disponibilidade dos bancos", "Todos ONLINE", "Banco fora de ONLINE exige investigação antes de qualquer ajuste de performance.", "critical", "Operação e disponibilidade"),
        ("db_auto_close_count", "db_auto_close_names", "database_auto_close", "AUTO_CLOSE", "Desativado", "AUTO_CLOSE fecha e reabre o banco, descartando cache e adicionando latência.", "high", "Configuração dos bancos"),
        ("db_auto_shrink_count", "db_auto_shrink_names", "database_auto_shrink", "AUTO_SHRINK", "Desativado", "AUTO_SHRINK causa movimentação recorrente de páginas, fragmentação e crescimento posterior.", "high", "tempdb e arquivos"),
        ("db_page_verify_count", "db_page_verify_names", "database_page_verify", "PAGE_VERIFY", "CHECKSUM", "Checksums de página permitem detectar corrupção de I/O quando páginas são lidas.", "critical", "Backup e integridade"),
        ("db_auto_create_stats_off_count", "db_auto_create_stats_off_names", "database_auto_create_stats", "Criação automática de estatísticas", "Ativada", "O otimizador precisa criar estatísticas de coluna quando necessário para estimativas adequadas.", "high", "Estatísticas e planos"),
        ("db_auto_update_stats_off_count", "db_auto_update_stats_off_names", "database_auto_update_stats", "Atualização automática de estatísticas", "Ativada", "Estatísticas desatualizadas degradam estimativas de cardinalidade e planos.", "high", "Estatísticas e planos"),
        ("db_query_store_off_count", "db_query_store_off_names", "database_query_store", "Query Store", "READ_WRITE com captura AUTO", "Mantém histórico de consultas, planos e regressões com política de captura e limpeza controladas.", "high", "Planos e diagnóstico"),
        ("db_trustworthy_count", "db_trustworthy_names", "database_trustworthy", "TRUSTWORTHY", "Desativado", "TRUSTWORTHY amplia caminhos de elevação de privilégio; use assinatura de módulos quando necessário.", "critical", "Segurança e superfície"),
        ("db_chaining_count", "db_chaining_names", "database_chaining", "DB_CHAINING", "Desativado salvo dependência documentada", "Encadeamento entre bancos pode atravessar fronteiras de permissão e deve ser explícito.", "high", "Segurança e superfície"),
    ]
    for count_key, names_key, key, label, recommended, rationale, priority, category in database_rules:
        count, current = database_state(count_key, names_key)
        items.append(
            RecommendationItem(
                key=key,
                category=category,
                label=label,
                current=current if count is None or count else "Conforme",
                recommended=recommended,
                rationale=rationale,
                priority=priority if count is not None and count > 0 else "info",
                change_required=count is not None and count > 0,
                reference="https://learn.microsoft.com/sql/relational-databases/databases/database-options-and-settings",
            )
        )

    file_growth_known = "db_percent_growth_count" in facts.values and "db_small_growth_count" in facts.values
    percent_growth = facts.integer("db_percent_growth_count", 0)
    small_growth = facts.integer("db_small_growth_count", 0)
    file_growth_issues = percent_growth + small_growth
    items.append(
        RecommendationItem(
            key="database_file_growth",
            category="tempdb e arquivos",
            label="Autogrowth dos arquivos de usuário",
            current=(f"{percent_growth} percentual(is); {small_growth} incremento(s) menor(es) que 64 MB" if file_growth_known else "Não foi possível verificar"),
            recommended="Crescimento fixo e dimensionado por tipo de arquivo",
            rationale="Incrementos percentuais tornam o crescimento imprevisível; incrementos muito pequenos geram pausas frequentes e fragmentação física.",
            priority="high" if file_growth_known and file_growth_issues else "info",
            change_required=file_growth_known and file_growth_issues > 0,
            reference="https://learn.microsoft.com/sql/relational-databases/databases/database-files-and-filegroups",
        )
    )

    full_backup_known = "db_full_backup_overdue_count" in facts.values
    log_backup_known = "db_log_backup_overdue_count" in facts.values
    suspect_pages_known = "suspect_pages_count" in facts.values
    full_backup_overdue = facts.integer("db_full_backup_overdue_count", 0)
    log_backup_overdue = facts.integer("db_log_backup_overdue_count", 0)
    suspect_pages = facts.integer("suspect_pages_count", 0)
    items.extend(
        [
            RecommendationItem(
                key="full_backup_recency",
                category="Backup e integridade",
                label="Recência de backup completo",
                current=(f"{full_backup_overdue} banco(s) sem full válido nos últimos 7 dias" if full_backup_known else "Não foi possível verificar o histórico do msdb"),
                recommended="Política de backup conforme RPO/RTO e teste de restauração",
                rationale="A existência do arquivo não comprova recuperabilidade; mantenha histórico recente e valide restaurações periodicamente.",
                priority="critical" if full_backup_known and full_backup_overdue else "info",
                change_required=full_backup_known and full_backup_overdue > 0,
                reference="https://learn.microsoft.com/sql/relational-databases/backup-restore/backup-overview-sql-server",
            ),
            RecommendationItem(
                key="log_backup_recency",
                category="Backup e integridade",
                label="Recência de backup de log",
                current=(f"{log_backup_overdue} banco(s) FULL/BULK_LOGGED sem log nas últimas 24 horas" if log_backup_known else "Não foi possível verificar o histórico do msdb"),
                recommended="Frequência compatível com o RPO definido",
                rationale="Sem backup de log a cadeia pode crescer e o ponto de recuperação fica distante do objetivo operacional.",
                priority="critical" if log_backup_known and log_backup_overdue else "info",
                change_required=log_backup_known and log_backup_overdue > 0,
                reference="https://learn.microsoft.com/sql/relational-databases/backup-restore/transaction-log-backups-sql-server",
            ),
            RecommendationItem(
                key="suspect_pages",
                category="Backup e integridade",
                label="Páginas suspeitas registradas",
                current=str(suspect_pages) if suspect_pages_known else "Não foi possível verificar msdb.dbo.suspect_pages",
                recommended="0; investigar imediatamente qualquer ocorrência",
                rationale="Eventos 1, 2 ou 3 em msdb.dbo.suspect_pages indicam erros graves de leitura ou checksum e exigem validação de storage, CHECKDB e estratégia de restauração.",
                priority="critical" if suspect_pages_known and suspect_pages else "info",
                change_required=suspect_pages_known and suspect_pages > 0,
                reference="https://learn.microsoft.com/sql/relational-databases/backup-restore/manage-the-suspect-pages-table-sql-server",
            ),
        ]
    )

    command_lines = [
        "-- Gerado a partir das não conformidades observadas nesta coleta.",
        "-- Revise em homologação, valide dependências e aplique em janela de mudança.",
    ]
    sp_config_changes: list[tuple[str, int]] = []
    if memory_changed:
        sp_config_changes.append(("max server memory (MB)", memory_mb))
    if current_min != 0:
        sp_config_changes.append(("min server memory (MB)", 0))
    if current_maxdop != maxdop:
        sp_config_changes.append(("max degree of parallelism", maxdop))
    if threshold != 50:
        sp_config_changes.append(("cost threshold for parallelism", 50))
    if backup_supported and not backup:
        sp_config_changes.append(("backup compression default", 1))
    if backup_checksum_known and not backup_checksum:
        sp_config_changes.append(("backup checksum default", 1))
    if ad_hoc_recommended:
        sp_config_changes.append(("optimize for ad hoc workloads", 1))
    for key, option_name, _, _, _ in safety_options:
        if facts.integer(key, 0) != 0:
            sp_config_changes.append((option_name, 0))
    for key, _, expected, _, _ in default_options:
        if facts.integer(key, expected) != expected:
            option_name = {
                "max_worker_threads": "max worker threads",
                "network_packet_size": "network packet size (B)",
                "recovery_interval": "recovery interval (min)",
            }[key]
            sp_config_changes.append((option_name, expected))
    if default_trace_known and not default_trace:
        sp_config_changes.append(("default trace enabled", 1))

    if sp_config_changes:
        command_lines.extend(
            [
                "",
                "-- Configurações da instância",
                "EXEC sys.sp_configure N'show advanced options', 1;",
                "RECONFIGURE;",
            ]
        )
        command_lines.extend(
            f"EXEC sys.sp_configure N'{option_name.replace(chr(39), chr(39) * 2)}', {value};"
            for option_name, value in sp_config_changes
        )
        command_lines.append("RECONFIGURE;")
    if tempdb_current < tempdb_target or not equal_sizes or growth != 512:
        command_lines.extend(
            [
                "",
                "-- tempdb: valide o espaço livre no volume antes de executar este bloco.",
                "-- Arquivos existentes não são removidos; os menores crescem até o tamanho do maior.",
                "USE [master];",
                f"DECLARE @tempdbTargetFileCount int = {tempdb_target};",
                "DECLARE @tempdbGrowthMb int = 512;",
                "DECLARE @tempdbCurrentFileCount int;",
                "DECLARE @tempdbTargetSizeMb bigint;",
                "DECLARE @tempdbDirectory nvarchar(4000);",
                "DECLARE @tempdbLogicalName sysname;",
                "DECLARE @tempdbPhysicalName nvarchar(4000);",
                "DECLARE @tempdbSql nvarchar(max);",
                "DECLARE @tempdbCandidate int = 2;",
                "SELECT",
                "    @tempdbCurrentFileCount = COUNT(*),",
                "    @tempdbTargetSizeMb = MAX((CONVERT(bigint, size) + 127) / 128)",
                "FROM tempdb.sys.database_files",
                "WHERE type = 0;",
                "SET @tempdbTargetSizeMb = CASE WHEN @tempdbTargetSizeMb < 256 THEN 256 ELSE @tempdbTargetSizeMb END;",
                "SELECT TOP (1)",
                "    @tempdbDirectory = LEFT(physical_name, LEN(physical_name) - CHARINDEX(N'\\', REVERSE(physical_name)) + 1)",
                "FROM tempdb.sys.database_files",
                "WHERE type = 0",
                "ORDER BY file_id;",
                "WHILE @tempdbCurrentFileCount < @tempdbTargetFileCount",
                "BEGIN",
                "    SET @tempdbLogicalName = N'tempdb_advisor_' + CONVERT(nvarchar(10), @tempdbCandidate);",
                "    SET @tempdbPhysicalName = @tempdbDirectory + @tempdbLogicalName + N'.ndf';",
                "    IF EXISTS (SELECT 1 FROM tempdb.sys.database_files WHERE name = @tempdbLogicalName)",
                "    BEGIN",
                "        SET @tempdbCandidate += 1;",
                "        CONTINUE;",
                "    END;",
                "    SET @tempdbSql = N'ALTER DATABASE [tempdb] ADD FILE (NAME = N'''",
                "        + REPLACE(@tempdbLogicalName, N'''', N'''''') + N''', FILENAME = N'''",
                "        + REPLACE(@tempdbPhysicalName, N'''', N'''''') + N''', SIZE = '",
                "        + CONVERT(nvarchar(20), @tempdbTargetSizeMb) + N'MB, FILEGROWTH = '",
                "        + CONVERT(nvarchar(20), @tempdbGrowthMb) + N'MB);';",
                "    EXEC sys.sp_executesql @tempdbSql;",
                "    SET @tempdbCurrentFileCount += 1;",
                "    SET @tempdbCandidate += 1;",
                "END;",
                "SET @tempdbSql = N'';",
                "SELECT @tempdbSql = @tempdbSql",
                "    + N'ALTER DATABASE [tempdb] MODIFY FILE (NAME = N'''",
                "    + REPLACE(name, N'''', N'''''') + N''', SIZE = '",
                "    + CONVERT(nvarchar(20), @tempdbTargetSizeMb) + N'MB, FILEGROWTH = '",
                "    + CONVERT(nvarchar(20), @tempdbGrowthMb) + N'MB);' + CHAR(13) + CHAR(10)",
                "FROM tempdb.sys.database_files",
                "WHERE type = 0;",
                "EXEC sys.sp_executesql @tempdbSql;",
            ]
        )

    def append_database_option_block(
        title: str,
        variable: str,
        predicate: str,
        option_clause: str,
    ) -> None:
        command_lines.extend(
            [
                "",
                f"-- {title}",
                f"DECLARE @{variable} nvarchar(max) = N'';",
                f"SELECT @{variable} = @{variable} + N'ALTER DATABASE ' + QUOTENAME(name) + N' {option_clause};' + CHAR(13) + CHAR(10)",
                "FROM sys.databases",
                f"WHERE database_id > 4 AND state_desc = 'ONLINE' AND {predicate};",
                f"EXEC sys.sp_executesql @{variable};",
            ]
        )

    if facts.integer("db_auto_close_count", 0):
        append_database_option_block("Desativar AUTO_CLOSE", "advisorAutoClose", "is_auto_close_on = 1", "SET AUTO_CLOSE OFF")
    if facts.integer("db_auto_shrink_count", 0):
        append_database_option_block("Desativar AUTO_SHRINK", "advisorAutoShrink", "is_auto_shrink_on = 1", "SET AUTO_SHRINK OFF")
    if facts.integer("db_page_verify_count", 0):
        append_database_option_block("Habilitar PAGE_VERIFY CHECKSUM", "advisorPageVerify", "page_verify_option_desc <> 'CHECKSUM'", "SET PAGE_VERIFY CHECKSUM")
    if facts.integer("db_auto_create_stats_off_count", 0):
        append_database_option_block("Habilitar criação automática de estatísticas", "advisorAutoCreateStats", "is_auto_create_stats_on = 0", "SET AUTO_CREATE_STATISTICS ON")
    if facts.integer("db_auto_update_stats_off_count", 0):
        append_database_option_block("Habilitar atualização automática de estatísticas", "advisorAutoUpdateStats", "is_auto_update_stats_on = 0", "SET AUTO_UPDATE_STATISTICS ON")
    if facts.integer("db_query_store_off_count", 0):
        command_lines.extend(
            [
                "",
                "-- Query Store: valide espaço, retenção e sobrecarga antes de habilitar em massa.",
                "DECLARE @advisorQueryStore nvarchar(max) = N'';",
                "SELECT @advisorQueryStore = @advisorQueryStore",
                "    + N'ALTER DATABASE ' + QUOTENAME(name) + N' SET QUERY_STORE = ON;' + CHAR(13) + CHAR(10)",
                "    + N'ALTER DATABASE ' + QUOTENAME(name) + N' SET QUERY_STORE (OPERATION_MODE = READ_WRITE, QUERY_CAPTURE_MODE = AUTO, SIZE_BASED_CLEANUP_MODE = AUTO);' + CHAR(13) + CHAR(10)",
                "FROM sys.databases",
                "WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_query_store_on = 0;",
                "EXEC sys.sp_executesql @advisorQueryStore;",
            ]
        )
    if facts.integer("db_trustworthy_count", 0):
        append_database_option_block(
            "Desativar TRUSTWORTHY somente após validar módulos, assemblies e EXECUTE AS",
            "advisorTrustworthy",
            "is_trustworthy_on = 1",
            "SET TRUSTWORTHY OFF",
        )
    if facts.integer("db_chaining_count", 0):
        append_database_option_block(
            "Desativar DB_CHAINING somente após validar dependências entre bancos",
            "advisorDbChaining",
            "is_db_chaining_on = 1",
            "SET DB_CHAINING OFF",
        )
    if file_growth_issues:
        command_lines.extend(
            [
                "",
                "-- Autogrowth: baseline inicial; ajuste os incrementos ao tamanho e à taxa de crescimento de cada banco.",
                "DECLARE @advisorFileGrowth nvarchar(max) = N'';",
                "SELECT @advisorFileGrowth = @advisorFileGrowth",
                "    + N'ALTER DATABASE ' + QUOTENAME(DB_NAME(database_id)) + N' MODIFY FILE (NAME = N'''",
                "    + REPLACE(name, N'''', N'''''') + N''', FILEGROWTH = '",
                "    + CASE WHEN type = 1 THEN N'256MB' ELSE N'512MB' END + N');' + CHAR(13) + CHAR(10)",
                "FROM sys.master_files",
                "WHERE database_id > 4",
                "  AND (is_percent_growth = 1 OR (is_percent_growth = 0 AND growth > 0 AND growth * 8 / 1024 < 64));",
                "EXEC sys.sp_executesql @advisorFileGrowth;",
            ]
        )

    if ifi_known and not ifi_enabled:
        command_lines.extend(
            [
                "",
                "-- Ação no Windows: conceda 'Perform volume maintenance tasks' à conta do serviço SQL.",
                "-- Reinicie o serviço em uma janela controlada e confirme instant_file_initialization_enabled = Y.",
            ]
        )
    if lpim_relevant and locked_pages_mb == 0:
        command_lines.extend(
            [
                "",
                "-- Ação no Windows: conceda 'Lock pages in memory' à conta do serviço SQL.",
                "-- Confirme primeiro o limite de max server memory e reinicie o serviço em janela controlada.",
            ]
        )
    if full_backup_overdue or log_backup_overdue:
        command_lines.extend(
            [
                "",
                "-- Backups vencidos exigem definição de destino, retenção, criptografia e RPO/RTO.",
                "-- O painel não gera BACKUP DATABASE/LOG sem essas decisões para evitar gravar em destino inadequado.",
            ]
        )
    if suspect_pages:
        command_lines.extend(
            [
                "",
                "-- ALERTA: páginas suspeitas detectadas. Não execute REPAIR_ALLOW_DATA_LOSS automaticamente.",
                "-- Valide storage, execute DBCC CHECKDB em janela apropriada e priorize restauração de backup íntegro.",
            ]
        )

    notes = [
        "Não foi sugerida máscara de afinidade: ela pode criar gargalos e exige topologia NUMA e perfil de carga para ser definida com segurança.",
        "Mudanças de Windows, backup, integridade e disponibilidade aparecem como ações operacionais quando não existe T-SQL universal e seguro.",
        "A avaliação é uma baseline; decisões de índice, consultas, HA/DR e capacidade exigem histórico representativo, RPO/RTO e contexto da aplicação.",
    ]
    collection_errors = [
        facts.values[key]
        for key in ("host_metrics_error", "database_metrics_error", "operational_metrics_error")
        if facts.values.get(key)
    ]
    if collection_errors:
        notes.append(
            "Cobertura parcial: uma ou mais áreas não puderam ser consultadas com as permissões atuais. "
            + " | ".join(collection_errors)
        )
    return items, "\n".join(command_lines), notes
