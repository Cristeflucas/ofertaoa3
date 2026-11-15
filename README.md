# Ofertão

Monte sua feira e descubra qual supermercado **real** da Grande Natal sai mais barato,
já contando o deslocamento até a loja.

## Como rodar

Pré-requisito: [uv](https://docs.astral.sh/uv/) (ele instala o Python sozinho).

```bash
uv sync                                  # instala as dependências
uv run python -m app.coletar osm         # baixa as lojas reais (OpenStreetMap)
uv run python -m app.coletar atacadao    # baixa os preços da loja online do Atacadão (~3 min)
uv run python -m app.coletar encartes    # lê os encartes de ofertas com IA (precisa da chave abaixo)
uv run uvicorn app.main:app --reload --port 8765   # sobe o site
```

Abra http://localhost:8765. A documentação da API fica em http://localhost:8765/docs.
Para rodar os testes: `uv run pytest`.

### Leitura por IA: encartes e cupons fiscais

Para ler encartes e cupons, é preciso uma chave da API do Claude:

```powershell
$env:ANTHROPIC_API_KEY = "sua-chave"     # PowerShell
uv run uvicorn app.main:app --reload --port 8765
```

### Encartes de ofertas

- **Automático:** `uv run python -m app.coletar encartes` busca o encarte nos sites oficiais
  listados em `FONTES` (`app/coletores/encarte.py`); hoje, a Rede Mais.
- **Manual:** abra http://localhost:8765/admin, escolha a rede, envie o PDF ou a imagem do encarte
  (do site, Instagram ou WhatsApp oficial da rede) e informe o token de administrador.

A IA extrai produto, preço e validade. As ofertas valem para todas as lojas da rede e saem da
comparação quando vencem. Quando o preço em destaque é "só no app", o sistema usa o preço regular.
O mesmo arquivo nunca é processado duas vezes.

### Token de administrador (encartes e painel do dono)

```powershell
$env:OFERTAO_ADMIN_TOKEN = "um-token-secreto"
```

`POST /api/mercados/{id}/precos` com o cabeçalho `X-Token` e o corpo
`{"itens": [{"ean": "789...", "nome": "Arroz X 5kg", "preco": 24.9}]}`.

## De onde vêm os dados

| Fonte | O que traz | Observação |
|---|---|---|
| OpenStreetMap (Overpass API) | ~150 lojas reais em Natal, Parnamirim, São Gonçalo, Macaíba, Extremoz, Ceará-Mirim, São José de Mipibu e Nísia Floresta | Dados © colaboradores do OSM (ODbL) |
| Catálogo público VTEX do Atacadão | Preços reais com código de barras (EAN) | É o preço da **loja online**; pode diferir da gôndola |
| Cupom fiscal (NFC-e) enviado pelo usuário | Preços reais pagos em **qualquer** loja | Foto lida pelo Claude; a consulta da SEFAZ-RN tem captcha e não é usada |
| Encartes de ofertas das redes | Promoções da semana com validade | Lidos pelo Claude; aplicados a todas as lojas da rede |
| Painel do dono | Preços informados pela própria loja | Protegido por token |

Fontes que **não** são usadas: Carrefour (bloqueia acesso automatizado) e Nordestão/Favorito
(a API da VipCommerce exige credenciais; só com parceria da rede).

## Estrutura

| Arquivo | O que faz |
|---|---|
| `app/models.py` | Tabelas: `Mercado`, `ItemGenerico`, `Produto` (EAN), `Preco` (fonte + data) |
| `app/catalogo.py` | Itens da feira ("Arroz tipo 1 5kg") e regras para encaixar produtos reais neles |
| `app/coletores/osm.py` | Importa as lojas reais |
| `app/coletores/vtex.py` | Coletor de preços do Atacadão |
| `app/coletores/cupom.py` | Lê o cupom fiscal com o Claude |
| `app/recommender.py` | Ranking das lojas + divisão da feira em 2 lojas + explicação |
| `app/main.py` | API FastAPI e página web |
| `static/index.html` | Página com mapa (Leaflet + OpenStreetMap) |

## Como o motor decide

`custo_total = soma(preço × quantidade) + distância ida e volta × R$/km`

- **Item genérico**: usa o produto mais barato daquele tipo em cada loja.
- **Produto específico**: usa o preço daquele código de barras.
- Só entram no ranking as lojas com preço para pelo menos um item do carrinho, e só preços com até 30 dias.
- Item que falta numa loja é estimado pela mediana das outras + 20%.
