"""Leitura de imagens (encartes e cupons fiscais) por IA, com saída estruturada.

Provedor escolhido pelas variáveis de ambiente (ou OFERTAO_VISAO_PROVEDOR=gemini|anthropic):

- GEMINI_API_KEY    -> Google Gemini, plano gratuito (padrão quando a chave existe)
- ANTHROPIC_API_KEY -> Claude (pago)

Os dois recebem a imagem + instruções e devolvem JSON validado por um modelo Pydantic.
"""

import base64
import json
import logging
import os
import time
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

URL_GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
# Para ler preços, o "flash" erra menos; o "lite" é a reserva quando o flash está sobrecarregado.
MODELOS_GEMINI = ("gemini-flash-latest", "gemini-flash-lite-latest")
MODELO_CLAUDE = "claude-opus-5-5"
STATUS_TENTAR_DE_NOVO = {429, 500, 502, 503, 504}
TENTATIVAS_POR_MODELO = 2
TIMEOUT_S = 90  # uma página costuma levar 10–40 s; mais que isso, o modelo está travado: tenta o próximo


class SemCredencial(RuntimeError):
    def __init__(self):
        super().__init__("Leitura por IA indisponível: configure GEMINI_API_KEY (grátis) ou ANTHROPIC_API_KEY.")


class FalhaLeitura(RuntimeError):
    """O provedor de IA não conseguiu responder (fora do ar, cota esgotada, resposta inválida)."""


class Leitor(Protocol):
    nome: str

    def ler(self, sistema: str, imagem: bytes | None, media_type: str, texto: str,
            formato: type[T]) -> T: ...


# ---------- Gemini (API nativa: aceita JSON Schema completo na resposta) ----------

def _schema_gemini(formato: type[BaseModel]) -> dict:
    """JSON Schema do Pydantic, sem $defs (o Gemini prefere o schema todo expandido)."""
    schema = formato.model_json_schema()
    defs = schema.pop("$defs", {})

    def expandir(no):
        if isinstance(no, dict):
            if "$ref" in no:
                return expandir(defs[no["$ref"].split("/")[-1]])
            return {k: expandir(v) for k, v in no.items() if k != "title"}
        if isinstance(no, list):
            return [expandir(v) for v in no]
        return no

    return expandir(schema)


class LeitorGemini:
    nome = "gemini"

    def __init__(self, chave: str, modelos: tuple[str, ...] = MODELOS_GEMINI,
                 http: httpx.Client | None = None, espera_s: float = 3.0):
        self.chave, self.modelos, self.espera_s = chave, modelos, espera_s
        self.http = http or httpx.Client(timeout=TIMEOUT_S)

    def ler(self, sistema, imagem, media_type, texto, formato):
        partes = []
        if imagem:
            partes.append({"inline_data": {"mime_type": media_type,
                                           "data": base64.standard_b64encode(imagem).decode()}})
        partes.append({"text": texto})
        corpo = {
            "system_instruction": {"parts": [{"text": sistema}]},
            "contents": [{"role": "user", "parts": partes}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 32000,
                "responseMimeType": "application/json",
                "responseJsonSchema": _schema_gemini(formato),
            },
        }
        ultimo_erro = "nenhum modelo configurado"
        for modelo in self.modelos:
            for tentativa in range(TENTATIVAS_POR_MODELO):
                try:
                    r = self.http.post(URL_GEMINI.format(modelo=modelo), json=corpo,
                                       headers={"x-goog-api-key": self.chave})
                except httpx.TimeoutException:
                    ultimo_erro = f"{modelo}: tempo esgotado"
                    break
                if r.status_code in (400, 401, 403):
                    msg = _mensagem_erro(r)
                    if "API key" in msg or r.status_code in (401, 403):
                        raise SemCredencial() from FalhaLeitura(msg)
                    raise FalhaLeitura(f"Gemini recusou o pedido: {msg}")
                if r.status_code == 404:  # modelo não existe para esta conta
                    ultimo_erro = f"{modelo}: {_mensagem_erro(r)}"
                    break
                if r.status_code in STATUS_TENTAR_DE_NOVO:
                    ultimo_erro = f"{modelo}: HTTP {r.status_code} {_mensagem_erro(r)}"
                    if tentativa + 1 < TENTATIVAS_POR_MODELO:
                        time.sleep(self.espera_s * (tentativa + 1))
                    continue
                r.raise_for_status()
                try:
                    return _interpretar(r.json(), formato)
                except (ValueError, ValidationError) as e:
                    ultimo_erro = f"{modelo}: resposta inválida ({e})"
                    break
            log.info("Visão: %s; tentando o próximo modelo", ultimo_erro)
        raise FalhaLeitura(f"Gemini indisponível agora ({ultimo_erro}). Tente de novo em alguns minutos.")


def _mensagem_erro(r: httpx.Response) -> str:
    try:
        dados = r.json()
        dados = dados[0] if isinstance(dados, list) else dados
        return dados["error"]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return r.text[:200]


def _interpretar(dados: dict, formato: type[T]) -> T:
    candidato = (dados.get("candidates") or [{}])[0]
    if candidato.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
        raise ValueError(f"bloqueado ({candidato['finishReason']})")
    texto = "".join(p.get("text", "") for p in candidato.get("content", {}).get("parts", [])
                    if not p.get("thought"))
    if not texto.strip():
        raise ValueError(f"vazia (finishReason={candidato.get('finishReason')})")
    return formato.model_validate(json.loads(texto))


# ---------- Claude ----------

class LeitorClaude:
    nome = "anthropic"

    def __init__(self, cliente=None, modelo: str | None = None):
        import anthropic

        self.cliente = cliente or anthropic.Anthropic()
        self.modelo = modelo or os.getenv("OFERTAO_MODELO", MODELO_CLAUDE)

    def ler(self, sistema, imagem, media_type, texto, formato):
        conteudo: list[dict] = []
        if imagem:
            conteudo.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(imagem).decode(),
            }})
        conteudo.append({"type": "text", "text": texto})
        try:
            resposta = self.cliente.beta.messages.parse(
                model=self.modelo,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=sistema,
                messages=[{"role": "user", "content": conteudo}],
                output_format=formato,
            )
        except TypeError as e:
            if "authentication" in str(e):  # SDK sem chave configurada
                raise SemCredencial() from e
            raise
        if resposta.stop_reason == "refusal" or resposta.parsed_output is None:
            raise ValueError("Não foi possível ler esta imagem.")
        return resposta.parsed_output


# ---------- escolha ----------

def leitor_padrao() -> Leitor:
    forcado = os.getenv("OFERTAO_VISAO_PROVEDOR", "").strip().lower()
    modelo = os.getenv("OFERTAO_VISAO_MODELO")
    if forcado in ("", "gemini") and (chave := os.getenv("GEMINI_API_KEY")):
        return LeitorGemini(chave, (modelo,) if modelo else MODELOS_GEMINI)
    if forcado in ("", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        return LeitorClaude(modelo=modelo)
    raise SemCredencial()


def como_leitor(cliente) -> Leitor:
    """Aceita um Leitor pronto, um cliente da Anthropic (ou um falso, nos testes) ou None."""
    if cliente is None:
        return leitor_padrao()
    if hasattr(cliente, "ler"):
        return cliente
    return LeitorClaude(cliente)
