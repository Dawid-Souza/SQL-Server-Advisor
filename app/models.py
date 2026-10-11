from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.security import normalize_server_name


class ScanRequest(BaseModel):
    server: str = Field(min_length=1, max_length=255)
    windows_authentication: Literal["current", "credentials"] = "current"
    windows_username: str | None = Field(default=None, max_length=256)
    windows_password: str | None = Field(default=None, max_length=512)
    winrm_use_ssl: bool = False
    authentication: Literal["windows", "sql"] = "windows"
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    reserve_memory_percent: int = Field(default=25, ge=10, le=60)
    reserve_memory_gb: int = Field(default=4, ge=2, le=1024)
    reserve_logical_cpus: int | None = Field(default=None, ge=1, le=256)
    login_timeout_seconds: int = Field(default=8, ge=3, le=30)
    encrypt_connection: bool = True
    trust_server_certificate: bool = True

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        return normalize_server_name(value)

    @model_validator(mode="after")
    def validate_credentials(self) -> "ScanRequest":
        if self.windows_authentication == "credentials":
            if not self.windows_username or not self.windows_password:
                raise ValueError("Usuário e senha do Windows são obrigatórios para a credencial administrativa.")
        else:
            self.windows_username = None
            self.windows_password = None
        if self.authentication == "sql":
            if not self.username or not self.password:
                raise ValueError("Usuário e senha são obrigatórios para autenticação SQL.")
        else:
            self.username = None
            self.password = None
        return self


class RecommendationItem(BaseModel):
    key: str
    category: str = "Instância"
    label: str
    current: str
    recommended: str
    rationale: str
    priority: Literal["critical", "high", "medium", "info"]
    change_required: bool
    reference: str | None = None


class InstanceReport(BaseModel):
    instance_name: str
    endpoint: str
    service_state: str
    service_start_mode: str | None = None
    version: str | None = None
    edition: str | None = None
    product_level: str | None = None
    connected: bool
    connection_error: str | None = None
    recommendations: list[RecommendationItem] = Field(default_factory=list)
    script: str | None = None
    notes: list[str] = Field(default_factory=list)


class HostSummary(BaseModel):
    computer_name: str
    os_caption: str
    os_version: str
    total_memory_mb: int
    free_memory_mb: int
    logical_cpus: int
    sql_memory_budget_mb: int
    reserved_memory_mb: int
    observed_non_sql_memory_mb: int
    sql_cpu_budget: int
    reserved_logical_cpus: int


class ScanResponse(BaseModel):
    scanned_at: str
    host: HostSummary
    instances: list[InstanceReport]
    warnings: list[str] = Field(default_factory=list)
    methodology: list[str] = Field(default_factory=list)


class SnapshotHost(BaseModel):
    computer_name: str = Field(min_length=1, max_length=255)
    os_caption: str = Field(default="Windows Server", max_length=500)
    os_version: str = Field(default="Não informado", max_length=200)
    total_memory_mb: int = Field(gt=0, le=16_777_216)
    free_memory_mb: int = Field(ge=0, le=16_777_216)
    logical_cpus: int = Field(gt=0, le=4096)


class SnapshotSqlFacts(BaseModel):
    connected: bool = False
    endpoint: str = Field(default="", max_length=500)
    error: str | None = Field(default=None, max_length=2000)
    values: dict[str, str] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def limit_values(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("O diagnóstico contém configurações demais.")
        return {str(key)[:100]: str(item)[:4000] for key, item in value.items()}


class SnapshotInstance(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(default="", max_length=256)
    service_name: str = Field(default="", max_length=256)
    service_state: str = Field(default="Desconhecido", max_length=100)
    start_mode: str | None = Field(default=None, max_length=100)
    tcp_port: str | None = Field(default=None, max_length=10)
    dynamic_port: str | None = Field(default=None, max_length=10)
    process_memory_mb: int = Field(default=0, ge=0, le=16_777_216)
    facts: SnapshotSqlFacts = Field(default_factory=SnapshotSqlFacts)


class SnapshotDocument(BaseModel):
    schema_version: Literal[1]
    generated_at: str = Field(max_length=100)
    host: SnapshotHost
    instances: list[SnapshotInstance] = Field(default_factory=list, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class ImportSnapshotRequest(BaseModel):
    snapshot: SnapshotDocument
    reserve_memory_percent: int = Field(default=25, ge=10, le=60)
    reserve_memory_gb: int = Field(default=4, ge=2, le=1024)
    reserve_logical_cpus: int | None = Field(default=None, ge=1, le=256)
