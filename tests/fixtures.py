"""
Real fixtures copied verbatim from the user's signal channel.

Two flavors:
  - *_BOLD     : as Discord renders the message (with **asterisks** for bold)
  - *_PLAIN    : as it appears when copied out of the rendered Discord UI (no asterisks)

The parser must handle both.
"""

PLAN_BOLD = """\
\U0001F4CA **\u65E5\u5185\u77ED\u7EBF\u4EA4\u6613\u8BA1\u5212**

**Ticker:** SOUN
**Type:** \U0001F7E2 Long
**Trigger:** > 9.64
**Target:** 11.07
**Setup:** \u7A81\u7834\u4E0A\u5468\u4E94\u7206\u91CF\u62C9\u5347\u7684\u9AD8\u70B9
**Chart:** https://www.tradingview.com/x/ZLvRTfN8/
**Attention:** \u4E0D\u5E26\u5355\uFF0C\u4EC5\u9002\u5408\u6709\u7ECF\u9A8C\u7684trader\uFF0C\u672C\u4EBA\u4F7F\u7528\u4EA4\u6613\u673A\u5668\u4EBA\u6267\u884C\uFF0C\u8282\u594F\u5FEB\uFF0C\u98CE\u9669\u9AD8\uFF0C\u81EA\u8D1F\u76C8\u4E8F @everyone\
"""

TRIGGER_BOLD_AXTI = """\
\U0001F3AF **\u65E5\u5185\u77ED\u7EBF\u89E6\u53D1**
**Ticker:** AXTI
**Type:** \U0001F7E2 LONG
**Trigger Price:** $96.32
**Current Price:** $98.99
**Setup:** Trigger: > 96.32 | Target: None | Setup: \u8D22\u62A5\u540E\u7206\u91CFATH\u7A81\u7834 | Posted by: willtherocket
**Attention:** \u4E0D\u5E26\u5355\uFF0C\u4EC5\u9002\u5408\u6709\u7ECF\u9A8C\u7684trader\uFF0C\u672C\u4EBA\u4F7F\u7528\u4EA4\u6613\u673A\u5668\u4EBA\u6267\u884C\uFF0C\u8282\u594F\u5FEB\uFF0C\u98CE\u9669\u9AD8\uFF0C\u81EA\u8D1F\u76C8\u4E8F @everyone\
"""

TRIGGER_BOLD_SOUN = """\
\U0001F3AF **\u65E5\u5185\u77ED\u7EBF\u89E6\u53D1**
**Ticker:** SOUN
**Type:** \U0001F7E2 LONG
**Trigger Price:** $9.64
**Current Price:** $9.71
**Setup:** Trigger: > 9.64 | Target: 11.07 | Setup: \u7A81\u7834\u4E0A\u5468\u4E94\u7206\u91CF\u62C9\u5347\u7684\u9AD8\u70B9 | Posted by: willtherocket
**Attention:** \u4E0D\u5E26\u5355\uFF0C\u4EC5\u9002\u5408\u6709\u7ECF\u9A8C\u7684trader @everyone\
"""

TRIGGER_PLAIN_LAC = """\
\U0001F3AF \u65E5\u5185\u77ED\u7EBF\u89E6\u53D1
Ticker: LAC
Type: \U0001F7E2 LONG
Trigger Price: $5.77
Current Price: $5.83
Setup: Trigger: > 5.77 | Target: 6.45 | Setup: \u5EF6\u7EED\u6628\u5929\u653E\u91CF\u7A81\u7834\u884C\u60C5 | Posted by: willtherocket
Attention: \u4E0D\u5E26\u5355\uFF0C\u4EC5\u9002\u5408\u6709\u7ECF\u9A8C\u7684trader @everyone\
"""

TRIGGER_PLAIN_LWLG_NO_TARGET = """\
\U0001F3AF \u65E5\u5185\u77ED\u7EBF\u89E6\u53D1
Ticker: LWLG
Type: \U0001F7E2 LONG
Trigger Price: $17.28
Current Price: $17.44
Setup: Trigger: > 17.28 | Target: None | Setup: \u7206\u91CF\u7A81\u7834ATH | Posted by: willtherocket
Attention: \u4E0D\u5E26\u5355\uFF0C\u4EC5\u9002\u5408\u6709\u7ECF\u9A8C\u7684trader @everyone\
"""

PROFIT_PLAIN_AXTI = """\
\U0001F4C8 \u65E5\u5185\u77ED\u7EBF\u76C8\u5229\u63D0\u9192
Ticker: AXTI
Type: \U0001F7E2 LONG
Trigger Price: $96.32
Current Price: $99.82
Profit: +3.6%\
"""

# A scrollback-style blob that contains 4 stacked signals as it appeared in the
# user's original paste -- exercises split_messages.
MIXED_BLOB = "\n".join([
    TRIGGER_PLAIN_LAC,
    TRIGGER_BOLD_SOUN,  # mixed bold + plain in one blob
    TRIGGER_PLAIN_LWLG_NO_TARGET,
    PROFIT_PLAIN_AXTI,
])

# Things that should NOT parse as signals.
NOISE_PLAIN_CHAT = "lol nice trade @bob, did you take it?"
NOISE_BOT_NAME_LINE = "Will the Rocket \u53D1\u5E16BOT\u6768\u5E42\nAPP\n \u2014 6:30 AM"
EMPTY = ""
