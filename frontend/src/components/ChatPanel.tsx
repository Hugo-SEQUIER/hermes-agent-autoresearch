"use client";

import { useState, useRef, useEffect } from "react";
import { sendRunChat, sendGlobalChat, listGlobalChat, ChatMessage } from "@/lib/api";
import { Icon } from "@/components/Icon";

interface ChatPanelProps {
  runId?: string;
  runTitle?: string;
}

interface LocalMessage {
  id: string;
  content: string;
  author: string;
  timestamp: string;
}

export default function ChatPanel({ runId, runTitle }: ChatPanelProps) {
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [input, setInput] = useState("");
  const [scope, setScope] = useState<"run" | "global">(runId ? "run" : "global");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  /* scroll to bottom on new messages */
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  /* load global chat history when scope switches to global */
  useEffect(() => {
    if (scope === "global") {
      listGlobalChat()
        .then((res) => {
          setMessages(
            (res.messages ?? []).map((m: ChatMessage) => ({
              id: m.id,
              content: m.content,
              author: (m.metadata?.author as string) ?? "agent",
              timestamp: m.timestamp,
            }))
          );
        })
        .catch(() => {});
    } else {
      setMessages([]);
    }
  }, [scope]);

  async function handleSend() {
    const text = input.trim();
    if (!text) return;

    const optimistic: LocalMessage = {
      id: crypto.randomUUID(),
      content: text,
      author: "operator",
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    setInput("");

    try {
      if (scope === "run" && runId) {
        await sendRunChat(runId, text);
      } else {
        await sendGlobalChat(text);
      }
    } catch {
      /* keep optimistic message visible even on failure */
    }
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

  const title = scope === "run" && runTitle ? runTitle : "Hermes Workspace";

  return (
    <div className="flex flex-col h-full">
      {/* ── Header ── */}
      <div className="p-6 border-b border-outline-variant/10 flex justify-between items-center">
        <div>
          <h2 className="font-headline text-lg font-bold text-primary">
            {title}
          </h2>
          <p className="text-xs text-outline uppercase tracking-widest font-label">
            {scope === "run" ? "Run Chat" : "Global Chat"}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <span className="flex h-2 w-2 rounded-full bg-secondary-container shadow-[0_0_8px_#ffbf00]" />

          {runId && (
            <button
              onClick={() => setScope((s) => (s === "run" ? "global" : "run"))}
              className="flex items-center gap-1 text-xs text-outline font-label uppercase tracking-widest hover:text-primary transition-colors"
            >
              <Icon name={scope === "run" ? "language" : "target"} className="text-sm" />
              {scope === "run" ? "Global" : "Run"}
            </button>
          )}
        </div>
      </div>

      {/* ── Messages ── */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {messages.length === 0 && (
          <p className="text-center text-outline/50 text-sm font-label mt-12">
            No messages yet. Start the conversation.
          </p>
        )}

        {messages.map((msg) => {
          const isUser = msg.author === "operator";
          return (
            <div
              key={msg.id}
              className={`flex ${isUser ? "justify-end" : "justify-start"}`}
            >
              <div className={`max-w-[80%] space-y-1`}>
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
                  {formatTime(msg.timestamp)}
                </p>
              </div>
            </div>
          );
        })}

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
            className="w-full bg-surface-container-lowest border-none rounded-lg p-4 pr-14 text-sm text-on-surface focus:ring-1 focus:ring-primary placeholder:text-outline/50 resize-none"
          />
          <button
            onClick={handleSend}
            disabled={!input.trim()}
            className="absolute bottom-4 right-4 p-2 bg-primary text-on-primary rounded-md hover:scale-105 active:scale-95 transition-all disabled:opacity-40 disabled:hover:scale-100"
          >
            <Icon name="send" className="text-base" />
          </button>
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
