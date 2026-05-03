PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL UNIQUE,
    display_name TEXT,
    mode TEXT NOT NULL DEFAULT 'approval' CHECK (mode IN ('auto', 'approval', 'always_me')),
    trust_score REAL NOT NULL DEFAULT 0.5 CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    confidence_threshold REAL NOT NULL DEFAULT 0.7 CHECK (confidence_threshold >= 0.0 AND confidence_threshold <= 1.0),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_base (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    keywords TEXT NOT NULL,
    answer TEXT NOT NULL,
    confidence_threshold REAL NOT NULL DEFAULT 0.7 CHECK (confidence_threshold >= 0.0 AND confidence_threshold <= 1.0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'self_replied')),
    intent TEXT NOT NULL DEFAULT 'unknown' CHECK (intent IN ('routine_question', 'error_report', 'task_request', 'greeting', 'unknown')),
    original_msg TEXT NOT NULL,
    suggested_reply TEXT,
    final_reply TEXT,
    confidence_score REAL CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    meta_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sender_id) REFERENCES senders (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notion_id TEXT,
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'synced', 'failed', 'closed')),
    sender TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversation_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    intent TEXT NOT NULL DEFAULT 'unknown' CHECK (intent IN ('routine_question', 'error_report', 'task_request', 'greeting', 'unknown')),
    message TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sender_id) REFERENCES senders (id) ON DELETE CASCADE,
    UNIQUE (sender_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS conversation_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'watching' CHECK (status IN ('watching', 'auto_replied', 'needs_human', 'resolved', 'ignored')),
    last_incoming_message TEXT NOT NULL,
    last_incoming_message_id TEXT,
    last_incoming_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_jeff_reply_message TEXT,
    last_jeff_reply_message_id TEXT,
    last_jeff_reply_at TEXT,
    auto_reply_sent_at TEXT,
    needs_human_reason TEXT,
    meta_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sender_id) REFERENCES senders (id) ON DELETE CASCADE,
    UNIQUE (sender_id, channel_id)
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    source_message_count INTEGER NOT NULL DEFAULT 0,
    last_summarized_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sender_id) REFERENCES senders (id) ON DELETE CASCADE,
    UNIQUE (sender_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_senders_discord_id ON senders (discord_id);
CREATE INDEX IF NOT EXISTS idx_senders_mode ON senders (mode);
CREATE INDEX IF NOT EXISTS idx_kb_category ON knowledge_base (category);
CREATE INDEX IF NOT EXISTS idx_kb_active ON knowledge_base (is_active);
CREATE INDEX IF NOT EXISTS idx_queue_sender_status ON message_queue (sender_id, status);
CREATE INDEX IF NOT EXISTS idx_queue_created_at ON message_queue (created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_context_sender_created ON conversation_context (sender_id, created_at);
CREATE INDEX IF NOT EXISTS idx_watch_status_incoming ON conversation_watch (status, last_incoming_at);
CREATE INDEX IF NOT EXISTS idx_watch_channel ON conversation_watch (channel_id);
CREATE INDEX IF NOT EXISTS idx_summaries_sender_channel ON conversation_summaries (sender_id, channel_id);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    item_json  TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_session ON agent_sessions (session_id, id);
