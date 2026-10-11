# API HTTP

O servidor Uvicorn escuta em `127.0.0.1:9000`. Swagger, ReDoc e OpenAPI estão desligados (`docs_url=None`, `redoc_url=None`, `openapi_url=None`). Todas as respostas levam `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` e `Referrer-Policy: no-referrer`.

`TrustedHostMiddleware` aceita apenas `127.0.0.1`, `localhost` e `testserver`.

## Rotas

| Método | Caminho | Descrição |
| --- | --- | --- |
| `GET` | `/` | Painel HTML |
| `GET` | `/health` | `{"status":"ok"}` |
| `GET` | `/static/...` | CSS e JavaScript |
| `GET` | `/collector/Collect-SqlServerAdvisor.ps1` | Download do coletor |
| `GET` | `/collector/Configure-SqlAdvisorWinRMHttps.ps1` | Download do configurador WinRM |
| `POST` | `/api/scan` | Análise ao vivo |
| `POST` | `/api/import` | Análise a partir de snapshot JSON |

## POST /api/scan

Corpo JSON (`ScanRequest`):

```json
{
  "server": "db01.corp.local",
  "windows_authentication": "current",
  "windows_username": null,
  "windows_password": null,
  "winrm_use_ssl": false,
  "authentication": "windows",
  "username": null,
  "password": null,
  "reserve_memory_percent": 25,
  "reserve_memory_gb": 4,
  "reserve_logical_cpus": null,
  "login_timeout_seconds": 8,
  "encrypt_connection": true,
  "trust_server_certificate": true
}
```

| Campo | Tipo | Padrão | Regras |
| --- | --- | --- | --- |
| `server` | string | — | 1–255 caracteres; DNS, NetBIOS ou IP. Sem `\`, `/` ou `:` |
| `windows_authentication` | `current` \| `credentials` | `current` | Com `credentials`, usuário e senha Windows são obrigatórios |
| `windows_username` | string | null | Máx. 256. Formato `DOMINIO\usuario` ou `SERVIDOR\usuario` |
| `windows_password` | string | null | Máx. 512 |
| `winrm_use_ssl` | bool | false | `true` = HTTPS 5986 |
| `authentication` | `windows` \| `sql` | `windows` | Com `sql`, usuário e senha SQL são obrigatórios |
| `username` / `password` | string | null | Máx. 128 / 512 |
| `reserve_memory_percent` | int | 25 | 10–60 |
| `reserve_memory_gb` | int | 4 | 2–1024 |
| `reserve_logical_cpus` | int \| null | null | 1–256; `null` = 20% automático |
| `login_timeout_seconds` | int | 8 | 3–30 |
| `encrypt_connection` | bool | true | `sqlcmd -N` |
| `trust_server_certificate` | bool | true | `sqlcmd -C` se a criptografia estiver ligada |

`server` passa por `normalize_server_name()`: rejeita metacaracteres de shell e labels DNS inválidos. IPv4 e IPv6 literais (sem porta) são aceitos.

## POST /api/import

```json
{
  "snapshot": { "schema_version": 1, "generated_at": "…", "host": {}, "instances": [], "warnings": [] },
  "reserve_memory_percent": 25,
  "reserve_memory_gb": 4,
  "reserve_logical_cpus": null
}
```

O objeto `snapshot` segue o schema do coletor. Ver [coletores.md](coletores.md#schema-do-snapshot-schema_version-1).

Reservas de memória/CPU são as mesmas do scan: o JSON traz o estado coletado; o orçamento é recalculado no import.

## Resposta (`ScanResponse`)

```json
{
  "scanned_at": "2026-10-11T14:22:01-03:00",
  "host": {
    "computer_name": "DB01",
    "os_caption": "Microsoft Windows Server 2022 Standard",
    "os_version": "10.0.20348",
    "total_memory_mb": 65536,
    "free_memory_mb": 18432,
    "logical_cpus": 16,
    "sql_memory_budget_mb": 49152,
    "reserved_memory_mb": 16384,
    "observed_non_sql_memory_mb": 8192,
    "sql_cpu_budget": 13,
    "reserved_logical_cpus": 3
  },
  "instances": [
    {
      "instance_name": "ERP",
      "endpoint": "tcp:DB01,51433",
      "service_state": "Running",
      "service_start_mode": "Auto",
      "version": "16.0.1000.6",
      "edition": "Standard Edition",
      "product_level": "RTM",
      "connected": true,
      "connection_error": null,
      "recommendations": [
        {
          "key": "max_server_memory",
          "category": "Memória e CPU",
          "label": "Memória máxima do SQL Server",
          "current": "Sem limite prático",
          "recommended": "36.864 MB",
          "rationale": "…",
          "priority": "critical",
          "change_required": true,
          "reference": "https://learn.microsoft.com/sql/database-engine/configure-windows/server-memory-server-configuration-options"
        }
      ],
      "script": "-- Gerado a partir das não conformidades…",
      "notes": []
    }
  ],
  "warnings": [],
  "methodology": [
    "Reserva do Windows: o maior valor entre 25% da RAM e 4 GB.",
    "…"
  ]
}
```

`scanned_at` no import é o `generated_at` do snapshot.

Instância descoberta mas sem conexão SQL devolve `recommendations: []`, `script: null` e notas pedindo `CONNECT SQL` e `VIEW SERVER STATE`.

## Erros

| HTTP | Situação |
| --- | --- |
| 422 | Host inválido, credenciais incompletas, descoberta local falhou, snapshot inconsistente, schema ≠ 1, hardware inacessível (Windows e DMVs) |
| 500 | Falha inesperada no processo |

O campo `detail` é uma string já sanitizada: senhas SQL e Windows são substituídas por `********` (`safe_public_error`) e o texto é cortado em 1200 caracteres.

Validação Pydantic pode devolver `detail` como lista de `{loc, msg, type}`; o JavaScript do painel concatena os `msg`.

## Health e downloads

```http
GET /health
```

```http
GET /collector/Collect-SqlServerAdvisor.ps1
Content-Type: text/plain; charset=utf-8
Content-Disposition: attachment; filename="Collect-SqlServerAdvisor.ps1"
```

Os downloads leem os arquivos em `collector/` no disco; não há geração dinâmica do script.
