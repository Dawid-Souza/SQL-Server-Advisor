# Segurança

O SQL Server Advisor foi desenhado para rodar na estação do DBA, falar com um servidor de banco e **não** persistir segredo nenhum. Esta página descreve os controles que existem no código e os limites que continuam sendo responsabilidade do operador.

## Superfície local

| Controle | Implementação |
| --- | --- |
| Bind | Uvicorn em `127.0.0.1:9000` (`start.bat` e `app/main.py`) |
| Host header | `TrustedHostMiddleware`: `127.0.0.1`, `localhost`, `testserver` |
| Docs automáticos | `docs_url`, `redoc_url` e `openapi_url` = `None` |
| Cache | `Cache-Control: no-store` em todas as respostas |
| MIME sniffing | `X-Content-Type-Options: nosniff` |
| Referrer | `Referrer-Policy: no-referrer` |
| Subprocessos | lista de argumentos, sem `shell=True` |
| Janela | `CREATE_NO_WINDOW` no Windows |

O painel **não** é um serviço exposto na rede. Colocar um proxy reverso na frente, abrir o firewall ou mudar o bind para `0.0.0.0` amplia a superfície e foge do modelo assumido.

## Validação do alvo

`normalize_server_name()` aceita `.`, `localhost`, IPv4/IPv6 e FQDN/NetBIOS com labels DNS. Rejeita:

- barras `\` e `/` (bloqueia `host\instance` no campo de Windows Server)
- dois-pontos (bloqueia `host:1433` e URLs)
- caracteres fora de `[A-Za-z0-9-]` em cada label
- comprimento > 255

O hostname vai para variáveis de ambiente (`DTA_TARGET_HOST`) e para o parâmetro `-S` do sqlcmd, nunca interpolado em uma linha de comando montada como string.

## Credenciais

### Tempo de vida

Senhas existem apenas no corpo da requisição HTTP local, no modelo Pydantic daquele request e, se necessário, no ambiente do processo filho. Não há disco, cookie, localStorage, log de aplicação nem campo na `ScanResponse`.

O JavaScript chama `clearPasswords()` no sucesso e no erro da análise.

### SQL

`sqlcmd -U usuario` + variável de ambiente `SQLCMDPASSWORD`. A senha **não** entra em `-P`. Depois da execução, o coletor remoto remove `Env:\SQLCMDPASSWORD`.

### Windows

`ConvertTo-SecureString` + `PSCredential` dentro do PowerShell filho. O script é enviado como `-EncodedCommand` (Base64 UTF-16LE). A senha vai na env `DTA_WINDOWS_PASSWORD` do processo filho, e os testes garantem que ela **não** aparece no texto do script.

### Redação

`safe_public_error(message, secret)` substitui a senha por `********` antes de qualquer HTTP 422. A orquestração aplica isso tanto à senha SQL quanto à senha Windows.

## Transporte

| Canal | Criptografia |
| --- | --- |
| Painel ↔ navegador | HTTP em loopback |
| Painel ↔ Windows remoto | WinRM HTTP (Kerberos) ou WinRM HTTPS (certificado de computador) |
| Painel ↔ SQL | `sqlcmd -N` ligado por padrão; `-C` confia no certificado apresentado |
| Coletor RDP | arquivo JSON copiado pelo operador; sem senha no arquivo |

Desmarcar a criptografia SQL só é aceitável em rede já confiável e se a política da organização permitir, para instâncias antigas sem TLS compatível.

O configurador WinRM HTTPS exige certificado com EKU de Server Authentication, chave privada e validade corrente. A regra de firewall nasce restrita a `LocalSubnet` (ou a um IP informado). Listeners e regras já existentes são preservados.

## O que a coleta lê

Somente leitura:

- CIM/WMI: sistema operacional, computador, serviços, processo
- Registro: nomes de instância e portas TCP
- SQL: `SERVERPROPERTY`, `sys.configurations`, DMVs, `sys.databases`, `sys.master_files`, `msdb.dbo.backupset`, `msdb.dbo.suspect_pages`

Nenhum `ALTER`, `sp_configure` ou mudança de serviço é executado pelo painel ou pelo coletor. O T-SQL gerado é copiado para a área de transferência sob ação explícita do usuário.

## O que o operador ainda precisa garantir

- A estação que roda o painel é confiável (loopback HTTP sem TLS).
- A conta usada no WinRM tem o menor privilégio que ainda lê CIM e Registro.
- O login SQL de diagnóstico tem `VIEW SERVER STATE` e pouco mais.
- O JSON do coletor é tratado como dado interno (nomes de banco, portas, versões).
- Scripts T-SQL passam por homologação, janela de mudança e checagem de espaço em disco (tempdb).
- TRUSTWORTHY, DB_CHAINING e Query Store em massa são validados contra dependências reais antes de aplicar.

## Testes de regressão de segurança

Em `tests/test_recommendations.py` e `tests/test_scan_fallback.py`:

- hostname com `; whoami` é rejeitado
- senha administrativa não aparece no script PowerShell nem no JSON da resposta
- login SQL não dispara WinRM só para consultar a instância
- WinRM HTTPS marca `DTA_WINRM_USE_SSL=1` sem reconfigurar a máquina local
- snapshot RDP é analisado sem chamar `discover_host`
