from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ItemCarrinho(BaseModel):
    """Ou um item genérico (qualquer marca, pega o mais barato) ou um produto específico."""

    grupo_id: int | None = None
    produto_id: int | None = None
    quantidade: float = Field(default=1, gt=0)

    @model_validator(mode="after")
    def _um_dos_dois(self):
        if (self.grupo_id is None) == (self.produto_id is None):
            raise ValueError("Informe grupo_id OU produto_id")
        return self

    @property
    def chave(self) -> tuple[str, int]:
        return ("g", self.grupo_id) if self.grupo_id is not None else ("p", self.produto_id)


class PedidoRecomendacao(BaseModel):
    lat: float
    lon: float
    itens: list[ItemCarrinho] = Field(min_length=1)
    raio_km: float = Field(default=15, gt=0)
    custo_km: float = Field(default=0.80, ge=0, description="Custo de deslocamento por km (R$)")
    max_idade_dias: int = Field(default=30, gt=0, description="Ignora preços mais antigos que isso")
    permitir_divisao: bool = True


class LinhaItem(BaseModel):
    pedido: str  # o que o usuário pediu ("Arroz branco tipo 1 5kg")
    produto_id: int
    produto: str  # o que foi encontrado ("Arroz Kicaldo Agulhinha Tipo 1 5kg")
    quantidade: float
    preco_unitario: float
    subtotal: float
    promocao: bool
    fonte: str
    coletado_em: datetime


class ResultadoMercado(BaseModel):
    mercado_id: int
    nome: str
    rede: str | None
    endereco: str
    cidade: str
    lat: float
    lon: float
    distancia_km: float
    subtotal_carrinho: float
    custo_deslocamento: float
    custo_total: float
    itens: list[LinhaItem]
    faltantes: list[str]
    completo: bool


class ParteDivisao(BaseModel):
    mercado_id: int
    nome: str
    itens: list[LinhaItem]
    subtotal: float


class ResultadoDivisao(BaseModel):
    partes: list[ParteDivisao]
    distancia_rota_km: float
    custo_deslocamento: float
    custo_total: float
    economia_vs_melhor_unico: float


class RespostaRecomendacao(BaseModel):
    melhor: ResultadoMercado | None
    ranking: list[ResultadoMercado]
    divisao: ResultadoDivisao | None
    lojas_no_raio: int
    lojas_sem_dados: int
    explicacao: str


class PrecoDono(BaseModel):
    ean: str | None = None
    nome: str
    preco: float = Field(gt=0)
    promocao: bool = False
    item_generico: str | None = None  # slug do item da feira, quando o nome não basta para encaixar


class PrecosDono(BaseModel):
    itens: list[PrecoDono] = Field(min_length=1)


class MensagemChat(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class PedidoChat(BaseModel):
    mensagens: list[MensagemChat] = Field(min_length=1, max_length=40)


class RespostaChat(BaseModel):
    resposta: str
    provedor: str  # gemini, groq, ollama, anthropic ou faq (respostas locais, sem IA externa)
