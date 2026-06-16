export type SignalKind = "PLAN" | "TRIGGER" | "PROFIT";
export type Side = "LONG" | "SHORT";

export interface Signal {
  kind: SignalKind;
  ticker: string;
  side: Side | null;
  trigger: number | null;
  target: number | null;
  current_price: number | null;
  profit_pct: number | null;
  setup: string | null;
  chart_url: string | null;
  raw_text: string;
  received_at: string;
  discord?: {
    message_id: number;
    channel_id: number;
    channel_name: string | null;
    guild_id: number | null;
    author_id: number;
    author_name: string;
    created_at: string;
  };
}

export interface TradePlan {
  ticker: string | null;
  watch_levels: number[];
  narrative: string;
  chart_url: string | null;
  glossary: Record<string, string>;
  raw_text: string;
  received_at: string;
  discord?: {
    message_id: number;
    channel_id: number;
    channel_name: string | null;
    guild_id: number | null;
    author_id: number;
    author_name: string;
    created_at: string;
  };
}

export type ActionKind =
  | "ENTRY"
  | "ADD"
  | "REDUCE"
  | "CLOSE"
  | "STOP_TRIGGER"
  | "STOP_UPDATE"
  | "POSITION_UPDATE";

export interface TradeAction {
  kind: ActionKind;
  ticker: string;
  side: Side | null;
  price: number | null;
  avg_cost: number | null;
  stop_loss: number | null;
  stop_loss_label: string | null;
  profit_pct: number | null;
  position_size: string | null;
  position_fraction: number | null;
  delta_size: string | null;
  action_text: string | null;
  stop_type: string | null;
  posted_by: string | null;
  raw_text: string;
  received_at: string;
  discord?: {
    message_id: number;
    channel_id: number;
    channel_name: string | null;
    guild_id: number | null;
    author_id: number;
    author_name: string;
    created_at: string;
  };
}

export interface OpenPosition {
  ticker: string;
  side: Side | null;
  avg_cost: number | null;
  position_size: string | null;
  position_fraction: number | null;
  stop_loss: number | null;
  stop_loss_label: string | null;
  opened_at: string | null;
  last_action_at: string | null;
  last_action_kind: ActionKind | null;
  last_price: number | null;
  last_pnl_pct: number | null;
}

// -- Executor types -----------------------------------------------------------

export interface VirtualPosition {
  ticker: string;
  side: "LONG";
  shares: number;
  deployed_usd: number;
  budget_usd: number;
  avg_price: number | null;
  stop_loss: number | null;
  stop_loss_label: string | null;
  last_signal_fraction: number | null;
  last_signal_size: string | null;
  first_entry_at: string;
  last_action_at: string;
  actions_count: number;
}

export interface VirtualBook {
  present: boolean;
  reason?: string;
  mode?: string;
  budget_per_ticker?: number;
  max_open_tickers?: number;
  started_at?: string;
  last_processed_at?: string | null;
  last_decision_at?: string | null;
  decisions_total?: number;
  positions?: Record<string, VirtualPosition>;
  summary?: {
    open_tickers: number;
    max_tickers: number;
    total_deployed_usd: number;
    account_budget_usd: number;
    available_usd: number;
  };
}

export type DecisionAction = "BUY" | "SELL" | "REJECT";

export interface ProposedOrder {
  id: string;
  decided_at: string;
  mode: string;
  signal_kind: string;
  ticker: string;
  action: DecisionAction;
  usd_amount: number | null;
  shares_estimate: number | null;
  signal_price: number | null;
  rationale: string;
  book_before: Record<string, unknown>;
  book_after: Record<string, unknown>;
  signal: {
    kind?: string;
    ticker?: string;
    side?: string | null;
    price?: number | null;
    stop_loss?: number | null;
    stop_loss_label?: string | null;
    position_size?: string | null;
    position_fraction?: number | null;
    delta_size?: string | null;
    action_text?: string | null;
    received_at?: string;
    posted_by?: string | null;
    message_id?: number;
    channel_id?: number;
    created_at?: string;
  };
}

// -- P&L types ----------------------------------------------------------------

export type TradeKind = "ENTRY" | "ADD" | "REDUCE" | "CLOSE";

export interface PnlRecord {
  timestamp: string;
  ticker: string;
  kind: TradeKind;
  action: "BUY" | "SELL";
  order_id: string;
  signal_price: number | null;
  fill_price: number | null;
  fill_qty: number | null;
  fill_usd: number | null;
  avg_cost_before: number | null;
  avg_cost_after: number | null;
  position_qty_after: number | null;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
}

export interface PnlSummary {
  count: number;
  total_realized_pnl: number;
  wins: number;
  losses: number;
  records: PnlRecord[];
}

export interface Stats {
  total: number;
  by_kind: Record<string, number>;
  by_side: Record<string, number>;
  earliest: string | null;
  latest: string | null;
  today_count: number;
  today_date_utc: string;
  has_target: number;
  no_target: number;
  by_hour_utc: { hour: number; count: number }[];
  by_day: { date: string; count: number }[];
  top_tickers: {
    ticker: string;
    total: number;
    trigger: number;
    plan: number;
    profit: number;
  }[];
  trigger_prices: { ticker: string; prices: number[] }[];
}
