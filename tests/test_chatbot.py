import httpx
import pytest

from app import chatbot

VARS = ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "OFERTAO_CHAT_PROVEDOR", "OFERTAO_CHAT_MODELO")


@pytest.fixture(autouse=True)
def sem_chaves(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


def _usuario(texto):
    return [{"role": "user", "content": texto}]


def test_sem_provedor_usa_faq():
    resposta, provedor = chatbot.responder(_usuario("Preciso pagar alguma coisa?"))
    assert provedor == "faq"
    assert "gratuito" in resposta


@pytest.mark.parametrize("pergunta, trecho", [
    ("Funciona em Parnamirim?", "Grande Natal"),
    ("como é calculado o transporte?", "ida e volta"),
    ("vale a pena dividir em duas lojas?", "duas lojas"),
    ("Por que não tem Carrefour?", "Carrefour"),
    ("qual a capital da França?", "Posso ajudar"),
    ("Como uso o agente de IA?", "Perguntar ao Agente"),
    ("como funciona o agente?", "Perguntar ao Agente"),
    ("como usar o site?", "3 passos"),
    ("dá pra escrever o pedido e falar que só tenho 50 reais?", "Agente de IA"),
])
def test_faq_encontra_resposta(pergunta, trecho):
    assert trecho in chatbot.responder_faq(pergunta)


def test_prompt_ensina_o_agente():
    sistema = chatbot._prompt_sistema(None)
    assert "AGENTE DE IA" in sistema and "Perguntar ao Agente" in sistema


def test_escolha_do_provedor(monkeypatch):
    assert chatbot.provedor_configurado() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert chatbot.provedor_configurado().nome == "anthropic"
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert chatbot.provedor_configurado().nome == "groq"
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert chatbot.provedor_configurado().nome == "gemini"  # o gratuito tem prioridade
    monkeypatch.setenv("OFERTAO_CHAT_PROVEDOR", "ollama")
    assert chatbot.provedor_configurado().nome == "ollama"
    monkeypatch.setenv("OFERTAO_CHAT_PROVEDOR", "nenhum")
    assert chatbot.provedor_configurado() is None


def test_gemini_recebe_prompt_e_historico(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "chave-teste")
    recebido = {}

    def tratar(req: httpx.Request):
        recebido["url"] = str(req.url)
        recebido["auth"] = req.headers["authorization"]
        recebido["corpo"] = req.read().decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": " Olá! "}}]})

    cliente = httpx.Client(transport=httpx.MockTransport(tratar))
    historico = [
        {"role": "assistant", "content": "mensagem de boas-vindas"},  # descartada: precisa começar no usuário
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "Olá, como ajudo?"},
        {"role": "user", "content": "é grátis?"},
    ]
    resposta, provedor = chatbot.responder(historico, status={"lojas": 20, "produtos": 230}, cliente=cliente)

    assert (resposta, provedor) == ("Olá!", "gemini")
    assert "generativelanguage.googleapis.com" in recebido["url"]
    assert recebido["auth"] == "Bearer chave-teste"
    assert "20 lojas cadastradas" in recebido["corpo"]
    assert "boas-vindas" not in recebido["corpo"]


def test_modelo_sobrecarregado_tenta_o_proximo(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    modelos = []

    def tratar(req: httpx.Request):
        import json
        modelo = json.loads(req.read())["model"]
        modelos.append(modelo)
        if modelo == chatbot.MODELOS_GEMINI[0]:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cliente = httpx.Client(transport=httpx.MockTransport(tratar))
    assert chatbot.responder(_usuario("oi"), cliente=cliente) == ("ok", "gemini")
    assert modelos == list(chatbot.MODELOS_GEMINI)


def test_chave_invalida_nao_tenta_outros_modelos(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    chamadas = []

    def tratar(req):
        chamadas.append(1)
        return httpx.Response(401)

    cliente = httpx.Client(transport=httpx.MockTransport(tratar))
    assert chatbot.responder(_usuario("é gratis?"), cliente=cliente)[1] == "faq"
    assert len(chamadas) == 1


def test_falha_do_provedor_cai_no_faq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    cliente = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429)))
    resposta, provedor = chatbot.responder(_usuario("é gratis?"), cliente=cliente)
    assert provedor == "faq"
    assert "gratuito" in resposta


def test_endpoint_chat():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/chat", json={"mensagens": _usuario("Quais cidades vocês atendem?")})
    assert r.status_code == 200
    assert r.json()["provedor"] == "faq"
    assert "Natal" in r.json()["resposta"]
