# Documentação do SQL Server Advisor

Índice dos guias. O [README](../README.md) na raiz cobre instalação, início rápido e visão do produto.

| Guia | Conteúdo |
| --- | --- |
| [Arquitetura](arquitetura.md) | Módulos, fluxo de análise, descoberta em cascata, orçamento de RAM/CPU, MAXDOP |
| [Recomendações](recomendacoes.md) | Catálogo das regras, prioridades, T-SQL gerado e ações operacionais |
| [Acesso remoto](acesso-remoto.md) | Local, WinRM Kerberos, WinRM HTTPS, DCOM, SQL Browser, coletor RDP |
| [Coletores](coletores.md) | Scripts PowerShell, parâmetros e schema do JSON (`schema_version: 1`) |
| [API](api.md) | Endpoints, payloads, `ScanResponse` e erros |
| [Segurança](seguranca.md) | Bind local, credenciais, TLS, o que a coleta lê |

O diagnóstico é somente leitura. O painel gera recomendações e T-SQL para revisão; a aplicação no SQL Server permanece manual.
