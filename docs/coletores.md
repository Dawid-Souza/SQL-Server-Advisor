# Coletores PowerShell

Os dois scripts em `collector/` também são oferecidos pelo painel:

| Arquivo | URL | Função |
| --- | --- | --- |
| `Collect-SqlServerAdvisor.ps1` | `GET /collector/Collect-SqlServerAdvisor.ps1` | Diagnóstico somente leitura → JSON |
| `Configure-SqlAdvisorWinRMHttps.ps1` | `GET /collector/Configure-SqlAdvisorWinRMHttps.ps1` | Listener WinRM HTTPS + firewall |

Requerem Windows PowerShell 5.1 ou superior e **execução elevada**. Se o Windows marcar o download como bloqueado:

```powershell
Unblock-File .\Collect-SqlServerAdvisor.ps1
Unblock-File .\Configure-SqlAdvisorWinRMHttps.ps1
```

## Collect-SqlServerAdvisor.ps1

Execute **dentro da sessão RDP** do Windows Server. O servidor remoto não precisa de Python, FastAPI nem WinRM aberto para o DBA.

```powershell
.\Collect-SqlServerAdvisor.ps1
.\Collect-SqlServerAdvisor.ps1 -UseSqlAuthentication
.\Collect-SqlServerAdvisor.ps1 -DisableEncryption
.\Collect-SqlServerAdvisor.ps1 -OutputPath 'D:\Temp\db01.json'
```

| Parâmetro | Efeito |
| --- | --- |
| `-OutputPath` | Destino do JSON. Padrão: Área de Trabalho, `sql-advisor-<COMPUTERNAME>-yyyyMMdd-HHmmss.json` |
| `-UseSqlAuthentication` | Abre `Get-Credential` para um login SQL; senha vai para `SQLCMDPASSWORD` e é apagada no `finally` |
| `-DisableEncryption` | Omite `-N -C` no sqlcmd. Use só em rede confiável e conforme a política |

O script:

1. Recusa execução sem administrador.
2. Lê `Win32_OperatingSystem` e `Win32_ComputerSystem`.
3. Monta o mapa de instâncias pelo Registro (`Instance Names\SQL` em 64 e 32 bits) e pelos serviços `MSSQLSERVER` / `MSSQL$*`.
4. Lê porta TCP estática e dinâmica em `SuperSocketNetLib\Tcp\IPAll`.
5. Mede o working set do `sqlservr`.
6. Para cada instância em execução, chama `sqlcmd` em `127.0.0.1,porta` ou `.\NOME` com a mesma consulta do Python.
7. Grava UTF-8 **sem BOM**.

Serviço parado gera `facts.error = "O serviço da instância está parado."`. `sqlcmd` ausente gera erro correspondente. Falha SQL entra em `warnings`.

O JSON **não** contém senhas.

### Schema do snapshot (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-11T10:30:00-03:00",
  "host": {
    "computer_name": "DB-RDP-01",
    "os_caption": "Microsoft Windows Server 2022 Standard",
    "os_version": "10.0.20348",
    "total_memory_mb": 65536,
    "free_memory_mb": 16384,
    "logical_cpus": 16
  },
  "instances": [
    {
      "name": "ERP",
      "instance_id": "MSSQL16.ERP",
      "service_name": "MSSQL$ERP",
      "service_state": "Running",
      "start_mode": "Auto",
      "tcp_port": "51433",
      "dynamic_port": null,
      "process_memory_mb": 24576,
      "facts": {
        "connected": true,
        "endpoint": "tcp:DB-RDP-01,51433",
        "error": null,
        "values": {
          "product_version": "16.0.1000.6",
          "max_server_memory_mb": "50000"
        }
      }
    }
  ],
  "warnings": []
}
```

Limites aceitos em `POST /api/import`:

| Campo | Limite |
| --- | --- |
| `schema_version` | exatamente `1` |
| `host.total_memory_mb` | 1 … 16 777 216 |
| `host.logical_cpus` | 1 … 4096 |
| `instances` | até 64 |
| `facts.values` | até 100 chaves; chave ≤ 100 caracteres, valor ≤ 4000 |
| `warnings` | até 100 |
| arquivo no navegador | 2 MB |

Versão desconhecida é rejeitada. Depois do parse, o FastAPI aplica o **mesmo** motor de recomendações da análise ao vivo e acrescenta o aviso `Diagnóstico coletado dentro da sessão RDP em <generated_at>.`.

## Configure-SqlAdvisorWinRMHttps.ps1

Preparação única do canal remoto. Detalhes de quando usar: [acesso-remoto.md](acesso-remoto.md#3-winrm-https-máquina-local-fora-do-domínio).

```powershell
.\Configure-SqlAdvisorWinRMHttps.ps1 -CertificateThumbprint '0123456789ABCDEF0123456789ABCDEF01234567'
.\Configure-SqlAdvisorWinRMHttps.ps1 -CertificateThumbprint '…' -AllowedRemoteAddress '10.20.30.40'
```

| Parâmetro | Padrão | Validação |
| --- | --- | --- |
| `-CertificateThumbprint` | obrigatório | 40–80 hex (espaços ignorados) |
| `-AllowedRemoteAddress` | `LocalSubnet` | lista para `New-NetFirewallRule -RemoteAddress` |

O script **preserva** um listener HTTPS ou uma regra de firewall já existentes, para não sobrescrever configuração corporativa. Confira o listener com:

```powershell
winrm enumerate winrm/config/listener
```

Perfis de firewall criados: **Domain** e **Private**. Porta **5986/TCP**.
