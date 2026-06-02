"""Verbatim fixtures from the swing-trade channel."""

ENTRY = """🚨 正股交易
股票: LITE
操作: 🟢 买入开仓 (做多)
价格: $873.00
仓位: 1/2
止损: 无
止损类型: 立即


Posted by: Will

- 除非特殊说明, 止损空间是我买入价的2-5%. 非投资建议
- 在没有阅读和完全理解新手手册之前不建议做任何操作
@everyone"""

ADD = """🚨 正股加仓
股票: NVDA
操作: 🔵 买入加仓 (做多)
价格: $210.80 → 均价: $214.90
仓位: +1/4 → 3/4
止损: 无
止损类型: 立即


Posted by: Will
@everyone"""

REDUCE = """🚨 正股减仓
股票: INFQ
操作: 🟠 卖出减仓 1/8
盈亏: +15.00%


Posted by: Will
@everyone"""

CLOSE_SIMPLIFIED = """🚨 正股平仓
股票: MU
操作: 🔴 卖出平仓
盈亏: +50.00%


Posted by: Will
@everyone"""

CLOSE_TRADITIONAL = """🚨 正股平倉
Ticker: TQQQ
Action: 🔴 Sell to close
Profit: -16.00%


Posted by: Will
@everyone"""

STOP_TRIGGER = """🛑 止损提醒
Ticker: NVDA
Trade Type: LONG
Avg Cost: $218.84
Stop Loss: $217.5
Current Price: $217.49

STOP LOSS TRIGGERED!

Posted by: Will
@everyone"""

STOP_UPDATE = """🛡️ 正股止损更新
股票: INFQ
操作: 🔄 更新止损
新止损: 保本 (均价)


Posted by: Will
@everyone"""

POSITION_UPDATE = """📈 持仓股票提醒
Ticker: DOCN
Trade Type: LONG
Position Size: 1/2
Avg Cost: $149.68
Current Price: $176.00
Profit: +17.6%

Posted by: Will
@everyone"""

SIZE_UPDATE_RARE = """🚨 正股仓位更新
股票: CRWD
操作: 🔄 仓位更新
仓位: 7/8 → 3/4
止损: 无


Posted by: Will
@everyone"""

# Noise: chat reply, no header, should be rejected.
NOISE_NO_HEADER = "yes I agree, watch carefully @everyone"
NOISE_EMPTY = "   \n  \n"
