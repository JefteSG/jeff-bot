
import React, { useState, useEffect, useCallback, useRef } from "react";

// API helpers
const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
async function fetchPending() {
  const response = await fetch(`${API_BASE}/api/messages/queue?status=pending`);
  if (!response.ok) throw new Error("Erro ao buscar mensagens pendentes");
  return response.json();
}
async function approveMessage(id, reply) {
  const options = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (reply !== undefined && reply !== null) {
    options.body = JSON.stringify({ final_reply: reply });
  } else {
    options.body = JSON.stringify({});
  }
  const response = await fetch(`${API_BASE}/api/messages/queue/${id}/approve`, options);
  if (!response.ok) throw new Error("Erro ao aprovar mensagem");
  return response.json();
}
async function selfReplied(id) {
  const response = await fetch(`${API_BASE}/api/messages/queue/${id}/self-replied`, { method: "POST" });
  if (!response.ok) throw new Error("Erro ao marcar como respondido");
  return response.json();
}

// Paleta e estilos escuros
const colors = {
  bg: "#0f0f1a",
  surface: "#16162a",
  card: "#1c1c34",
  border: "#2a2a4a",
  text: "#e2e2f0",
  textDim: "#5a5a7a",
  accent: "#7c6af7",
  green: "#4ade80",
  amber: "#fbbf24",
  red: "#f87171",
};
const s = {
  page: { minHeight: "100vh", background: colors.bg, color: colors.text, fontFamily: "'Inter', system-ui, sans-serif", padding: "0 0 48px 0" },
  header: { background: colors.surface, borderBottom: `1px solid ${colors.border}`, padding: "16px 24px", display: "flex", alignItems: "center", justifyContent: "space-between", position: "sticky", top: 0, zIndex: 10 },
  headerLeft: { display: "flex", alignItems: "center", gap: "10px" },
  headerTitle: { fontSize: "15px", fontWeight: 600, color: colors.text, margin: 0, letterSpacing: "0.01em" },
  headerBadge: { background: colors.accent + "22", color: colors.accent, fontSize: "11px", fontWeight: 600, padding: "2px 8px", borderRadius: "999px", border: `1px solid ${colors.accent}33` },
  headerMeta: { fontSize: "12px", color: colors.textDim, display: "flex", alignItems: "center", gap: "8px" },
  pulseDot: { width: "7px", height: "7px", borderRadius: "50%", background: colors.green, display: "inline-block", flexShrink: 0 },
  content: { maxWidth: "680px", margin: "0 auto", padding: "24px 16px" },
  errorBanner: { background: colors.red + "22", border: `1px solid ${colors.red}44`, color: colors.red, padding: "10px 14px", borderRadius: "8px", fontSize: "13px", marginBottom: "16px", display: "flex", alignItems: "center", gap: "8px" },
  loadingWrap: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", paddingTop: "80px", gap: "12px", color: colors.textDim, fontSize: "13px" },
  spinner: { width: "24px", height: "24px", border: `2px solid ${colors.border}` , borderTopColor: colors.accent, borderRadius: "50%", animation: "spin 0.7s linear infinite" },
  emptyState: { textAlign: "center", paddingTop: "80px", color: colors.textDim, fontSize: "13px", lineHeight: "1.7" },
  groupLabel: { fontSize: "11px", fontWeight: 600, color: colors.textDim, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "8px", marginTop: "24px", paddingLeft: "2px" },
  card: { background: colors.card, border: `1px solid ${colors.border}`, borderRadius: "10px", padding: "14px 16px", marginBottom: "8px", transition: "border-color 0.15s" },
  cardHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "10px" },
  senderName: { fontSize: "13px", fontWeight: 600, color: colors.text },
  timestamp: { fontSize: "11px", color: colors.textDim, fontVariantNumeric: "tabular-nums" },
  discordId: { fontSize: "11px", color: colors.textDim, marginBottom: "10px", fontFamily: "monospace" },
  label: { fontSize: "10px", fontWeight: 600, color: colors.textDim, textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: "4px" },
  originalMsg: { fontSize: "13px", color: colors.textDim, lineHeight: "1.5", marginBottom: "10px", padding: "8px 10px", background: colors.surface, borderRadius: "6px", border: `1px solid ${colors.border}` },
  botSuggestionWrap: { background: colors.surface, border: `1px solid ${colors.accent}33`, borderRadius: "6px", padding: "8px 10px", marginBottom: "12px" },
  botSuggestion: { fontFamily: "'JetBrains Mono', monospace", fontSize: "12px", color: colors.accent, lineHeight: "1.6", margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" },
  actions: { display: "flex", gap: "6px", flexWrap: "wrap" },
  btn: { fontSize: "12px", fontWeight: 500, padding: "5px 12px", borderRadius: "6px", border: "1px solid", cursor: "pointer", transition: "opacity 0.15s", lineHeight: "1.4", userSelect: "none" },
  btnApprove: { background: colors.green + "18", borderColor: colors.green + "44", color: colors.green },
  btnEdit: { background: colors.amber + "18", borderColor: colors.amber + "44", color: colors.amber },
  btnSelf: { background: colors.textDim + "18", borderColor: colors.textDim + "44", color: colors.textDim },
  btnSend: { background: colors.accent + "22", borderColor: colors.accent + "55", color: colors.accent },
  btnCancel: { background: "transparent", borderColor: colors.border, color: colors.textDim },
  btnDisabled: { opacity: 0.45, cursor: "not-allowed" },
  editArea: { marginBottom: "8px" },
  textarea: { width: "100%", background: colors.surface, border: `1px solid ${colors.accent}66`, borderRadius: "6px", color: colors.accent, fontFamily: "'JetBrains Mono', monospace", fontSize: "12px", lineHeight: "1.6", padding: "8px 10px", resize: "vertical", outline: "none", boxSizing: "border-box", minHeight: "72px", marginBottom: "6px" },
};

