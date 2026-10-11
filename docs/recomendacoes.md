# Catálogo de recomendações

Cada verificação vira um `RecommendationItem` com categoria, valor atual, valor recomendado, justificativa, prioridade e flag `change_required`. O script T-SQL contém **somente** as mudanças observáveis nesta coleta. Itens que exigem decisão operacional (destino de backup, direito do Windows, CHECKDB) entram como comentários no script ou como notas da instância.

Prioridades:

| Prioridade | Uso |
| --- | --- |
| `critical` | Risco de segurança, corrupção, instância sem teto de memória, banco fora de ONLINE, backup vencido |
| `high` | Desvio relevante de baseline (MAXDOP 0, tempdb desigual, AUTO_CLOSE, Query Store off) |
| `medium` | Ajuste fino (min memory, cost threshold, worker threads manuais) |
| `info` | Já conforme, ou evidência insuficiente para pedir mudança |

## Memória e CPU

### max_server_memory

| Campo | Valor |
| --- | --- |
| Categoria | Memória e CPU |
| Recomendado | Parcela desta instância no orçamento SQL (MB) |
| Ação | Diferença ≥ 256 MB em relação ao valor em uso |
| Prioridade | `critical` se o valor atual for o teto prático (2 147 483 647); senão `high` |
| T-SQL | `sp_configure 'max server memory (MB)'` |

Limita o buffer pool à parcela do host. Reavalie após medir o pico real da carga.

### min_server_memory

| Campo | Valor |
| --- | --- |
| Recomendado | 0 MB |
| Ação | Qualquer valor diferente de zero |
| Prioridade | `medium` se diferente de zero |
| T-SQL | `sp_configure 'min server memory (MB)', 0` |

Zero permite devolver memória sob pressão, o que importa com vários serviços ou várias instâncias no mesmo Windows.

### MAXDOP

