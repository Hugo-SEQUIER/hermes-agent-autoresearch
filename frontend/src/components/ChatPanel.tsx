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

const SYSTEM_PROMPT = `You are Hermes, an AI research assistant. You help operators manage automated research runs.
You can discuss research goals, help plan experiments, and provide guidance on the AutoResearch system.
Be concise and helpful. When the operator wants to create a research run, help them define the goal, parameters, and methodology.`;

export default function ChatPanel({ runId, runTitle }: ChatPanelProps) {
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  /* scroll to bottom on new messages or streaming update */
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingContent]);

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
    if (!text || loading) return;

    const userMsg: LocalMessage = {
      id: crypto.randomUUID(),
      content: text,
      author: "operator",
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
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
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        const errorMsg: LocalMessage = {
          id: crypto.randomUUID(),
          content: `Error: ${(err as Error).message}`,
          author: "hermes",
          timestamp: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, errorMsg]);
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

  const title = runTitle ?? "Hermes";

  return (
    <div className="flex flex-col h-full w-full">
      {/* ── Header ── */}
      <div className="p-6 border-b border-outline-variant/10 flex justify-between items-center">
        <div>
          <h2 className="font-headline text-lg font-bold text-primary">
            {title}
          </h2>
          <p className="text-xs text-outline uppercase tracking-widest font-label">
            {runId ? "Run Chat" : "Ask anything"}
          </p>
        </div>
        <span
          className={`flex h-2 w-2 rounded-full ${
            loading
              ? "bg-secondary-container animate-pulse shadow-[0_0_8px_#ffbf00]"
              : "bg-primary/40"
          }`}
        />
      </div>

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
