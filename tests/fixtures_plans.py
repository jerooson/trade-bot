"""Verbatim plan-channel messages, used as test fixtures."""


# -- AKAM: ticker on first line + range "150-151" + 8EMA mention --------------

PLAN_AKAM = """AKAM 我们之前交易过，最近几天在8EMA附近盘整了很久，150-151附近区间随时可能突破，https://www.tradingview.com/x/jWs91aoz/

技术名词解释: 📋
盘整: 价格在窄幅区间内波动，多空力量暂时平衡。,
8EMA: 8周期指数移动平均线，短期趋势跟踪指标。

点击链接看高清技术图
试着自己先找出：① 关键支撑/阻力在哪 ② 失效条件是什么
以上为个人观点分享，不构成任何投资建议
想好再按 🚀 确认已阅读 @everyone"""

PLAN_AKAM_EMBED_TITLE = "BATS:AKAM Chart Image by PaxisTrading"


# -- OKTA: ticker on its own line, single level 127.57 ------------------------

PLAN_OKTA = """OKTA

这个单纯是技术性突破，基本面分析（研报正在写中）其实不怎么支持那么高的估值。但是短期市场技术性操作还是可以考虑操作的。马上要突破127.57这个技术性阻力。 https://www.tradingview.com/x/FSxn85uC/

技术名词解释: 📋
阻力位: 价格上升时可能遇阻回落的关键价位。,
技术性突破: 价格突破关键位置后的走势延续信号。

点击链接看高清技术图
试着自己先找出：① 关键支撑/阻力在哪 ② 失效条件是什么
以上为个人观点分享，不构成任何投资建议
想好再按 🚀 确认已阅读 @everyone"""

PLAN_OKTA_EMBED_TITLE = "BATS:OKTA Chart Image by PaxisTrading"


# -- JOBY: two levels 11.38 (entry zone) + 13.38 (target) ---------------------

PLAN_JOBY = """JOBY 航空板块的股票，这个股票特别难操作，我个人不太会碰。但如果要交易的话，11.38附近是值得留意的阻力区间。如果突破这里，结构上有机会向上补缺，缺口上沿在13.38左右。

https://www.tradingview.com/x/RnIcv4gw/

技术名词解释: 📋
缺口: 价格跳空区域，常形成支撑或阻力。,
阻力: 价格上升时可能遇阻回落的关键位置。

点击链接看高清技术图
试着自己先找出：① 关键支撑/阻力在哪 ② 失效条件是什么
以上为个人观点分享，不构成任何投资建议
想好再按 🚀 确认已阅读 @everyone"""

PLAN_JOBY_EMBED_TITLE = "BATS:JOBY Chart Image by PaxisTrading"


# -- QCOM: 8EMA reclaim, downside invalidation 191 ----------------------------

PLAN_QCOM = """QCOM昨天重新站回8EMA，图形看起来不错。如果要参与的话，我个人会关注接近8EMA的区间。如果跌破191，这个结构就失效了，思路作废。https://www.tradingview.com/x/5AdeykPY/

技术名词解释: 📋
8EMA: 8日指数移动平均线，对价格变化反应更灵敏的短期趋势指标。

点击链接看高清技术图
试着自己先找出：① 关键支撑/阻力在哪 ② 失效条件是什么
以上为个人观点分享，不构成任何投资建议
想好再按 🚀 确认已阅读 @everyone"""

PLAN_QCOM_EMBED_TITLE = "BATS:QCOM Chart Image by PaxisTrading"


# -- META: minimal version, no glossary --------------------------------------

PLAN_META = """META 尝试突破了 https://www.tradingview.com/x/ReKaEvbf/

点击链接看高清技术图
试着自己先找出：① 关键支撑/阻力在哪 ② 失效条件是什么
以上为个人观点分享，不构成任何投资建议
想好再按 🚀 确认已阅读 @everyone"""

PLAN_META_EMBED_TITLE = "BATS:META Chart Image by PaxisTrading"


# -- Casual comment without a chart -- should be REJECTED --------------------

PLAN_NOISE_NO_CHART = "看样子没人抓住TE @everyone"


# -- Reply that's just text, no ticker, no chart -- REJECTED -----------------

PLAN_NOISE_REPLY = "+1 同意你的看法 @everyone"