Calculado por `recommended_maxdop()` com a parcela de CPU da instância e a topologia NUMA coletada (`numa_node_count`, `max_schedulers_per_numa`). Ver [arquitetura.md](arquitetura.md#maxdop).

| Campo | Valor |
| --- | --- |
| Ação | Valor em uso diferente do calculado |
| Prioridade | `high` se atual for 0 ou maior que o teto; senão `medium` |
| T-SQL | `sp_configure 'max degree of parallelism'` |

MAXDOP limita paralelismo **por operador**. O mecanismo continua podendo agendar trabalho em todos os schedulers visíveis.

### cost_threshold

| Campo | Valor |
| --- | --- |
| Recomendado | 50 (ponto de partida) |
| Ação | Valor atual ≠ 50 |
| Prioridade | `high` se ≤ 5 (padrão de instalação); senão `medium` |
| T-SQL | `sp_configure 'cost threshold for parallelism', 50` |

Refine com Query Store e waits depois de um ciclo representativo.

### Lock Pages in Memory

| Campo | Valor |
| --- | --- |
| Recomendado | Conceder o direito à conta do serviço **depois** de fixar max server memory |
| Condição | Host com ≥ 16 GB e edição diferente de Express |
| Ação | Condição verdadeira e `locked_pages_mb = 0` |
| T-SQL | Comentário operacional (direito do Windows + restart) |

## tempdb e arquivos

### Arquivos de dados do tempdb

| Campo | Valor |
| --- | --- |
| Alvo inicial | `min(8, CPUs lógicas do host)` |
| Alvo final | `max(arquivos atuais, alvo inicial)` |
| Ação | Quantidade diferente do alvo **ou** tamanhos desiguais |
| T-SQL | Bloco que **adiciona** arquivos até o alvo, iguala o tamanho ao maior arquivo (piso 256 MB) e aplica `FILEGROWTH = 512MB` |

Arquivos existentes nunca são removidos. Aumente além de 8 somente com evidência de contenção `PAGELATCH` no tempdb.

O tempdb é dimensionado pelos processadores lógicos **visíveis ao SQL Server / host**, não pela parcela usada no MAXDOP. Usar a parcela poderia sugerir reduzir 8 arquivos para 6 em um host de 12 CPUs.

### Crescimento do tempdb

| Campo | Valor |
| --- | --- |
| Recomendado | 512 MB fixos por arquivo |
| Ação | Crescimento percentual, não identificado, ou diferente de 512 MB |
| T-SQL | Incluído no bloco de tempdb acima |

### Instant File Initialization

| Campo | Valor |
| --- | --- |
| Fonte | `sys.dm_server_services.instant_file_initialization_enabled` |
| Recomendado | Ativado para a conta do serviço SQL |
| Ação | Valor conhecido e desativado |
| Prioridade | `high` |
| T-SQL | Comentário: conceder *Perform volume maintenance tasks* e reiniciar o serviço |

Arquivos de log continuam sendo zerados pelo mecanismo.

### AUTO_SHRINK

Bancos de usuário ONLINE com `is_auto_shrink_on = 1`. Prioridade `high`. T-SQL: `ALTER DATABASE … SET AUTO_SHRINK OFF`.

### Autogrowth de arquivos de usuário

Conta arquivos em `sys.master_files` (database_id > 4) com crescimento percentual **ou** incremento fixo menor que 64 MB.

| Campo | Valor |
| --- | --- |
| Recomendado | Crescimento fixo: 512 MB para dados, 256 MB para log |
| Prioridade | `high` se houver ocorrências |
| T-SQL | `ALTER DATABASE … MODIFY FILE (FILEGROWTH = …)` |

O script é uma baseline inicial. Ajuste o incremento ao tamanho e à taxa de cada banco.

## Backup e integridade

### Compressão de backup padrão

Ativar `backup compression default` em edições que não sejam Express. Prioridade `medium`. T-SQL correspondente.

### Checksum padrão dos backups

Ativar `backup checksum default` quando a opção foi lida e está desligada. Prioridade `high`. Sem a chave na coleta, o item fica `info` (“Não foi possível verificar”).

### PAGE_VERIFY

Bancos ONLINE cujo `page_verify_option_desc` não é `CHECKSUM`. Prioridade `critical`. T-SQL: `ALTER DATABASE … SET PAGE_VERIFY CHECKSUM`.

### Recência de backup completo

Bancos de usuário ONLINE, não snapshot, sem backup tipo `D` (não copy-only) nos últimos **7 dias**, segundo `msdb.dbo.backupset`. Prioridade `critical`. **Sem T-SQL de BACKUP** — o script só lembra que destino, retenção, criptografia e RPO precisam ser definidos.

### Recência de backup de log

Bancos FULL/BULK_LOGGED sem backup tipo `L` nas últimas **24 horas**. Mesma política: alerta operacional, sem `BACKUP LOG` gerado.

### Páginas suspeitas

Contagem em `msdb.dbo.suspect_pages` com `event_type IN (1, 2, 3)`. Prioridade `critical`. O script alerta para **não** executar `REPAIR_ALLOW_DATA_LOSS` automaticamente; priorize storage, `DBCC CHECKDB` e restauração íntegra.

## Planos e diagnóstico

### Otimizar cargas ad hoc

A opção `optimize for ad hoc workloads` só vira ação quando **as três** condições são verdadeiras:

- a opção está desligada
- há pelo menos **256 MB** de planos ad hoc de uso único (`usecounts = 1`)
- esses planos representam pelo menos **20%** do cache de planos

Sem essa evidência, o item permanece “Manter e acompanhar o cache de planos”. Ativar cedo demais esconde planos úteis para diagnóstico.

### Default Trace

Recomendado ativado enquanto a versão ainda oferece o recurso. Ação se a chave foi lida e o valor é 0.

### Query Store

Bancos de usuário ONLINE com `is_query_store_on = 0`. T-SQL:

```sql
ALTER DATABASE [nome] SET QUERY_STORE = ON;
ALTER DATABASE [nome] SET QUERY_STORE (
  OPERATION_MODE = READ_WRITE,
  QUERY_CAPTURE_MODE = AUTO,
  SIZE_BASED_CLEANUP_MODE = AUTO
);
```

Valide espaço, retenção e sobrecarga antes de habilitar em massa.

## Segurança e superfície

Opções de servidor que devem permanecer **desativadas**. Se estiverem ligadas, entram no `sp_configure` com valor 0.

| Chave | Label | Prioridade se ligado |
| --- | --- | --- |
| `xp_cmdshell` | Execução de comandos do sistema operacional | critical |
| `ole_automation` | Automação OLE | high |
| `ad_hoc_distributed_queries` | Consultas distribuídas ad hoc | high |
| `cross_db_ownership_chaining` | Encadeamento de propriedade entre bancos | critical |
| `priority_boost` | Priority boost | high |
| `lightweight_pooling` | Fiber mode | high |

### TRUSTWORTHY e DB_CHAINING (por banco)

Bancos ONLINE com a opção ligada. Prioridade `critical` / `high`. O T-SQL desliga com comentário para validar módulos, assemblies, `EXECUTE AS` e dependências entre bancos **antes** de aplicar.

## Configuração da instância

Valores de fábrica preferidos:

| Opção | Esperado | Motivo |
| --- | --- | --- |
| `max worker threads` | 0 | O mecanismo calcula conforme CPU e arquitetura |
| `network packet size (B)` | 4096 | Padrão adequado à maioria das cargas |
| `recovery interval (min)` | 0 | Checkpoint indireto e dimensionamento automático |

Qualquer desvio conhecido gera `sp_configure` de volta ao padrão, prioridade `medium`.

## Estatísticas e opções de banco

Aplicam-se a bancos de usuário (`database_id > 4`) ONLINE, não snapshot. O T-SQL itera `sys.databases` e emite `ALTER DATABASE`.

| Chave | Problema | Recomendado | Prioridade |
| --- | --- | --- | --- |
| `database_state` | `state_desc <> 'ONLINE'` | Investigar antes de tunar | critical |
| `database_auto_close` | AUTO_CLOSE ligado | OFF | high |
| `database_auto_create_stats` | criação automática desligada | ON | high |
| `database_auto_update_stats` | atualização automática desligada | ON | high |

Bancos fora de ONLINE **não** geram T-SQL de performance. O nome dos bancos afetados aparece no campo “Atual” quando a coleta conseguiu montar a lista.

## O que o script nunca faz

- Máscara de afinidade (`affinity mask` / `ALTER SERVER CONFIGURATION SET PROCESS AFFINITY`)
- `BACKUP DATABASE` / `BACKUP LOG` com destino inventado
- `REMOVE FILE` no tempdb
- `DBCC CHECKDB` com reparo destrutivo
- Qualquer `sp_configure` para itens sem evidência nesta coleta
- Gravação de senha, hostname extra ou comando interativo

Notas fixas em toda instância conectada:

1. Afinidade de CPU exige topologia NUMA e perfil de carga para ser definida com segurança.
2. Mudanças de Windows, backup, integridade e disponibilidade aparecem como ações operacionais quando não existe T-SQL universal.
3. Índice, consultas, HA/DR e capacidade exigem histórico representativo, RPO/RTO e contexto da aplicação.

Se `host_metrics_error`, `database_metrics_error` ou `operational_metrics_error` vierem na coleta, uma nota extra descreve cobertura parcial.

## Referências usadas nas regras

Os itens apontam para Microsoft Learn quando a URL é estável. O painel só renderiza links que começam com `https://learn.microsoft.com/`.
