# Jeff Bot

Assistente pessoal para Discord com roteamento de mensagens, fila de aprovação, triagem de erros e captura de tasks no Notion.

## Stack

- Python 3.11+
- FastAPI
- selfcord.py
- SQLite
- DeepSeek API (function calling)
- Notion API
- React + Vite

## Estrutura

```text
jeff-bot/
├── bot/
│   ├── main.py
│   ├── handlers.py
│   └── router.py
├── api/
│   ├── main.py
│   ├── routes/
│   │   ├── messages.py
│   │   ├── knowledge.py
│   │   └── tasks.py
│   └── services/
│       ├── db.py
│       ├── llm.py
│       ├── notion.py
│       └── classifier.py
├── db/
│   └── schema.sql
├── ui/
│   ├── package.json
│   ├── index.html
│   └── src/
│       ├── main.jsx
│       └── Inbox.jsx
├── config.py
├── requirements.txt
└── .env.example
```

## Setup backend

1. Crie e ative ambiente virtual Python.
2. Instale dependências:

```bash
pip install -r requirements.txt
python -m pip install --no-deps selfcord.py==1.0.3
```

3. Copie variáveis de ambiente:

```bash
cp .env.example .env
```

Personalidade do bot via `.env`:

- `BOT_PERSONALITY=jeff_direct` (padrão)
- `BOT_PERSONALITY=friendly_mentor`
- `BOT_PERSONALITY=strict_sre`
- `BOT_PERSONALITY_CUSTOM=` (opcional, para instruções extras)

A personalidade ativa é registrada nos logs de inicialização e também em `meta_json` das filas.

4. Inicie API:

```bash
/home/jefte/Documents/project/.venv/bin/python -m uvicorn api.main:app --app-dir /home/jefte/Documents/project/jeff-bot --host 127.0.0.1 --port 8000
```

5. Teste healthcheck:

```bash
curl http://127.0.0.1:8000/health
```

## Fluxo principal do roteador

1. Cria remetente automaticamente em `approval` se não existir.
2. Se remetente está em `always_me`, só notifica inbox (sem resposta automática).
3. Classifica intenção (`routine_question`, `error_report`, `task_request`, `greeting`, `unknown`).
4. `routine_question`: tenta KB por keywords; se confiança atender limiar responde direto, senão fila de aprovação.
5. `error_report`: triagem até 3 perguntas; após contexto suficiente gera diagnóstico e coloca em aprovação.
6. `task_request`: cria task no Notion, responde `anotado!` e persiste na tabela `tasks`.

## Endpoints principais

- `POST /ingest/message`: entrada canônica de mensagem.
- `GET /api/messages/queue?status=pending`: inbox de aprovação.
- `POST /api/messages/queue/{id}/approve`: aprovar resposta.
- `POST /api/messages/queue/{id}/reject`: rejeitar sugestão.
- `POST /api/messages/queue/{id}/self-replied`: marcar respondida manualmente.
- `GET /api/knowledge`: listar knowledge base.
- `POST /api/knowledge`: criar item KB.
- `PUT /api/knowledge/{id}`: atualizar item KB.
- `DELETE /api/knowledge/{id}`: remover item KB.
- `GET /api/tasks`: listar tasks capturadas.

## Setup UI

```bash
cd ui
npm install
npm run dev:polling
```

Variável opcional da UI:

- `VITE_API_BASE_URL` (default: `http://localhost:8000`)

## Observações

- A UI é opcional e não bloqueia o bot/API.
- Quando DeepSeek ou Notion não estão configurados, o sistema usa fallback local para não travar o fluxo.
- No Python 3.12, `selfcord.py` precisa ser instalado sem deps fixas legadas para evitar erro de build em `aiohttp`.
