// Assistente do Ofertão: botão flutuante + janela de conversa que chama POST /api/chat.
(() => {
  const NOMES = { gemini: "Google Gemini", groq: "Groq (Llama)", ollama: "Ollama (local)", anthropic: "Claude", faq: "respostas automáticas" };
  const SUGESTOES = ["Como funciona?", "De onde vêm os preços?", "Quais cidades atende?", "Como o transporte é calculado?"];
  const CHAVE = "ofertao-chat";

  const css = `
  .oc-botao { position: fixed; right: 20px; bottom: 20px; z-index: 2000; width: 60px; height: 60px; border-radius: 50%;
    border: 0; background: #f26b1d; color: #fff; font-size: 28px; cursor: pointer; display: grid; place-items: center;
    box-shadow: 0 6px 20px rgba(242,107,29,.4); transition: transform .15s; }
  .oc-botao:hover { transform: scale(1.06); background: #df5b10; }
  .oc-janela { position: fixed; right: 20px; bottom: 92px; z-index: 2000; width: 370px; max-width: calc(100vw - 32px);
    height: 540px; max-height: calc(100vh - 120px); background: #fff; border-radius: 18px; border: 1px solid #e3e8f2;
    box-shadow: 0 12px 40px rgba(11,37,96,.22); display: flex; flex-direction: column; overflow: hidden;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #182135; }
  .oc-janela[hidden] { display: none; }
  .oc-topo { background: #123a8c; color: #fff; padding: 14px 16px; display: flex; align-items: center; gap: 10px; }
  .oc-topo b { display: block; font-size: 1rem; }
  .oc-topo small { opacity: .8; font-size: .78rem; }
  .oc-topo button { margin-left: auto; background: none; border: 0; color: #fff; font-size: 22px; cursor: pointer; line-height: 1; }
  .oc-msgs { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; background: #f6f8fc; }
  .oc-msg { max-width: 85%; padding: 10px 13px; border-radius: 14px; font-size: .92rem; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
  .oc-bot { background: #fff; border: 1px solid #e3e8f2; align-self: flex-start; border-bottom-left-radius: 4px; }
  .oc-user { background: #123a8c; color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; }
  .oc-erro { background: #fdecec; color: #b42318; align-self: flex-start; }
  .oc-digitando { color: #5f6b82; font-style: italic; }
  .oc-sugs { display: flex; flex-wrap: wrap; gap: 6px; }
  .oc-sugs button { border: 1px solid #e3e8f2; background: #fff; color: #123a8c; border-radius: 999px; padding: 6px 11px;
    font-size: .82rem; cursor: pointer; font-weight: 600; }
  .oc-sugs button:hover { background: #eef3ff; }
  .oc-form { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #e3e8f2; background: #fff; }
  .oc-form input { flex: 1; min-width: 0; padding: 11px 12px; border: 1px solid #e3e8f2; border-radius: 12px; font-size: .95rem; outline: none; }
  .oc-form input:focus { border-color: #123a8c; box-shadow: 0 0 0 3px rgba(18,58,140,.12); }
  .oc-form button { border: 0; background: #f26b1d; color: #fff; border-radius: 12px; padding: 0 16px; font-weight: 700; cursor: pointer; }
  .oc-form button:disabled { opacity: .5; cursor: default; }
  .oc-rodape { font-size: .72rem; color: #5f6b82; text-align: center; padding: 0 10px 8px; background: #fff; }
  @media (max-width: 900px) {
    body:has(.cta-fixo) .oc-botao { bottom: 88px; }
    body:has(.cta-fixo) .oc-janela { bottom: 160px; height: calc(100vh - 176px); max-height: 540px; }
  }
  @media (max-width: 480px) {
    .oc-janela { right: 8px; left: 8px; width: auto; max-width: none; bottom: 84px; height: calc(100vh - 100px); }
    .oc-botao { right: 14px; bottom: 14px; }
  }`;
  document.head.insertAdjacentHTML("beforeend", `<style>${css}</style>`);

  document.body.insertAdjacentHTML("beforeend", `
    <button class="oc-botao" id="ocBotao" aria-label="Abrir assistente do Ofertão" title="Tire suas dúvidas">💬</button>
    <div class="oc-janela" id="ocJanela" role="dialog" aria-label="Assistente do Ofertão" hidden>
      <div class="oc-topo">
        <span style="font-size:24px">🤖</span>
        <div><b>Assistente Ofertão</b><small id="ocProvedor">Tire suas dúvidas sobre o site</small></div>
        <button id="ocFechar" aria-label="Fechar">×</button>
      </div>
      <div class="oc-msgs" id="ocMsgs" aria-live="polite"></div>
      <form class="oc-form" id="ocForm">
        <input id="ocTexto" placeholder="Digite sua dúvida..." maxlength="1000" autocomplete="off">
        <button id="ocEnviar">Enviar</button>
      </form>
      <div class="oc-rodape" id="ocRodape">As respostas são geradas por IA e podem conter erros.</div>
    </div>`);

  const $ = id => document.getElementById(id);
  const janela = $("ocJanela"), msgs = $("ocMsgs"), texto = $("ocTexto"), enviar = $("ocEnviar");
  let historico = [];
  try { historico = JSON.parse(sessionStorage.getItem(CHAVE)) || []; } catch { historico = []; }
  const salvar = () => { try { sessionStorage.setItem(CHAVE, JSON.stringify(historico.slice(-20))); } catch {} };

  // escapa o HTML e só então aplica **negrito** e quebras de linha
  const formatar = t => t.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]))
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

  function bolha(classe, conteudo) {
    const div = document.createElement("div");
    div.className = `oc-msg ${classe}`;
    div.innerHTML = formatar(conteudo);
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  function renderizar() {
    msgs.innerHTML = "";
    bolha("oc-bot", "Olá! 👋 Sou o assistente do Ofertão. Posso explicar como fazer sua cotação, como o cálculo funciona e de onde vêm os preços.");
    historico.forEach(m => bolha(m.role === "user" ? "oc-user" : "oc-bot", m.content));
    if (!historico.length) {
      const sugs = document.createElement("div");
      sugs.className = "oc-sugs";
      SUGESTOES.forEach(s => {
        const b = document.createElement("button");
        b.type = "button"; b.textContent = s;
        b.onclick = () => perguntar(s);
        sugs.appendChild(b);
      });
      msgs.appendChild(sugs);
    }
  }

  async function perguntar(pergunta) {
    pergunta = pergunta.trim();
    if (!pergunta || enviar.disabled) return;
    msgs.querySelector(".oc-sugs")?.remove();
    historico.push({ role: "user", content: pergunta });
    salvar();
    bolha("oc-user", pergunta);
    texto.value = "";
    enviar.disabled = true;
    const digitando = bolha("oc-bot oc-digitando", "Digitando...");
    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mensagens: historico }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const dados = await r.json();
      digitando.remove();
      historico.push({ role: "assistant", content: dados.resposta });
      salvar();
      bolha("oc-bot", dados.resposta);
      $("ocProvedor").textContent = `Respondendo com ${NOMES[dados.provedor] || dados.provedor}`;
      $("ocRodape").textContent = dados.provedor === "faq"
        ? "IA indisponível no momento: respostas automáticas do FAQ."
        : "As respostas são geradas por IA e podem conter erros.";
    } catch {
      digitando.remove();
      historico.pop();
      salvar();
      bolha("oc-erro", "Não consegui responder agora. Tente de novo em instantes.");
    } finally {
      enviar.disabled = false;
      texto.focus();
    }
  }

  $("ocBotao").onclick = () => {
    janela.hidden = !janela.hidden;
    if (!janela.hidden) { if (!msgs.childElementCount) renderizar(); texto.focus(); }
  };
  $("ocFechar").onclick = () => { janela.hidden = true; $("ocBotao").focus(); };
  $("ocForm").onsubmit = e => { e.preventDefault(); perguntar(texto.value); };
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !janela.hidden) janela.hidden = true; });
})();