function formatTime(iso) {
  try { const d = new Date(iso); return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }); } catch { return "—"; }
}
function formatLastUpdate(date) {
  if (!date) return null;
  return date.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}


function RemetenteCard({ group, onAction }) {
  const [selectedId, setSelectedId] = useState(null);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [loadingId, setLoadingId] = useState(null);

  const handleSelect = (msg) => {
    setSelectedId(msg.id);
    setEditing(false);
    setEditText(msg.suggested_reply || "");
  };
  const handleEditOpen = (msg) => {
    setSelectedId(msg.id);
    setEditing(true);
    setEditText(msg.suggested_reply || "");
  };
  const handleApprove = async (msg) => {
    setLoadingId(msg.id);
    await onAction(() => approveMessage(msg.id));
    setLoadingId(null);
    setSelectedId(null);
    setEditing(false);
  };
  const handleSendEdit = async (msg) => {
    if (!editText.trim()) return;
    setLoadingId(msg.id);
    await onAction(() => approveMessage(msg.id, editText.trim()));
    setLoadingId(null);
    setSelectedId(null);
    setEditing(false);
  };
  const handleSelfReplied = async (msg) => {
    setLoadingId(msg.id);
    await onAction(() => selfReplied(msg.id));
    setLoadingId(null);
    setSelectedId(null);
    setEditing(false);
  };

  return (
    <div style={s.card}>
      <div style={s.cardHeader}>
        <span style={s.senderName}>{group.sender_name || "(sem nome)"}</span>
        <span style={s.discordId}>{group.discord_id}</span>
      </div>
      {group.msgs.map((msg) => (
        <div key={msg.id} style={{ marginBottom: 16, border: selectedId === msg.id ? `1px solid ${colors.accent}` : "1px solid transparent", borderRadius: 6, padding: 6, background: selectedId === msg.id ? colors.surface : "none" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={s.timestamp}>{formatTime(msg.created_at)}</span>
            <button style={{ ...s.btn, fontSize: 11, padding: "2px 8px", marginLeft: 8, background: selectedId === msg.id ? colors.accent + "22" : colors.surface, color: colors.accent, borderColor: colors.accent + "33" }} onClick={() => handleSelect(msg)} disabled={loadingId === msg.id}>
              {selectedId === msg.id ? "Selecionada" : "Selecionar"}
            </button>
          </div>
          <div style={s.label}>mensagem original</div>
          <div style={s.originalMsg}>{msg.original_msg}</div>
          <div style={s.label}>sugestão do bot</div>
          <div style={s.botSuggestionWrap}>
            <pre style={s.botSuggestion}>{msg.suggested_reply}</pre>
          </div>
          {selectedId === msg.id && (
            editing ? (
              <div style={s.editArea}>
                <textarea
                  style={s.textarea}
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  disabled={loadingId === msg.id}
                  autoFocus
                  rows={3}
                />
                <div style={s.actions}>
                  <button style={{ ...s.btn, ...s.btnSend, ...(loadingId === msg.id ? s.btnDisabled : {}) }} onClick={() => handleSendEdit(msg)} disabled={loadingId === msg.id}>
                    {loadingId === msg.id ? "enviando…" : "Enviar"}
                  </button>
                  <button style={{ ...s.btn, ...s.btnCancel }} onClick={() => setEditing(false)} disabled={loadingId === msg.id}>
                    cancelar
                  </button>
                </div>
              </div>
            ) : (
              <div style={s.actions}>
                <button style={{ ...s.btn, ...s.btnApprove, ...(loadingId === msg.id ? s.btnDisabled : {}) }} onClick={() => handleApprove(msg)} disabled={loadingId === msg.id}>
                  {loadingId === msg.id ? "…" : "✓ Aprovar"}
                </button>
                <button style={{ ...s.btn, ...s.btnEdit, ...(loadingId === msg.id ? s.btnDisabled : {}) }} onClick={() => handleEditOpen(msg)} disabled={loadingId === msg.id}>
                  ✎ Editar
                </button>
                <button style={{ ...s.btn, ...s.btnSelf, ...(loadingId === msg.id ? s.btnDisabled : {}) }} onClick={() => handleSelfReplied(msg)} disabled={loadingId === msg.id}>
                  ↩ Respondi eu mesmo
                </button>
              </div>
            )
          )}
        </div>
      ))}
    </div>
  );
}

export default function Inbox() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdate, setLastUpdate] = useState(null);
  const intervalRef = useRef(null);

  const loadMessages = useCallback(async (isInitial = false) => {
    try {
      const data = await fetchPending();
      setMessages(data);
      setError(null);
      setLastUpdate(new Date());
    } catch (err) {
      setError(err.message || "Falha ao conectar com a API.");
    } finally {
      if (isInitial) setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMessages(true);
    intervalRef.current = setInterval(() => loadMessages(false), 5000);
    return () => clearInterval(intervalRef.current);
  }, [loadMessages]);

  async function handleAction(id, apiFn) {
    setMessages((prev) => prev.filter((m) => m.id !== id));
    try {
      await apiFn();
    } catch (err) {
      setError(`Ação falhou: ${err.message}`);
      loadMessages(false);
    }
  }

  // Agrupa por discord_id, mas mantém nome para exibição
  const grouped = messages.reduce((acc, msg) => {
    const key = msg.discord_id || "desconhecido";
    if (!acc[key]) acc[key] = { sender_name: msg.sender_name, discord_id: msg.discord_id, msgs: [] };
    acc[key].msgs.push(msg);
    return acc;
  }, {});
  const totalPending = messages.length;

  return (
    <div style={s.page}>
      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
        button:hover:not(:disabled) { opacity: 0.8; }
        textarea:focus { box-shadow: 0 0 0 2px #7c6af733; }
        * { box-sizing: border-box; }
      `}</style>
      <header style={s.header}>
        <div style={s.headerLeft}>
          <h1 style={s.headerTitle}>Jeff Bot Inbox</h1>
          {totalPending > 0 && (
            <span style={s.headerBadge}>{totalPending} pendente{totalPending !== 1 ? "s" : ""}</span>
          )}
        </div>
        <div style={s.headerMeta}>
          <span style={s.pulseDot} />
          {lastUpdate ? `atualizado às ${formatLastUpdate(lastUpdate)}` : "aguardando…"}
        </div>
      </header>
      <main style={s.content}>
        {error && (
          <div style={s.errorBanner}>
            <span>⚠</span>
            <span>{error}</span>
          </div>
        )}
        {loading && (
          <div style={s.loadingWrap}>
            <div style={s.spinner} />
            <span>carregando mensagens…</span>
          </div>
        )}
        {!loading && totalPending === 0 && !error && (
          <div style={s.emptyState}>
            <div style={{ fontSize: "28px", marginBottom: "8px", opacity: 0.5 }}>✓</div>
            <div>Nenhuma mensagem pendente.</div>
            <div style={{ marginTop: "4px", fontSize: "12px" }}>Verificando novamente a cada 5 segundos.</div>
          </div>
        )}
        {!loading &&
          Object.entries(grouped).map(([discord_id, group]) => (
            <div key={discord_id}>
              <div style={s.groupLabel}>
                {group.sender_name || "(sem nome)"} <span style={{ color: colors.textDim, fontSize: 10 }}>({discord_id})</span> — {group.msgs.length} mensagem{group.msgs.length !== 1 ? "s" : ""}
              </div>
              <RemetenteCard group={group} onAction={(apiFn) => handleAction(null, apiFn)} />
            </div>
          ))}
      </main>
    </div>
  );
}
