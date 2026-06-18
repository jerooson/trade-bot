import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { Bot, CheckCircle2, Loader2, MessageSquare, Send, Trash2, X, XCircle } from "lucide-react";

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

function getSessionId(): string {
  let sid = sessionStorage.getItem(SESSION_KEY);
  if (!sid) {
    sid = crypto.randomUUID();
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
              id: crypto.randomUUID(),
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

      const userMsg: Message = { id: crypto.randomUUID(), role: "user", text };
      const assistantMsgId = crypto.randomUUID();
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
      } catch (err) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId
              ? { ...m, text: `Error: ${String(err)}`, loading: false }
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
          "fixed bottom-6 right-6 z-50 flex h-12 w-12 items-center justify-center rounded-full shadow-lg transition-all duration-200 md:bottom-8 md:right-8",
          open
            ? "bg-crt-info text-ink-950"
            : "bg-ink-800 text-bone-200 hover:bg-ink-700 hover:text-bone-50 border border-ink-500/50"
        )}
        aria-label="Open agent chat"
      >
        {open ? <X className="h-5 w-5" /> : <MessageSquare className="h-5 w-5" />}
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
                  {msg.text || (msg.loading && (
                    <span className="flex items-center gap-1.5 text-bone-500">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      thinking…
                    </span>
                  ))}
                  {msg.loading && msg.text && (
                    <span className="ml-1 inline-block h-2 w-0.5 animate-pulse bg-bone-400 align-middle" />
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
