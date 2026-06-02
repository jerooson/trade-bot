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
