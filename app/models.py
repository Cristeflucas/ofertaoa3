from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, UniqueConstraint


def agora() -> datetime:
    return datetime.now(UTC)


class Mercado(SQLModel, table=True):
    """Loja física real (importada do OpenStreetMap ou cadastrada pelo dono)."""

    id: int | None = Field(default=None, primary_key=True)
    osm_id: str | None = Field(default=None, unique=True, index=True)  # ex: "node/123"
    nome: str
    rede: str | None = Field(default=None, index=True)  # "Atacadão", "Nordestão"...
    tipo: str = "supermarket"  # supermarket | wholesale | convenience
    endereco: str = ""
    bairro: str = ""
    cidade: str = ""
    cnpj: str | None = Field(default=None, index=True)
    lat: float
    lon: float
    atualizado_em: datetime = Field(default_factory=agora)


class ItemGenerico(SQLModel, table=True):
    """Item da feira sem marca ("Arroz branco tipo 1 5kg"): compara o mais barato de cada loja."""

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    nome: str
    categoria: str


class Produto(SQLModel, table=True):
    """Produto específico (marca + tamanho). EAN = código de barras."""

    id: int | None = Field(default=None, primary_key=True)
    ean: str | None = Field(default=None, unique=True, index=True)
    nome: str = Field(index=True)
    marca: str | None = None
    imagem_url: str | None = None
    grupo_id: int | None = Field(default=None, foreign_key="itemgenerico.id", index=True)


class Preco(SQLModel, table=True):
    """Último preço observado de um produto numa loja, sempre com fonte e data."""

    __table_args__ = (UniqueConstraint("mercado_id", "produto_id"),)

    id: int | None = Field(default=None, primary_key=True)
    mercado_id: int = Field(foreign_key="mercado.id", index=True)
    produto_id: int = Field(foreign_key="produto.id", index=True)
    preco: float
    promocao: bool = False
    # atacadao_online | cupom_fiscal | dono | encarte
    fonte: str
    coletado_em: datetime = Field(default_factory=agora)
    valido_ate: datetime | None = None  # ofertas de encarte expiram
    encarte_id: int | None = Field(default=None, foreign_key="encarte.id")


class Encarte(SQLModel, table=True):
    """Encarte/tabloide de ofertas de uma rede, lido por IA."""

    id: int | None = Field(default=None, primary_key=True)
    rede: str = Field(index=True)
    hash_arquivo: str = Field(unique=True)  # evita processar o mesmo encarte duas vezes
    url_origem: str | None = None
    valido_de: datetime | None = None
    valido_ate: datetime | None = None
    itens_extraidos: int = 0
    situacao: str = "processado"  # processado | expirado | sem_lojas
    processado_em: datetime = Field(default_factory=agora)
