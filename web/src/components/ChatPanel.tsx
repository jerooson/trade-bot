import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { Bot, CheckCircle2, ChevronDown, ChevronRight, Loader2, MessageSquare, Send, Trash2, X, XCircle } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ProposedOrder {
  ticker: string;
  action: "BUY" | "SELL";
  dollar_amount?: number;
  shares?: number;
  rationale?: string;
}

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  loading?: boolean;
  proposedOrder?: ProposedOrder;
  orderPlaced?: string; // broker order ID
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const SESSION_KEY = "trade-bot-chat-session";

function uuid(): string {
  // crypto.randomUUID requires a secure context (HTTPS); use a fallback for HTTP
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function getSessionId(): string {
  let sid = sessionStorage.getItem(SESSION_KEY);
  if (!sid) {
    sid = uuid();
    sessionStorage.setItem(SESSION_KEY, sid);
  }
  return sid;
}

// ---------------------------------------------------------------------------
// ChatPanel
// ---------------------------------------------------------------------------

export function ChatPanel() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const sessionId = useRef(getSessionId());

  // Load history on mount
  useEffect(() => {
    fetch(`/api/chat/history/${sessionId.current}`)
      .then((r) => r.json())
      .then((d: { messages: { role: string; content: string }[] }) => {
        if (d.messages.length > 0) {
          setMessages(
            d.messages.map((m) => ({
        id: uuid(),
        role: m.role as "user" | "assistant",
              text: m.content,
            }))
          );
        }
      })
      .catch(() => {});
  }, []);

  // Scroll to bottom whenever messages update
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Focus input when panel opens
  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 80);
  }, [open]);

  const clearHistory = useCallback(async () => {
    await fetch(`/api/chat/history/${sessionId.current}`, { method: "DELETE" });
    setMessages([]);
  }, []);

  const sendMessage = useCallback(
    async (text: string, confirmed = false) => {
      if (!text.trim() || loading) return;

      const userMsg: Message = { id: uuid(), role: "user", text };
      const assistantMsgId = uuid();
      const assistantMsg: Message = {
        id: assistantMsgId,
        role: "assistant",
        text: "",
        loading: true,
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setInput("");
      setLoading(true);

      try {
        const resp = await fetch("/api/chat/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId.current,
            message: text,
            confirmed,
          }),
        });

        if (!resp.ok || !resp.body) {
          throw new Error(`HTTP ${resp.status}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let proposedOrder: ProposedOrder | undefined;
        let orderPlacedId: string | undefined;
        // 90-second abort controller so the spinner never hangs indefinitely
        const abortTimer = setTimeout(() => reader.cancel(), 90_000);

        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() ?? "";

            for (const line of lines) {
              if (line.startsWith("data: ")) {
                try {
                  const payload = JSON.parse(line.slice(6));

                  if (payload.type === "chunk") {
                    setMessages((prev) =>
                      prev.map((m) =>
                        m.id === assistantMsgId
                          ? { ...m, text: m.text + payload.text, loading: true }
                          : m
                      )
                    );
                  } else if (payload.type === "proposed_order") {
                    proposedOrder = payload.order;
                  } else if (payload.type === "order_placed") {
                    orderPlacedId = payload.order_id;
                  } else if (payload.type === "done") {
                    setMessages((prev) =>
                      prev.map((m) =>
                        m.id === assistantMsgId
                          ? {
                              ...m,
                              loading: false,
                              proposedOrder,
                              orderPlaced: orderPlacedId,
                            }
                          : m
                      )
                    );
                  }
                } catch {
                  // malformed SSE line — skip
                }
              }
            }
          }
        } finally {
          clearTimeout(abortTimer);
          // Always mark the message as done — handles the case where the stream
          // closes without emitting a "done" SSE event (network drop, timeout).
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId ? { ...m, loading: false, proposedOrder, orderPlaced: orderPlacedId } : m
            )
          );
        }
      } catch (err) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId
              ? { ...m, text: m.text || `[Error: ${String(err)}]`, loading: false }
              : m
          )
        );
      } finally {
        setLoading(false);
      }
    },
    [loading]
  );

  const handleConfirm = useCallback(
    (order: ProposedOrder) => {
      const confirmText = `Confirmed. Execute: ${order.action} ${order.ticker}${order.dollar_amount ? ` $${order.dollar_amount}` : ""}`;
      sendMessage(confirmText, true);
    },
    [sendMessage]
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    sendMessage(input);
  };

  return (
    <>
      {/* Floating trigger button */}
      <button
        onClick={() => setOpen((o) => !o)}
        className={clsx(
          "fixed bottom-6 right-6 z-50 flex items-center gap-2 rounded-full px-4 py-3 shadow-lg transition-all duration-200 md:bottom-8 md:right-8",
          open
            ? "bg-crt-info text-ink-950"
            : "border border-crt-info/40 bg-ink-900 text-crt-info hover:bg-crt-info/10 shadow-[0_0_16px_rgba(0,200,255,0.15)]"
        )}
        aria-label="Open agent chat"
      >
        {open ? <X className="h-4 w-4" /> : <MessageSquare className="h-4 w-4" />}
        {!open && <span className="text-[11px] font-semibold uppercase tracking-[0.18em]">Agent</span>}
      </button>

      {/* Drawer overlay */}
      <div
        className={clsx(
          "fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l border-ink-500/30 bg-ink-950/95 shadow-2xl backdrop-blur-xl transition-transform duration-300 sm:w-[420px]",
          open ? "translate-x-0" : "translate-x-full"
        )}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-ink-500/30 px-4 py-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-crt-info/15">
              <Bot className="h-4 w-4 text-crt-info" />
            </div>
            <div>
              <div className="text-sm font-semibold text-bone-50">Agent Chat</div>
              <div className="text-[10px] text-bone-500">buy · sell · debug · ask</div>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={clearHistory}
              className="flex h-7 w-7 items-center justify-center rounded text-bone-500 hover:bg-ink-800 hover:text-bone-200 transition-colors"
              title="Clear history"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setOpen(false)}
              className="flex h-7 w-7 items-center justify-center rounded text-bone-500 hover:bg-ink-800 hover:text-bone-200 transition-colors"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-ink-800">
                <Bot className="h-6 w-6 text-bone-400" />
              </div>
              <div className="text-sm font-medium text-bone-200">Ask the agent anything</div>
              <div className="text-[11px] text-bone-500 leading-relaxed max-w-[260px]">
                "What's my AXTI position?"<br />
                "Sell half of INTC"<br />
                "Why didn't my stop trigger?"<br />
                "Check buying power"
              </div>
            </div>
          )}

          {messages.map((msg) => (
            <div
              key={msg.id}
              className={clsx("flex gap-2.5", msg.role === "user" ? "justify-end" : "justify-start")}
            >
              {msg.role === "assistant" && (
                <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-crt-info/15">
                  <Bot className="h-3.5 w-3.5 text-crt-info" />
                </div>
              )}
              <div className={clsx("flex flex-col gap-2 max-w-[85%]", msg.role === "user" && "items-end")}>
                <div
                  className={clsx(
                    "rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed",
                    msg.role === "user"
                      ? "bg-crt-info/15 text-bone-100 rounded-tr-sm"
                      : "bg-ink-800/80 text-bone-200 rounded-tl-sm"
                  )}
                >
                  {msg.role === "assistant" ? (
                    <AgentText text={msg.text} loading={!!msg.loading} />
                  ) : (
                    msg.text
                  )}
                </div>

                {/* Proposed order card */}
                {!msg.loading && msg.proposedOrder && !msg.orderPlaced && (
                  <OrderConfirmCard
                    order={msg.proposedOrder}
                    onConfirm={() => handleConfirm(msg.proposedOrder!)}
                    disabled={loading}
                  />
                )}

                {/* Order placed confirmation */}
                {msg.orderPlaced && (
                  <div className="flex items-center gap-1.5 rounded-xl border border-crt-long/30 bg-crt-long/10 px-3 py-2 text-[11px] text-crt-long">
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                    Order placed · {msg.orderPlaced.slice(0, 8)}…
                  </div>
                )}
              </div>
            </div>
          ))}

          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <form
          onSubmit={handleSubmit}
          className="border-t border-ink-500/30 px-3 py-3"
        >
          <div className="flex items-center gap-2 rounded-xl border border-ink-500/40 bg-ink-900/60 px-3 py-2 focus-within:border-crt-info/50">
            <input
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask the agent…"
              disabled={loading}
              className="flex-1 bg-transparent text-sm text-bone-100 placeholder-bone-600 outline-none disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={!input.trim() || loading}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-crt-info/80 text-ink-950 transition-opacity disabled:opacity-30 hover:bg-crt-info"
            >
              {loading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Send className="h-3.5 w-3.5" />
              )}
            </button>
          </div>
          <div className="mt-1.5 px-1 text-[10px] text-bone-600">
            Orders require confirmation · Uses Robinhood MCP
          </div>
        </form>
      </div>

      {/* Backdrop (mobile) */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-ink-950/60 backdrop-blur-sm sm:hidden"
          onClick={() => setOpen(false)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Codex output parser
// ---------------------------------------------------------------------------

interface ToolCall {
  name: string;   // e.g. "robinhood-trading/get_accounts"
  done: boolean;
}

interface ThinkingBlock {
  narration: string;   // agent's reasoning text in this block
  tools: ToolCall[];
}

interface ParsedCodex {
  thinking: ThinkingBlock[];
  answer: string;
  stillThinking: boolean; // true while streaming before we see the final answer
}

const NOISE_LINE_RE =
  /^(WARNING:|workdir:|model:|provider:|approval:|sandbox:|reasoning|session id:|tokens used|--------|\d{4}-\d{2}-\d{2}T.*|Reading additional input|warning: Codex|OpenAI Codex v)/;

function parseCodexOutput(raw: string, loading: boolean): ParsedCodex {
  const lines = raw.split("\n");
  const thinking: ThinkingBlock[] = [];
  let answer = "";

  type Phase = "before_prompt" | "in_prompt" | "in_response" | "after_tokens";
  let phase: Phase = "before_prompt";
  let cur: ThinkingBlock | null = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (NOISE_LINE_RE.test(trimmed)) continue;

    // Skip everything after the "tokens used" marker (duplicate final answer)
    if (phase === "after_tokens") continue;

    // Skip standalone numeric token-count lines
    if (/^\d{4,}$/.test(trimmed)) continue;

    // "user" alone marks start of the prompt echo — skip until next "codex"
    if (trimmed === "user" && phase === "before_prompt") {
      phase = "in_prompt";
      continue;
    }

    // "codex" alone marks start of an agent response section
    if (trimmed === "codex") {
      if (phase === "in_prompt" || phase === "before_prompt") {
        phase = "in_response";
      }
      if (phase === "in_response" && cur !== null) {
        thinking.push(cur);
      }
      if (phase === "in_response") {
        cur = { narration: "", tools: [] };
      }
      continue;
    }

    // MCP tool call lines can appear before the first "codex" marker when the
    // model skips narration and goes straight to tool calls.
    if (trimmed.startsWith("mcp: ") && phase !== "in_response") {
      phase = "in_response";
      cur = { narration: "", tools: [] };
    }

    if (phase !== "in_response") continue;

    // "tokens used" marks the end of real responses; current block = answer
    if (trimmed === "tokens used") {
      if (cur) {
        answer = cur.narration.trim();
        cur = null;
      }
      phase = "after_tokens";
      continue;
    }

    // MCP tool call lines
    if (trimmed.startsWith("mcp: ")) {
      const info = trimmed.slice(5);
      const done = info.includes("(completed)") || info.includes("(failed)");
      const name = info.replace(/ started| \(completed\)| \(failed\)/, "").trim();
      if (cur) {
        const existing = cur.tools.find((t) => t.name === name);
        if (existing) {
          existing.done = done || existing.done;
        } else {
          cur.tools.push({ name, done });
        }
      }
      continue;
    }

    // Regular narration line
    if (cur) {
      cur.narration += (cur.narration ? "\n" : "") + line;
    }
  }

  // If we never hit "tokens used" (still streaming), cur is the in-progress block
  if (!answer && cur) {
    // While streaming the final answer block, treat it as the live answer
    answer = cur.narration.trim();
    // Keep it as a thinking step too so the section count is right
    if (cur.tools.length || cur.narration.trim()) {
      // Don't add to thinking — it becomes the answer once done
    }
  }

  return { thinking, answer, stillThinking: loading && !answer };
}

// ---------------------------------------------------------------------------
// Agent text renderer
// ---------------------------------------------------------------------------

function MarkdownAnswer({ text, loading = false }: { text: string; loading?: boolean }) {
  return (
    <div className="text-sm text-bone-100 leading-relaxed">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
          ul: ({ children }) => (
            <ul className="mb-2 ml-5 list-disc space-y-1 last:mb-0">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-2 ml-5 list-decimal space-y-1 last:mb-0">{children}</ol>
          ),
          table: ({ children }) => (
            <div className="my-3 overflow-x-auto rounded-lg border border-ink-500/50 bg-ink-900/60 shadow-inner">
              <table className="w-full min-w-[360px] border-collapse text-xs">
                {children}
              </table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-ink-950/90">{children}</thead>,
          tbody: ({ children }) => <tbody>{children}</tbody>,
          tr: ({ children }) => (
            <tr className="border-t border-ink-600/40 first:border-t-0 even:bg-white/[0.025] hover:bg-white/[0.04]">
              {children}
            </tr>
          ),
          th: ({ children }) => (
            <th className="px-3 py-2 text-left font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-bone-500 [&:not(:first-child)]:text-right">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="whitespace-nowrap px-3 py-2 text-left font-mono tabular-nums text-bone-300 first:font-semibold first:text-bone-100 [&:not(:first-child)]:text-right">
              {children}
            </td>
          ),
          code: ({ children }) => (
            <code className="rounded bg-ink-700/70 px-1 py-0.5 font-mono text-[0.9em] text-crt-long">
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre className="my-2 overflow-x-auto rounded-lg border border-ink-600/50 bg-ink-950 p-3 text-xs">
              {children}
            </pre>
          ),
          strong: ({ children }) => (
            <strong className="font-semibold text-bone-50">{children}</strong>
          ),
        }}
      >
        {text}
      </Markdown>
      {loading && (
        <span className="ml-0.5 inline-block h-3.5 w-0.5 animate-pulse bg-bone-400 align-middle" />
      )}
    </div>
  );
}

function AgentText({ text, loading }: { text: string; loading: boolean }) {
  // Auto-collapse thinking when the answer arrives
  const [open, setOpen] = useState(true);
  const prevLoading = useRef(loading);
  useEffect(() => {
    if (prevLoading.current && !loading) {
      // response just finished → collapse thinking
      setOpen(false);
    }
    prevLoading.current = loading;
  }, [loading]);

  if (!text && loading) {
    return (
      <span className="flex items-center gap-1.5 text-bone-500 text-sm">
        <Loader2 className="h-3 w-3 animate-spin shrink-0" />
        thinking…
      </span>
    );
  }

  const { thinking, answer } = parseCodexOutput(text, loading);
  const toolCount = thinking.reduce((n, b) => n + b.tools.length, 0);
  const hasThinking = thinking.length > 0 || (loading && !answer);

  return (
    <div className="flex flex-col gap-2">
      {/* ── Thinking section ───────────────────────────────────── */}
      {hasThinking && (
        <div className="rounded-lg border border-ink-600/40 bg-ink-900/50 overflow-hidden">
          <button
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] text-bone-500 hover:text-bone-300 transition-colors"
          >
            {open ? (
              <ChevronDown className="h-3 w-3 shrink-0" />
            ) : (
              <ChevronRight className="h-3 w-3 shrink-0" />
            )}
            {loading && !answer ? (
              <span className="flex items-center gap-1">
                <Loader2 className="h-2.5 w-2.5 animate-spin" />
                thinking…
              </span>
            ) : (
              <span>
                {thinking.length} step{thinking.length !== 1 ? "s" : ""}
                {toolCount > 0 && (
                  <span className="ml-1 text-bone-600">· {toolCount} tool call{toolCount !== 1 ? "s" : ""}</span>
                )}
              </span>
            )}
          </button>

          {open && (
            <div className="px-2.5 pb-2 flex flex-col gap-1.5 border-t border-ink-600/30">
              {thinking.map((block, bi) => (
                <div key={bi} className="pt-1.5">
                  {/* Tool calls as compact badges */}
                  {block.tools.map((tool, ti) => (
                    <div
                      key={ti}
                      className="flex items-center gap-1.5 text-[10px] font-mono py-0.5"
                    >
                      {tool.done ? (
                        <span className="text-crt-long/70">✓</span>
                      ) : (
                        <Loader2 className="h-2.5 w-2.5 animate-spin text-bone-500" />
                      )}
                      <span className={tool.done ? "text-bone-500" : "text-bone-400"}>
                        {tool.name.replace("robinhood-trading/", "")}
                      </span>
                    </div>
                  ))}
                  {/* Narration text — small, muted */}
                  {block.narration.trim() && (
                    <p className="text-[11px] text-bone-600 leading-relaxed mt-0.5 whitespace-pre-wrap">
                      {block.narration.trim()}
                    </p>
                  )}
                </div>
              ))}
              {/* Live block being streamed */}
              {loading && !answer && (
                <div className="pt-1 text-[11px] text-bone-600 italic">
                  <Loader2 className="inline h-2.5 w-2.5 animate-spin mr-1" />
                  working…
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Final answer ────────────────────────────────────────── */}
      {answer ? (
        <MarkdownAnswer text={answer} loading={loading} />
      ) : !hasThinking && text ? (
        /* Fallback: couldn't parse, show raw filtered text */
        <MarkdownAnswer
          text={text
            .split("\n")
            .filter((l) => !NOISE_LINE_RE.test(l.trim()))
            .join("\n")}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Order confirm card
// ---------------------------------------------------------------------------

function OrderConfirmCard({
  order,
  onConfirm,
  disabled,
}: {
  order: ProposedOrder;
  onConfirm: () => void;
  disabled: boolean;
}) {
  const [cancelled, setCancelled] = useState(false);

  if (cancelled) {
    return (
      <div className="flex items-center gap-1.5 rounded-xl border border-ink-500/30 px-3 py-2 text-[11px] text-bone-500">
        <XCircle className="h-3.5 w-3.5" />
        Order cancelled
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-crt-info/30 bg-crt-info/5 p-3 text-[12px]">
      <div className="mb-2 flex items-center gap-2">
        <span
          className={clsx(
            "rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide",
            order.action === "BUY"
              ? "bg-crt-long/20 text-crt-long"
              : "bg-crt-short/20 text-crt-short"
          )}
        >
          {order.action}
        </span>
        <span className="font-semibold text-bone-100">{order.ticker}</span>
        {order.dollar_amount && (
          <span className="text-bone-400">${order.dollar_amount.toFixed(2)}</span>
        )}
      </div>
      {order.rationale && (
        <div className="mb-2.5 text-bone-500 leading-relaxed">{order.rationale}</div>
      )}
      <div className="flex gap-2">
        <button
          onClick={onConfirm}
          disabled={disabled}
          className="flex-1 rounded-lg bg-crt-info/80 py-1.5 text-center text-[11px] font-semibold text-ink-950 transition-opacity hover:bg-crt-info disabled:opacity-40"
        >
          Confirm
        </button>
        <button
          onClick={() => setCancelled(true)}
          disabled={disabled}
          className="flex-1 rounded-lg border border-ink-500/40 py-1.5 text-center text-[11px] text-bone-400 hover:border-ink-400/60 hover:text-bone-200 disabled:opacity-40"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
