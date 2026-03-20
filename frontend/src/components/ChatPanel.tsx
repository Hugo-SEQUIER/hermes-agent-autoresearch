"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { chatWithHermes, CompletionMessage } from "@/lib/api";
import { Icon } from "@/components/Icon";

interface ChatPanelProps {
  runId?: string;
  runTitle?: string;
}

interface LocalMessage {
  id: string;
  content: string;
  author: "operator" | "hermes";
  timestamp: string;
}

interface ChatSession {
  id: string;
  title: string;
  messages: LocalMessage[];
  createdAt: string;
  updatedAt: string;
}

const SYSTEM_PROMPT = `You are Hermes, an AI research assistant. You help operators manage automated research runs.
You can discuss research goals, help plan experiments, and provide guidance on the AutoResearch system.
Be concise and helpful. When the operator wants to create a research run, help them define the goal, parameters, and methodology.`;

const STORAGE_KEY = "hermes-chat-sessions";
const ACTIVE_KEY = "hermes-active-session";

function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveSessions(sessions: ChatSession[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
}

function loadActiveId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

function saveActiveId(id: string) {
  localStorage.setItem(ACTIVE_KEY, id);
}

function makeSession(): ChatSession {
  return {
    id: crypto.randomUUID(),
    title: "New Chat",
    messages: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

/** Derive a short title from the first user message */
function deriveTitle(text: string): string {
  const clean = text.replace(/\n/g, " ").trim();
  return clean.length > 40 ? clean.slice(0, 40) + "…" : clean;
}

export default function ChatPanel({ runId, runTitle }: ChatPanelProps) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  /* hydrate from localStorage on mount */
  useEffect(() => {
    const stored = loadSessions();
    const savedActiveId = loadActiveId();

    if (stored.length === 0) {
      const first = makeSession();
      setSessions([first]);
      setActiveId(first.id);
      saveSessions([first]);
      saveActiveId(first.id);
    } else {
      setSessions(stored);
      const valid = stored.find((s) => s.id === savedActiveId);
      const id = valid ? valid.id : stored[0].id;
      setActiveId(id);
      saveActiveId(id);
    }
  }, []);

  const active = sessions.find((s) => s.id === activeId);
  const messages = active?.messages ?? [];

  /* persist sessions whenever they change */
  useEffect(() => {
    if (sessions.length > 0) saveSessions(sessions);
  }, [sessions]);

  /* scroll to bottom */
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingContent]);

  const updateSession = useCallback(
    (id: string, updater: (s: ChatSession) => ChatSession) => {
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? updater(s) : s)),
      );
    },
    [],
  );

  /* build conversation history for the API */
  const buildMessages = useCallback(
    (newUserText: string): CompletionMessage[] => {
      const history: CompletionMessage[] = [
        { role: "system", content: SYSTEM_PROMPT },
      ];

      if (runId) {
        history.push({
          role: "system",
          content: `The operator is currently viewing research run: ${runTitle ?? runId}`,
        });
      }

      for (const msg of messages) {
        history.push({
          role: msg.author === "operator" ? "user" : "assistant",
          content: msg.content,
        });
      }

      history.push({ role: "user", content: newUserText });
      return history;
    },
    [messages, runId, runTitle],
  );

  async function handleSend() {
    const text = input.trim();
    if (!text || loading || !activeId) return;

    const userMsg: LocalMessage = {
      id: crypto.randomUUID(),
      content: text,
      author: "operator",
      timestamp: new Date().toISOString(),
    };

    /* update session with user message + derive title from first message */
    updateSession(activeId, (s) => {
      const isFirst = s.messages.length === 0;
      return {
        ...s,
        messages: [...s.messages, userMsg],
        title: isFirst ? deriveTitle(text) : s.title,
        updatedAt: new Date().toISOString(),
      };
    });

    setInput("");
    setLoading(true);
    setStreamingContent("");

    const abort = new AbortController();
    abortRef.current = abort;

    try {
      const conversationMessages = buildMessages(text);

      const fullResponse = await chatWithHermes(
        conversationMessages,
        (token) => {
          setStreamingContent((prev) => prev + token);
        },
        abort.signal,
      );

      const assistantMsg: LocalMessage = {
        id: crypto.randomUUID(),
        content: fullResponse,
        author: "hermes",
        timestamp: new Date().toISOString(),
      };
      updateSession(activeId, (s) => ({
        ...s,
        messages: [...s.messages, assistantMsg],
        updatedAt: new Date().toISOString(),
      }));
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        const errorMsg: LocalMessage = {
          id: crypto.randomUUID(),
          content: `Error: ${(err as Error).message}`,
          author: "hermes",
          timestamp: new Date().toISOString(),
        };
        updateSession(activeId, (s) => ({
          ...s,
          messages: [...s.messages, errorMsg],
          updatedAt: new Date().toISOString(),
        }));
      }
    } finally {
      setStreamingContent("");
      setLoading(false);
      abortRef.current = null;
    }
  }

  function handleStop() {
    abortRef.current?.abort();
  }

  function handleNewChat() {
    const newSession = makeSession();
    setSessions((prev) => [newSession, ...prev]);
    setActiveId(newSession.id);
    saveActiveId(newSession.id);
    setShowHistory(false);
  }

  function handleSelectSession(id: string) {
    setActiveId(id);
    saveActiveId(id);
    setShowHistory(false);
  }

  function handleDeleteSession(id: string) {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      if (next.length === 0) {
        const fresh = makeSession();
        setActiveId(fresh.id);
        saveActiveId(fresh.id);
        return [fresh];
      }
      if (activeId === id) {
        setActiveId(next[0].id);
        saveActiveId(next[0].id);
      }
      return next;
    });
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function formatTime(iso: string) {
    try {
      return new Date(iso).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return "";
    }
  }

  function formatDate(iso: string) {
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      });
    } catch {
      return "";
    }
  }

  const title = runTitle ?? "Hermes";

  return (
    <div className="flex flex-col h-full w-full relative">
      {/* ── Header ── */}
      <div className="p-6 border-b border-outline-variant/10 flex justify-between items-center">
        <div className="min-w-0">
          <h2 className="font-headline text-lg font-bold text-primary truncate">
            {title}
          </h2>
          <p className="text-xs text-outline uppercase tracking-widest font-label">
            {runId ? "Run Chat" : "Ask anything"}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span
            className={`flex h-2 w-2 rounded-full ${
              loading
                ? "bg-secondary-container animate-pulse shadow-[0_0_8px_#ffbf00]"
                : "bg-primary/40"
            }`}
          />
          <button
            onClick={handleNewChat}
            className="p-1.5 text-outline hover:text-primary transition-colors"
            title="New chat"
          >
            <Icon name="add" className="text-lg" />
          </button>
          <button
            onClick={() => setShowHistory((v) => !v)}
            className={`p-1.5 transition-colors ${
              showHistory ? "text-primary" : "text-outline hover:text-primary"
            }`}
            title="Chat history"
          >
            <Icon name="history" className="text-lg" />
          </button>
        </div>
      </div>

      {/* ── History drawer ── */}
      {showHistory && (
        <div className="absolute inset-0 top-[73px] z-20 bg-surface-container-low/95 backdrop-blur-sm flex flex-col">
          <div className="p-4 border-b border-outline-variant/10 flex items-center justify-between">
            <span className="text-xs font-label uppercase tracking-widest text-outline">
              Chat History
            </span>
            <button
              onClick={() => setShowHistory(false)}
              className="p-1 text-outline hover:text-primary transition-colors"
            >
              <Icon name="close" className="text-lg" />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {sessions.map((s) => (
              <div
                key={s.id}
                className={`flex items-center gap-2 px-3 py-2.5 rounded-lg cursor-pointer transition-all group ${
                  s.id === activeId
                    ? "bg-surface-variant text-primary"
                    : "text-on-surface-variant hover:bg-surface-variant/50"
                }`}
                onClick={() => handleSelectSession(s.id)}
              >
                <Icon name="chat_bubble_outline" className="text-sm shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm truncate">{s.title}</p>
                  <p className="text-[10px] text-outline">
                    {formatDate(s.updatedAt)} · {s.messages.length} messages
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDeleteSession(s.id);
                  }}
                  className="p-1 text-outline hover:text-error opacity-0 group-hover:opacity-100 transition-all"
                  title="Delete"
                >
                  <Icon name="delete_outline" className="text-sm" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Messages ── */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {messages.length === 0 && !streamingContent && (
          <div className="text-center mt-12 space-y-3">
            <Icon
              name="smart_toy"
              className="text-4xl text-outline/30 block mx-auto"
            />
            <p className="text-outline/50 text-sm font-label">
              Talk to Hermes to start a research run,
              <br />
              ask questions, or get help.
            </p>
          </div>
        )}

        {messages.map((msg) => {
          const isUser = msg.author === "operator";
          return (
            <div
              key={msg.id}
              className={`flex ${isUser ? "justify-end" : "justify-start"}`}
            >
              <div className="max-w-[85%] space-y-1">
                <div
                  className={
                    isUser
                      ? "bg-primary-container text-white p-4 rounded-xl rounded-tr-none shadow-md"
                      : "bg-surface-container-high p-4 rounded-xl rounded-tl-none border-l-2 border-primary shadow-sm"
                  }
                >
                  <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                </div>
                <p
                  className={`text-[10px] text-outline font-label uppercase ${
                    isUser ? "text-right" : "text-left"
                  }`}
                >
                  {isUser ? "" : "Hermes · "}
                  {formatTime(msg.timestamp)}
                </p>
              </div>
            </div>
          );
        })}

        {/* Streaming response */}
        {streamingContent && (
          <div className="flex justify-start">
            <div className="max-w-[85%] space-y-1">
              <div className="bg-surface-container-high p-4 rounded-xl rounded-tl-none border-l-2 border-primary shadow-sm">
                <p className="text-sm whitespace-pre-wrap">
                  {streamingContent}
                  <span className="inline-block w-1.5 h-4 bg-primary/60 animate-pulse ml-0.5 align-text-bottom" />
                </p>
              </div>
              <p className="text-[10px] text-outline font-label uppercase text-left">
                Hermes · typing…
              </p>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Input ── */}
      <div className="p-6 bg-surface-container-low border-t border-outline-variant/10">
        <div className="relative">
          <textarea
            ref={textareaRef}
            rows={3}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Message Hermes…"
            disabled={loading}
            className="w-full bg-surface-container-lowest border-none rounded-lg p-4 pr-14 text-sm text-on-surface focus:ring-1 focus:ring-primary placeholder:text-outline/50 resize-none disabled:opacity-50"
          />
          {loading ? (
            <button
              onClick={handleStop}
              className="absolute bottom-4 right-4 p-2 bg-error text-on-error rounded-md hover:scale-105 active:scale-95 transition-all"
              title="Stop generating"
            >
              <Icon name="stop" className="text-base" />
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className="absolute bottom-4 right-4 p-2 bg-primary text-on-primary rounded-md hover:scale-105 active:scale-95 transition-all disabled:opacity-40 disabled:hover:scale-100"
            >
              <Icon name="send" className="text-base" />
            </button>
          )}
        </div>

        <div className="flex justify-between mt-3">
          <span className="text-[10px] text-outline font-label uppercase tracking-widest">
            Shift+Enter for new line
          </span>
          <span className="text-[10px] text-outline font-label uppercase tracking-widest">
            Powered by Hermes
          </span>
        </div>
      </div>
    </div>
  );
}
