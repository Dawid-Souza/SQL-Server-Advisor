# SQL Server Advisor

Painel web local, em Python e FastAPI, que inventaria um Windows Server, detecta as instâncias do Microsoft SQL Server e propõe uma **baseline explicada** para cada uma. O diagnóstico é somente leitura: o painel gera T-SQL e ações operacionais para revisão, e a aplicação fica sempre a cargo do DBA.

<img width="1446" height="788" alt="image" src="https://github.com/user-attachments/assets/646a386c-1014-4654-9b3d-5b25e0cc99ab" />


O orçamento de memória e CPU reserva recursos para o Windows e para os demais serviços do host. Em servidores com várias instâncias, a parcela restante é dividida de forma conservadora conforme o consumo observado.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Plataforma](https://img.shields.io/badge/Windows-10%2F11%20e%20Server-0078D6?logo=windows&logoColor=white)
![Diagnóstico](https://img.shields.io/badge/Diagnóstico-somente%20leitura-2ea44f)

## Sumário

- [O que o painel entrega](#o-que-o-painel-entrega)
- [Início rápido](#início-rápido)
- [Pré-requisitos](#pré-requisitos)
- [Três formas de analisar um servidor](#três-formas-de-analisar-um-servidor)
- [Como interpretar o resultado](#como-interpretar-o-resultado)
- [Reservas de memória e CPU](#reservas-de-memória-e-cpu)
- [Segurança](#segurança)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Testes](#testes)
- [Documentação](#documentação)
- [Referências técnicas](#referências-técnicas)

## O que o painel entrega

Para cada Windows Server informado, o Advisor:

1. Lê hardware, sistema operacional, serviços e chaves de Registro das instâncias SQL.
2. Consulta a instância com `sqlcmd` (somente leitura) e coleta configurações, DMVs e opções de banco.
3. Calcula um orçamento de RAM e CPU que preserva o Windows e os serviços não-SQL.
4. Compara o estado atual com práticas documentadas pela Microsoft.
5. Separa o que já está conforme do que exige ação, agrupa por categoria e gera um script T-SQL apenas para mudanças que podem ser expressas com segurança.

Áreas cobertas:

| Categoria | Exemplos |
| --- | --- |
| Memória e CPU | `max server memory`, `min server memory`, MAXDOP, Lock Pages in Memory |
| tempdb e arquivos | quantidade e igualdade de arquivos, crescimento fixo, Instant File Initialization, autogrowth de usuário |
| Backup e integridade | compressão, checksum, recência de full/log, PAGE_VERIFY, páginas suspeitas |
| Planos e diagnóstico | optimize for ad hoc workloads (com evidência de cache), Default Trace, Query Store |
| Segurança e superfície | `xp_cmdshell`, OLE Automation, ownership chaining, TRUSTWORTHY, DB_CHAINING |
| Configuração da instância | worker threads, network packet size, recovery interval, priority boost |
| Estatísticas | AUTO_CREATE_STATISTICS, AUTO_UPDATE_STATISTICS |
| Operação | bancos fora de ONLINE, AUTO_CLOSE, AUTO_SHRINK |

Backup, integridade, direitos do Windows e disponibilidade aparecem como **ações operacionais** quando não existe um comando T-SQL universal e seguro (por exemplo, o painel não inventa um `BACKUP DATABASE` sem destino, retenção e RPO).

Documentação detalhada das regras: [docs/recomendacoes.md](docs/recomendacoes.md).

## Início rápido

```powershell
.\setup.bat
.\start.bat
```

Abra [http://127.0.0.1:9000](http://127.0.0.1:9000). O servidor web aceita conexões apenas da máquina local.

O instalador cria o ambiente isolado em `.runtime\venv`. Para usar um Python já configurado:

```powershell
py -3 -m uvicorn app.main:app --host 127.0.0.1 --port 9000
```

No painel, informe o Windows Server (DNS, NetBIOS ou IP), escolha a autenticação e clique em **Analisar servidor**.

## Pré-requisitos

Na **máquina que executa o painel**:

- Windows 10, Windows 11 ou Windows Server
- Python 3.11 ou superior
- Microsoft `sqlcmd` (Command Line Utilities for SQL Server)

No **servidor analisado**, conforme o modo de acesso:

- PowerShell Remoting / WinRM habilitado, **ou**
- SQL Browser e/ou porta TCP acessível com um login SQL autorizado, **ou**
- uma sessão RDP para executar o coletor PowerShell

Permissões SQL recomendadas: `CONNECT SQL` e `VIEW SERVER STATE` (`VIEW SERVER PERFORMANCE STATE` no SQL Server 2022 ou superior). Sem essas permissões, a análise de instância e banco fica parcial.

## Três formas de analisar um servidor

| Modo | Quando usar | O que acontece |
| --- | --- | --- |
| **Painel direto** | Você já tem acesso administrativo ou SQL a partir da máquina local | O FastAPI descobre o host (local, WinRM, DCOM ou SQL Browser) e consulta as instâncias |
| **WinRM HTTPS** | A máquina local está fora do domínio do servidor | Configuração única no servidor; depois o painel analisa com credencial administrativa na porta 5986 |
| **Coletor RDP** | Política impede WinRM, ou você é administrador apenas dentro da sessão RDP | O script gera um JSON na Área de Trabalho; você importa no painel. O servidor remoto não precisa de Python |

Guia completo, incluindo Kerberos, grupo de trabalho e fallbacks: [docs/acesso-remoto.md](docs/acesso-remoto.md).

### WinRM HTTPS (preparação única no servidor)

1. Instale no Windows Server um certificado de computador com **Server Authentication** e o DNS do servidor no CN ou SAN.
2. No painel, baixe `Configure-SqlAdvisorWinRMHttps.ps1`.
3. No servidor, abra o **Windows PowerShell como administrador**:

```powershell
.\Configure-SqlAdvisorWinRMHttps.ps1 -CertificateThumbprint 'THUMBPRINT_DO_CERTIFICADO'
```

Por padrão a regra de firewall limita a porta 5986 à sub-rede local. Para restringir ao IP da máquina do painel:

```powershell
.\Configure-SqlAdvisorWinRMHttps.ps1 -CertificateThumbprint 'THUMBPRINT' -AllowedRemoteAddress 'IP_DA_MAQUINA_LOCAL'
```

No painel: **Informar credencial administrativa**, `DOMINIO\usuario` ou `SERVIDOR\Administrator`, canal **WinRM HTTPS (5986)** e o **nome DNS do certificado**.

### Coletor RDP (sem canal remoto)

```powershell
.\Collect-SqlServerAdvisor.ps1
```

Se a conta Windows não acessar a instância:

```powershell
.\Collect-SqlServerAdvisor.ps1 -UseSqlAuthentication
```

O JSON `sql-advisor-SERVIDOR-data-hora.json` vai para a Área de Trabalho. Importe-o em **Importar diagnóstico JSON**. Detalhes do schema e parâmetros: [docs/coletores.md](docs/coletores.md).

## Como interpretar o resultado

O painel mostra métricas do host (RAM física, reserva do Windows, orçamento SQL, CPUs lógicas) e, para cada instância, uma tabela:

- **Atual** — valor coletado
- **Recomendado** — baseline para aquele host e aquela parcela
- **Por quê** — justificativa, com link para Microsoft Learn quando existir
- Linhas em destaque exigem ação; as demais estão conformes ou aguardam evidência

Há um botão **Copiar script T-SQL** com as mudanças que o motor conseguiu expressar. Revise em homologação, valide espaço em disco (tempdb), dependências de TRUSTWORTHY/DB_CHAINING e a janela de mudança.

Os limites de **7 dias** para backup completo e **24 horas** para backup de log são alertas conservadores. Substitua-os pelos RPO/RTO oficiais do ambiente.

MAXDOP limita o paralelismo **por operador**. Ele não reserva nem limita o uso total de CPU da instância.

## Reservas de memória e CPU

Valores padrão, ajustáveis em **Ajustar reservas**:

| Parâmetro | Padrão | Faixa |
| --- | --- | --- |
| Reserva de RAM (%) | 25% | 10–60 |
| Reserva mínima (GB) | 4 GB | 2–1024 |
| CPUs reservadas | 20% dos lógicos (mínimo 1) | 1–256, deixando ao menos 1 para o SQL |

A reserva efetiva é o **maior** entre:

- a porcentagem da RAM física
- a reserva mínima em GB
- o consumo não-SQL observado no momento da coleta, acrescido de 5% de folga (mínimo 1 GB)

O restante é o orçamento SQL, arredondado para baixo em blocos de 256 MB e dividido entre as instâncias proporcionalmente ao working set observado (piso de 1 GB de peso). Em hosts de laboratório o motor garante uma parcela mínima utilizável ao SQL sem ultrapassar a RAM física.

Fórmulas e exemplos: [docs/arquitetura.md](docs/arquitetura.md#orçamento-de-recursos).

## Segurança

- Bind exclusivo em `127.0.0.1`; Trusted Hosts limitados a `127.0.0.1`, `localhost` e `testserver`
- Documentação automática (Swagger/ReDoc/OpenAPI) desativada
- Hostname validado; subprocessos sem `shell=True`
- Senhas apenas durante a requisição: SQL via `SQLCMDPASSWORD`, Windows via `SecureString` no processo PowerShell filho
- Campos de senha limpos no navegador ao final da tentativa
- Coleta somente leitura; o JSON do coletor não grava senhas
- Conexão SQL criptografada por padrão (`-N`); certificado confiável por padrão (`-C`)

Detalhes: [docs/seguranca.md](docs/seguranca.md).

## Estrutura do projeto

```
dbdba_cfg/
├── app/
│   ├── main.py                 # FastAPI: UI, /api/scan, /api/import, downloads
│   ├── models.py               # Contratos Pydantic da API e do snapshot
│   ├── services/
│   │   ├── discovery.py        # Inventário Windows (local, WinRM, DCOM, SQL Browser)
│   │   ├── sqlserver.py        # Coleta via sqlcmd
│   │   ├── recommendations.py  # Orçamento + regras + script T-SQL
│   │   └── security.py         # Validação de host e redação de segredos
│   ├── static/                 # CSS e JavaScript do painel
│   └── templates/index.html
├── collector/
│   ├── Collect-SqlServerAdvisor.ps1
│   └── Configure-SqlAdvisorWinRMHttps.ps1
├── tests/
├── setup.bat / start.bat
├── requirements.txt
└── docs/
```

## Testes

```powershell
py -3 -m pip install pytest
py -3 -m pytest tests
```

A suíte cobre orçamento de memória/CPU, MAXDOP, tempdb, opções perigosas, fallback SQL, snapshot RDP, validação de host e o fato de que senhas não entram na linha de comando nem na resposta da API.

## Documentação

| Documento | Conteúdo |
| --- | --- |
| [Arquitetura](docs/arquitetura.md) | Fluxo de análise, descoberta em cascata, orçamento, módulos |
| [Recomendações](docs/recomendacoes.md) | Catálogo das regras, prioridades, T-SQL gerado e o que fica operacional |
| [Acesso remoto](docs/acesso-remoto.md) | Local, WinRM, DCOM, SQL Browser, HTTPS, grupo de trabalho |
| [Coletores](docs/coletores.md) | Scripts PowerShell, parâmetros e schema do JSON |
| [API](docs/api.md) | Endpoints, payloads e códigos de erro |
| [Segurança](docs/seguranca.md) | Superfície de ataque, credenciais, TLS e limites |

## Referências técnicas

- [Configuração de memória do SQL Server](https://learn.microsoft.com/sql/database-engine/configure-windows/server-memory-server-configuration-options)
- [MAXDOP](https://learn.microsoft.com/sql/database-engine/configure-windows/configure-the-max-degree-of-parallelism-server-configuration-option)
- [Recomendações para o tempdb](https://learn.microsoft.com/sql/relational-databases/databases/tempdb-database)
- [Optimize for ad hoc workloads](https://learn.microsoft.com/sql/database-engine/configure-windows/optimize-for-ad-hoc-workloads-server-configuration-option)
- [Backup checksum default](https://learn.microsoft.com/sql/database-engine/configure-windows/backup-checksum-default-server-configuration-option)
- [Instant File Initialization](https://learn.microsoft.com/sql/relational-databases/databases/database-instant-file-initialization)
- [Lock Pages in Memory](https://learn.microsoft.com/sql/database-engine/configure-windows/enable-the-lock-pages-in-memory-option-windows)
- [Opções de configuração do servidor](https://learn.microsoft.com/sql/database-engine/configure-windows/server-configuration-options-sql-server)
