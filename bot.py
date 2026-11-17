"""
روبوت التداول — النسخة الكاملة (الحجم حسب المخاطرة + الذكاء الاصطناعي + أسواق متعددة)

يعمل على الحساب التجريبي افتراضياً. للحساب الحقيقي يجب ضبط الاثنين معاً في bot.yml:
    CAP_ENV: "live"   و   LIVE_CONFIRM: "I_UNDERSTAND"

كيف يقرر:
1) السياق: بدون ذكاء اصطناعي (USE_AI=0 افتراضياً): عناوين الأخبار الحديثة (Google News RSS) تُقيَّم بكلمات إيجابية/سلبية،
   وميول متداولي Capital.com (نسبة الشراء) تُستخدم عكسياً عند التطرف. خبر خطير (إيقاف تداول، إفلاس، تحقيق...) يمنع الدخول.
   يمكن تشغيل الذكاء الاصطناعي بدلاً منهما بـ USE_AI=1.
2) التحليل الفني (كل دقيقة) يعطي نتيجة من -100 إلى +100 لاتجاه الدقائق القادمة.
3) النتيجة النهائية = 60% فني + 40% ذكاء اصطناعي. يدخل فقط إذا:
   |النتيجة| >= ENTRY_THR، والفني والذكاء الاصطناعي غير متعاكسين.
4) الحجم = (الرصيد × RISK_PCT%) ÷ (مسافة الوقف × حجم العقد) → أقصى خسارة للصفقة = RISK_PCT% من الرصيد.

الأسهم (STOCKS=1): يمسح كل أسهم Capital.com (أوروبية وأمريكية) كل دورة، يختار الأنشط والأرخص سبريداً المفتوحة الآن،
يحللها بنفس الطريقة (فني + مرتدات + ذكاء اصطناعي) ويفتح حتى MAX_NEW_PER_CYCLE صفقات معاً عند وجود فرصة حقيقية.
MAX_OPEN=0 ومعه حد الخسارة اليومي 0 = بلا حدود. على الحساب الحقيقي يحتاج LIVE_NO_LIMITS=I_ACCEPT_UNLIMITED_RISK.
وقف الخسارة عند الوسيط يوضع دائماً لكل صفقة ولا يمكن إلغاؤه.

الحماية:
- لا أكثر من MAX_OPEN صفقات مفتوحة (0 = بلا حد)، وصفقة واحدة لكل سوق.
- حد خسارة ثابت MAX_DAILY_LOSS_EUR (مثلاً 100€): خسارة اليوم + خطر كل الصفقات المفتوحة لا يتجاوزه أبداً.
- تأمين: عندما يصل الربح لمسافة الوقف، ينقل الوقف لسعر الدخول (صفقة بلا خسارة).
- صفقة إضافية من نفس السوق فقط بعد تأمين الصفقات السابقة فيه (حتى MAX_PER_EPIC).
- المرتدات: بعد نزول/صعود سريع مع RSI متطرف وشمعة انعكاس، يدخل عكسياً بهدف سريع (نصف الحركة).
- كل صفقة لها وقف خسارة (مضمون إن أمكن) وهدف عند الوسيط.
- الصفقة تُغلق فقط عند الهدف أو الوقف (إلا إذا ضُبط MAX_HOLD_MIN).
- حجم الصفقة يبقى ضمن حدودك لكل سوق (SIZE_RANGES).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

E = os.environ.get
LIVE = E("CAP_ENV", "demo") == "live" and E("LIVE_CONFIRM", "") == "I_UNDERSTAND"
BASE = ("https://api-capital.backend-capital.com" if LIVE else "https://demo-api-capital.backend-capital.com") + "/api/v1"
API_KEY, IDENTIFIER, PASSWORD = E("CAP_API_KEY", "").strip(), E("CAP_IDENTIFIER", "").strip(), E("CAP_PASSWORD", "").strip()
CLAUDE_KEY = E("ANTHROPIC_API_KEY", "").strip()
EPICS = [x.strip() for x in E("EPICS", "GOLD,OIL_CRUDE").split(",") if x.strip()]
# حدود الحجم لكل سوق، مثال: "GOLD:1-10,OIL_CRUDE:50-200"
SIZE_RANGES = {k.strip(): tuple(float(x) for x in v.split("-"))
               for k, v in (p.split(":") for p in E("SIZE_RANGES", "GOLD:1-10,OIL_CRUDE:50-200").split(",") if ":" in p)}
RISK_PCT = float(E("RISK_PCT", "2"))
MAX_DAILY_LOSS_PCT = float(E("MAX_DAILY_LOSS_PCT", "5"))
MAX_DAILY_LOSS_EUR = float(E("MAX_DAILY_LOSS_EUR", "100"))   # >0: حد خسارة ثابت باليورو (يشمل خطر الصفقات المفتوحة)
TRADE_RISK_EUR = float(E("TRADE_RISK_EUR", "25"))            # أقصى خسارة للصفقة الواحدة (حتى تتسع لعدة صفقات)
MAX_PER_EPIC = int(E("MAX_PER_EPIC", "3"))                   # صفقات من نفس السوق (الجديدة فقط بعد تأمين السابقة)
BE_TRIGGER = float(E("BE_TRIGGER", "1"))                     # تأمين الصفقة عند ربح = هذا × مسافة الوقف
REBOUND = E("REBOUND", "1") == "1"
REB_MOVE_ATR = float(E("REB_MOVE_ATR", "3"))                 # حجم الحركة السريعة (× ATR) خلال آخر 5 دقائق
REB_RETRACE = float(E("REB_RETRACE", "0.5"))                 # الهدف: استرجاع هذه النسبة من الحركة
MAX_OPEN = int(E("MAX_OPEN", "5"))
ENTRY_THR = float(E("ENTRY_THR", "45"))
MAX_HOLD_MIN = float(E("MAX_HOLD_MIN", "0"))   # 0 = لا إغلاق بالوقت، فقط عند الهدف أو الوقف
SL_MULT = float(E("SL_MULT", "2"))
RR = float(E("RR", "1.5"))
GUARANTEED = E("GUARANTEED_STOP", "1") == "1"
AI_EVERY_MIN = float(E("AI_EVERY_MIN", "60"))
LOOP_MINUTES = float(E("LOOP_MINUTES", "14"))
DRY_RUN = E("DRY_RUN", "0") == "1"
# ===== الأسهم (أوروبية وأمريكية) =====
STOCKS = E("STOCKS", "1") == "1"                              # مسح كل أسهم Capital.com واختيار الأنسب
STOCK_TOP = int(E("STOCK_TOP", "20"))                         # كم سهماً يُحلَّل بالتفصيل كل دورة (الأنشط والأرخص سبريداً)
STOCK_MAX_SPREAD_PCT = float(E("STOCK_MAX_SPREAD_PCT", "0.15"))   # تجاهل الأسهم التي سبريدها أكبر من هذه النسبة من السعر
STOCK_NODES_RE = E("STOCK_NODES_RE", "share|stock|aktie|equit|accion|azioni|action")     # أسماء أقسام الأسهم في قائمة الأسواق
STOCK_EPICS_EXTRA = [x.strip() for x in E("STOCK_EPICS", "").split(",") if x.strip()]   # أسهم تضيفها يدوياً (اختياري)
MAX_UNIVERSE = int(E("MAX_UNIVERSE", "3000"))
MAX_NODE_CALLS = int(E("MAX_NODE_CALLS", "400"))
MAX_NEW_PER_CYCLE = int(E("MAX_NEW_PER_CYCLE", "10"))         # أقصى صفقات جديدة تُفتح معاً في الدورة الواحدة
MAX_MARGIN_PCT = float(E("MAX_MARGIN_PCT", "15"))             # أقصى هامش لصفقة سهم واحدة كنسبة من الرصيد (0 = بلا سقف)
AI_MIN_GAP_MIN = float(E("AI_MIN_GAP_MIN", "30"))             # أقل فاصل بين طلبين للذكاء الاصطناعي عند ظهور أسهم جديدة
LIVE_NO_LIMITS = E("LIVE_NO_LIMITS", "") == "I_ACCEPT_UNLIMITED_RISK"
USE_AI = E("USE_AI", "0") == "1"                              # الذكاء الاصطناعي معطّل افتراضياً؛ البديل: أخبار + ميول المتداولين
NEWS_HOURS = float(E("NEWS_HOURS", "12"))                     # عمر العناوين المعتبرة (ساعات)
NEWS_TTL_MIN = float(E("NEWS_TTL_MIN", "30"))                 # كم يُحفظ تحليل أخبار سوق قبل إعادة الجلب
SENT_WEIGHT = float(E("SENT_WEIGHT", "0.3"))                  # وزن ميول المتداولين (0 = تجاهله)
SENT_EDGE = float(E("SENT_EDGE", "15"))                       # لا يُعتبر الميل إلا إذا ابتعدت نسبة الشراء عن 50% بهذا المقدار
GH_TOKEN, GH_REPO = E("GITHUB_TOKEN", "").strip(), E("GITHUB_REPOSITORY", "").strip()
PUBLISH_ON = E("PUBLISH", "1") == "1"                          # نشر لوحة التطبيق (bot.json) لفرع bot-data
PUBLISH_BRANCH = E("PUBLISH_BRANCH", "bot-data")
PUBLISH_REMOTE = E("PUBLISH_REMOTE", "")                       # للاختبار المحلي فقط
NAMES_AR = {"GOLD": "الذهب", "OIL_CRUDE": "النفط الخام"}
CTX_VETO = float(E("CTX_VETO", "0.25"))                       # الأخبار/الميول تمنع الصفقة فقط إذا عاكستها بهذه القوة
STATE_FILE = "state.json"
BERLIN = ZoneInfo("Europe/Berlin")
WEIGHTS = {"trend": 2, "cross": 1.5, "slope": 1, "rsi": 1, "macd": 1, "mom": 1.5, "live": 0.5}


LOG_BUF = deque(maxlen=80)


def log(*a):
    ts = datetime.now(BERLIN).strftime("%H:%M:%S")
    LOG_BUF.append(ts + " " + " ".join(str(x) for x in a))
    print(ts, *a, flush=True)


# ===== HTTP =====
def http(method, url, headers, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers, json.loads(r.read().decode() or "{}")


class Capital:
    def __init__(self):
        self.cst = self.token = None

    def _h(self):
        return {"Content-Type": "application/json", "X-CAP-API-KEY": API_KEY, "CST": self.cst or "", "X-SECURITY-TOKEN": self.token or ""}

    def login(self):
        try:
            h, _ = http("POST", BASE + "/session", {"Content-Type": "application/json", "X-CAP-API-KEY": API_KEY},
                        {"identifier": IDENTIFIER, "password": PASSWORD, "encryptedPassword": False})
        except urllib.error.HTTPError as e:
            log(f"❌ فشل تسجيل الدخول: HTTP {e.code} {e.read().decode()[:300]}")
            raise SystemExit(1)
        self.cst, self.token = h.get("CST"), h.get("X-SECURITY-TOKEN")
        log(f"✓ تم تسجيل الدخول ({'💰 حساب حقيقي' if LIVE else 'Demo'})")

    def call(self, method, path, body=None):
        for attempt in range(2):
            try:
                return http(method, BASE + path, self._h(), body)[1]
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.login(); continue
                raise RuntimeError(f"{method} {path} → HTTP {e.code}: {e.read().decode()[:300]}")


# ===== المؤشرات =====
def ema(v, n):
    k, o = 2 / (n + 1), []
    for i, x in enumerate(v):
        o.append(x if i == 0 else x * k + o[-1] * (1 - k))
    return o


def rsi(c, n=14):
    o, g, l = [50.0] * len(c), 0.0, 0.0
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]; u, w = max(d, 0), max(-d, 0)
        if i <= n:
            g += u; l += w
            if i == n:
                g /= n; l /= n; o[i] = 100 - 100 / (1 + g / l) if l else 100
        else:
            g = (g * (n - 1) + u) / n; l = (l * (n - 1) + w) / n; o[i] = 100 - 100 / (1 + g / l) if l else 100
    return o


def atr(b, n=14):
    o, a = [None] * len(b), 0.0
    for i in range(1, len(b)):
        tr = max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i - 1]["c"]), abs(b[i]["l"] - b[i - 1]["c"]))
        if i <= n:
            a += tr
            if i == n:
                a /= n; o[i] = a
        else:
            a = (a * (n - 1) + tr) / n; o[i] = a
    return o


clip = lambda x: max(-1.0, min(1.0, x))
mid = lambda p: (float(p["bid"]) + float(p["ask"])) / 2


def bars(cap, epic, res, n):
    d = cap.call("GET", f"/prices/{epic}?resolution={res}&max={n}")
    return [{"o": mid(p["openPrice"]), "h": mid(p["highPrice"]), "l": mid(p["lowPrice"]), "c": mid(p["closePrice"])}
            for p in d.get("prices", [])]


def tech(cap, epic):
    allb, h15 = bars(cap, epic, "MINUTE", 300), bars(cap, epic, "MINUTE_15", 200)
    if len(allb) < 60 or len(h15) < 60:
        return None
    b, live = allb[:-1], allb[-1]
    c = [x["c"] for x in b]
    eF, eS, R, A = ema(c, 9), ema(c, 21), rsi(c), atr(b)
    macd = [x - y for x, y in zip(ema(c, 12), ema(c, 26))]
    hist = [x - y for x, y in zip(macd, ema(macd, 9))]
    trend = ema([x["c"] for x in h15[:-1]], 50)[-1]
    n, a = len(b) - 1, A[-1]
    if not a:
        return None
    fx = {"trend": clip((c[n] - trend) / (2 * a)), "cross": clip((eF[n] - eS[n]) / (0.5 * a)),
          "slope": clip((eF[n] - eF[n - 3]) / a), "rsi": clip((R[n] - 50) / 20), "macd": clip(hist[n] / (0.3 * a)),
          "mom": clip((c[n] - c[n - 5]) / (1.5 * a)), "live": clip((live["c"] - live["o"]) / a)}
    out = {"score": round(sum(fx[k] * WEIGHTS[k] for k in fx) / sum(WEIGHTS.values()) * 100), "atr": a,
           "price": live["c"], "rebound": None}
    if REBOUND:
        out["rebound"] = rebound(b, R, a)
    return out


def rebound(b, R, a, W=6):
    """مرتدة بعد حركة سريعة: نزول/صعود قوي خلال آخر W دقائق + RSI متطرف + شمعة انعكاس مؤكدة."""
    w = b[-W:]
    hi_i = max(range(W), key=lambda i: w[i]["h"]); lo_i = min(range(W), key=lambda i: w[i]["l"])
    hi, lo = w[hi_i]["h"], w[lo_i]["l"]
    move, last = hi - lo, w[-1]
    if move < REB_MOVE_ATR * a:
        return None
    rsis = R[-W:]
    # نزول سريع (القمة قبل القاع) ثم شمعة خضراء ترتد من القاع → شراء
    if hi_i < lo_i and min(rsis) < 25 and last["c"] > last["o"] and last["c"] > lo + 0.15 * move:
        return {"dir": "BUY", "stop": lo - 0.5 * a, "target": lo + REB_RETRACE * move,
                "why": f"مرتدة بعد نزول سريع {move:.2f} (RSI {min(rsis):.0f})"}
    # صعود سريع (القاع قبل القمة) ثم شمعة حمراء ترتد من القمة → بيع
    if lo_i < hi_i and max(rsis) > 75 and last["c"] < last["o"] and last["c"] < hi - 0.15 * move:
        return {"dir": "SELL", "stop": hi + 0.5 * a, "target": hi - REB_RETRACE * move,
                "why": f"مرتدة بعد صعود سريع {move:.2f} (RSI {max(rsis):.0f})"}
    return None


# ===== الذكاء الاصطناعي =====
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), ensure_ascii=False)


def ask_claude(prompt):
    body = {"model": "claude-sonnet-5-5", "max_tokens": 4500, "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]}
    base = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    auths = [{"authorization": "Bearer " + CLAUDE_KEY}, {"x-api-key": CLAUDE_KEY}]
    if not CLAUDE_KEY.startswith("sk-ant-usr"):
        auths.reverse()
    last = None
    for auth in auths:
        try:
            for _ in range(4):
                _, j = http("POST", "https://api.anthropic.com/v1/messages", {**base, **auth}, body, timeout=180)
                if j.get("stop_reason") == "pause_turn":
                    body["messages"].append({"role": "assistant", "content": j["content"]}); continue
                text = "".join(b.get("text", "") for b in j["content"] if b.get("type") == "text")
                m = re.search(r"\{[\s\S]*\}\s*$", text) or re.search(r"\{[\s\S]*\}", text)
                return json.loads(m.group(0))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read().decode()[:200]}"
            if e.code not in (401, 403):
                break
        except Exception as e:
            last = str(e); break
    raise RuntimeError(last or "لا رد")


def ai_fresh(a):
    """رأي الذكاء الاصطناعي صالح فقط إذا لم يمض عليه أكثر من ضعف AI_EVERY_MIN"""
    if isinstance(a, dict) and time.time() - float(a.get("t", 0)) <= 2 * AI_EVERY_MIN * 60:
        return a
    return None


def ai_view(state, symbols=None, names=None):
    """symbols: الأسواق المطلوب رأيها (الذهب/النفط + الأسهم المرشّحة الآن). الأسهم الجديدة تُضاف دون انتظار ساعة كاملة (بفاصل AI_MIN_GAP_MIN)."""
    if not CLAUDE_KEY:
        return {}
    symbols, names = symbols or list(EPICS), names or {}
    ai = state.get("ai", {})
    data = ai.get("data", {})
    age = time.time() - ai.get("time", 0)
    missing = [s for s in symbols if not ai_fresh(data.get(s))]
    if age < AI_EVERY_MIN * 60 and (not missing or age < AI_MIN_GAP_MIN * 60):
        return data
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M")
    listing = ", ".join(f"{s} ({names[s]})" if names.get(s) else s for s in symbols)
    prompt = f"""أنت محلل أسواق للمضاربة قصيرة المدى. الوقت الآن {now} بتوقيت برلين.
ابحث في الإنترنت عن أحدث الأخبار (آخر 24 ساعة) والأحداث القادمة اليوم: البنوك المركزية، التضخم والوظائف، الجيوسياسة، أوبك، نتائج الشركات وتوصيات المحللين، آراء البنوك الكبرى، ومزاج المتداولين.
ثم أعطِ الاتجاه الأرجح للساعات القليلة القادمة لكل سوق من هذه الرموز (رموز Capital.com): {listing}.
اختر buy أو sell عندما يوجد سبب واضح، و wait إذا كان هناك خبر قوي خلال 30 دقيقة أو الصورة متضاربة أو لا يوجد محفّز واضح لسهم معيّن.
أجب في النهاية بـ JSON فقط بدون أي نص بعده، بهذا الشكل (مفتاح لكل رمز بالضبط كما كُتب):
{{"GOLD":{{"bias":"buy|sell|wait","confidence":0-100,"reason":"جملة قصيرة"}}, ...}}"""
    try:
        new = ask_claude(prompt)
        stamp = time.time()
        for v in new.values():
            if isinstance(v, dict):
                v["t"] = stamp
        merged = {k: v for k, v in data.items() if ai_fresh(v)}   # نحتفظ بالآراء الحديثة السابقة ونحدّث الجديدة
        merged.update(new)
        state["ai"] = {"time": stamp, "data": merged}
        save_state(state)
        log("🤖 تحليل الذكاء الاصطناعي:")
        for k, v in new.items():
            if isinstance(v, dict):
                log(f"   {k}: {v.get('bias')} {v.get('confidence')}% — {v.get('reason', '')}")
        return merged
    except Exception as e:
        log("⚠️ تعذّر تحليل الذكاء الاصطناعي:", e)
        return data


# ===== الحساب =====
def balance(cap):
    accs = cap.call("GET", "/accounts").get("accounts", [])
    acc = next((a for a in accs if a.get("preferred")), accs[0] if accs else None)
    return float(acc["balance"]["balance"]) if acc else 0.0, (acc or {}).get("currency", "")


def pnl_today(cap):
    start = datetime.now(BERLIN).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    q = urllib.parse.urlencode({"from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                                "to": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "type": "TRADE"})
    try:
        return sum(float(t.get("size", 0) or 0) for t in cap.call("GET", "/history/transactions?" + q).get("transactions", []))
    except Exception as e:
        log("⚠️ تعذّر قراءة نتيجة اليوم:", e)
        return 0.0


def min_dist(rules, key, price):
    r = rules.get(key) or {}
    v = float(r.get("value") or 0)
    return price * v / 100 if r.get("unit") == "PERCENTAGE" else v


def step_round(x, step):
    return round((x // step) * step, 6) if step > 0 else x


# ===== منطق الصفقة =====
# ===== سياق السوق بدون ذكاء اصطناعي: أخبار (عناوين) + ميول المتداولين على Capital.com =====
POS_WORDS = ("surge", "soar", "jump", "rally", "beat", "upgrade", "record high", "raises", "raised", "strong", "gain",
             "rise", "rises", "climb", "outperform", "approval", "approved", "wins", "profit", "growth", "bullish",
             "boost", "higher", "recovers", "rebound")
NEG_WORDS = ("plunge", "slump", "tumble", "drop", "falls", "fall", "miss", "downgrade", "weak", "loss", "losses",
             "lawsuit", "probe", "investigation", "recall", "warning", "warns", "bearish", "sell-off", "selloff",
             "slides", "sinks", "decline", "layoff", "fraud", "bankruptcy", "halt", "lower", "cuts", "slumps")
NEWS_BLOCK = [w.strip().lower() for w in E("NEWS_BLOCK", "trading halt,halted,bankruptcy,fraud,sec charges,investigation,probe,delisting").split(",") if w.strip()]
NEWS_QUERY = {"GOLD": "gold price", "OIL_CRUDE": "crude oil price"}


def _clean_name(n):
    return re.sub(r"\b(inc|corp|corporation|plc|ag|se|sa|nv|ltd|co|holdings?|group|class [a-z])\b\.?", "", n or "", flags=re.I).strip(" ,.-")


def fetch_news(query):
    """عناوين آخر NEWS_HOURS ساعة من Google News RSS (مجاني، بدون مفتاح). يرجع قائمة عناوين."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        root = ET.fromstring(r.read())
    out, now = [], datetime.now(timezone.utc)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            age_h = (now - parsedate_to_datetime(it.findtext("pubDate"))).total_seconds() / 3600
        except Exception:
            age_h = 0
        if title and age_h <= NEWS_HOURS:
            out.append(title)
    return out[:15]


def score_headlines(titles):
    """-1..+1 من عدد الكلمات الإيجابية/السلبية في العناوين، و block إذا وُجد حدث خطير"""
    if not titles:
        return 0.0, False
    tot, block = 0.0, False
    for t in titles:
        low = t.lower()
        if any(w in low for w in NEWS_BLOCK):
            block = True
        p, n = sum(w in low for w in POS_WORDS), sum(w in low for w in NEG_WORDS)
        tot += max(-1, min(1, p - n))
    return max(-1.0, min(1.0, tot / max(3, len(titles)))), block


def news_view(state, epic, name):
    cache = state.setdefault("news", {})
    c = cache.get(epic)
    if c and time.time() - c["t"] < NEWS_TTL_MIN * 60:
        return c
    q = NEWS_QUERY.get(epic) or ((_clean_name(name) + " stock") if name else epic + " stock")
    try:
        titles = fetch_news(q)
    except Exception as e:
        if not state.get("news_err"):
            log("⚠️ تعذّر جلب الأخبار (سأكمل بدونها):", e)
            state["news_err"] = True
        titles = []
    s, block = score_headlines(titles)
    c = {"t": time.time(), "score": s, "n": len(titles), "block": block, "top": titles[:2]}
    cache[epic] = c
    return c


def sentiment_view(cap, state, epic):
    """ميول المتداولين على Capital.com: نسبة الشراء. نستخدمه عكسياً عند التطرف (الأغلبية الكبيرة غالباً على الجهة الخاطئة)."""
    cache = state.setdefault("sent", {})
    c = cache.get(epic)
    if c and time.time() - c["t"] < 600:
        return c.get("long")
    long = None
    try:
        d = cap.call("GET", f"/clientsentiment?marketIds={urllib.parse.quote(epic)}")
        cs = (d.get("clientSentiments") or [{}])[0]
        long = float(cs["longPositionPercentage"])
    except Exception as e:
        if not state.get("sent_err"):
            log("⚠️ ميول المتداولين غير متاح لهذه الأسواق (سأكمل بدونه):", e)
            state["sent_err"] = True
    cache[epic] = {"t": time.time(), "long": long}
    return long


def market_ctx(cap, state, epic, name):
    """يبني رأياً بنفس شكل رأي الذكاء الاصطناعي من الأخبار + الميول. None إذا لا يوجد شيء ذي معنى."""
    nv = news_view(state, epic, name)
    long = sentiment_view(cap, state, epic)
    sent = 0.0
    if long is not None and abs(long - 50) >= SENT_EDGE:
        sent = max(-1.0, min(1.0, (50 - long) / 30))          # أغلبية شراء = ميل هبوطي، والعكس
    parts = []
    if nv["n"]:
        parts.append(f"أخبار {nv['score']:+.2f} ({nv['n']} عنوان)")
    if long is not None:
        parts.append(f"{long:.0f}% شراء")
    why = " | ".join(parts)
    if nv["top"]:
        log(f"📰 {epic}: {nv['top'][0][:90]}")
    if nv["block"]:
        return {"bias": "wait", "confidence": 100, "reason": "خبر خطير (" + (nv["top"][0][:60] if nv["top"] else "") + ")", "soft": True, "t": time.time()}
    a = max(-1.0, min(1.0, 0.7 * nv["score"] + SENT_WEIGHT * sent))
    if abs(a) < 0.1:
        return None
    return {"bias": "buy" if a > 0 else "sell", "confidence": round(abs(a) * 100), "reason": why, "soft": True, "t": time.time()}


def decide(t, ai):
    """يرجع (الاتجاه، النتيجة النهائية، السبب)"""
    if ai and ai.get("bias") == "wait":
        return None, 0, ai.get("reason") or "الذكاء الاصطناعي ينصح بالانتظار (خبر قوي أو صورة متضاربة)"
    if ai and ai.get("bias") in ("buy", "sell"):
        a = {"buy": 1, "sell": -1}[ai["bias"]] * (float(ai.get("confidence", 50)) / 100)
        if a * t["score"] < 0 and not (ai.get("soft") and abs(a) < CTX_VETO):
            return None, 0, "الفني والأخبار/الميول متعاكسان" if ai.get("soft") else "الفني والذكاء الاصطناعي متعاكسان"
        final = round(0.6 * t["score"] + 40 * a)
        thr = ENTRY_THR
    else:
        final, thr = t["score"], ENTRY_THR + 10    # بدون سياق (أخبار/ميول): نطلب قوة فنية أعلى
    if abs(final) < thr:
        return None, final, "القوة غير كافية"
    return ("BUY" if final > 0 else "SELL"), final, ""


def open_trade(cap, epic, direction, t, bal, ai_reason, budget, levels=None):
    m = cap.call("GET", f"/markets/{epic}")
    snap, rules, inst = m.get("snapshot", {}), m.get("dealingRules", {}), m.get("instrument", {})
    if snap.get("marketStatus") != "TRADEABLE":
        log(f"{epic}: السوق مغلق"); return
    buy = direction == "BUY"
    price = float(snap["offer"] if buy else snap["bid"])
    spread = float(snap["offer"]) - float(snap["bid"])
    dist = max(t["atr"] * SL_MULT, spread * 3, min_dist(rules, "minStopOrProfitDistance", price) * 1.1)
    if levels:                                     # المرتدة: وقف خلف القمة/القاع
        dist = max(abs(price - levels["stop"]), spread * 3, min_dist(rules, "minStopOrProfitDistance", price) * 1.1)
    if GUARANTEED:
        dist = max(dist, min_dist(rules, "minGuaranteedStopDistance", price) * 1.1)
    lot = float(inst.get("lotSize") or 1)
    risk_money = min(bal * RISK_PCT / 100, budget, TRADE_RISK_EUR or 1e12) * 0.9   # 10% هامش أمان لفرق العملة
    min_size = float(rules["minDealSize"]["value"])
    step = float((rules.get("minSizeIncrement") or {}).get("value") or min_size)
    size = step_round(risk_money / (dist * lot), step)
    max_size = float((rules.get("maxDealSize") or {}).get("value") or 1e12)
    size = min(size, max_size)
    cap_size = None                                 # سقف الهامش لصفقة سهم (الأسهم وقفها قريب فيطلع حجم ضخم بدون هذا السقف)
    mf = float(inst.get("marginFactor") or 0)
    if epic not in SIZE_RANGES and MAX_MARGIN_PCT > 0 and mf > 0:
        cap_size = step_round(bal * MAX_MARGIN_PCT / 100 / (price * lot * mf / 100), step)
        size = min(size, cap_size)
    if epic in SIZE_RANGES:                         # حدودك أنت لها الأولوية
        lo, hi = SIZE_RANGES[epic]
        size = min(max(size, lo), hi)
    elif size < min_size:
        if min_size * dist * lot > risk_money * 1.5:
            log(f"{epic}: أصغر حجم ({min_size}) خطره أكبر من {RISK_PCT}% — تخطي"); return
        size = min_size
    size = max(size, min_size)
    if min_size * dist * lot > risk_money * 1.5:
        log(f"{epic}: أصغر حجم ({min_size}) خطره {min_size * dist * lot:.2f} أكبر من حد المخاطرة ({risk_money:.2f}) — تخطي"); return
    if cap_size is not None and size > cap_size:
        log(f"{epic}: أصغر حجم ({min_size}) يتجاوز سقف الهامش {MAX_MARGIN_PCT:.0f}% من الرصيد — تخطي"); return
    dec = int(snap.get("decimalPlacesFactor") or (3 if price < 10 else 2))
    stop = round(price - dist if buy else price + dist, dec)
    target = round(price + dist * RR if buy else price - dist * RR, dec)
    if levels:
        target = round(levels["target"], dec)
        if (target - price if buy else price - target) < 0.6 * dist:
            log(f"{epic}: المرتدة ربحها صغير مقارنة بالوقف — تخطي"); return
    loss = size * dist * lot
    if loss > budget or (TRADE_RISK_EUR and loss > TRADE_RISK_EUR * 1.5):
        log(f"{epic}: خطر الصفقة {loss:.2f} أكبر من المسموح (المتبقي {budget:.2f}، للصفقة {TRADE_RISK_EUR:.0f}) — تخطي"); return
    log(f"{epic}: {'🟢 شراء' if buy else '🔴 بيع'} حجم {size} @ {price} | وقف {stop} | هدف {target} | "
        f"أقصى خسارة ≈ {loss:.2f} ({loss / bal * 100:.1f}% من الرصيد) | {ai_reason}")
    if DRY_RUN:
        return
    order = {"epic": epic, "direction": direction, "size": size, "stopLevel": stop, "profitLevel": target,
             "guaranteedStop": GUARANTEED}
    for g in ([True, False] if GUARANTEED else [False]):
        order["guaranteedStop"] = g
        try:
            ref = cap.call("POST", "/positions", order).get("dealReference")
            time.sleep(1)
            c = cap.call("GET", f"/confirms/{ref}")
            log(f"{epic}: {c.get('dealStatus')} {c.get('reason', '')} {'(وقف مضمون)' if g else ''}")
            if c.get("dealStatus") == "ACCEPTED":
                ids = [c.get("dealId")] + [a.get("dealId") for a in c.get("affectedDeals", []) or []]
                return {"loss": loss, "ids": list(dict.fromkeys(i for i in ids if i)), "size": size, "price": price, "stop": stop, "target": target}
        except Exception as e:
            log(f"{epic}: رفض {'بوقف مضمون' if g else ''}: {e}")


def pos_risk(p):
    """الخسارة الممكنة لصفقة مفتوحة حتى وقفها (0 إذا كانت مؤمّنة)"""
    pos = p["position"]; lvl, stop = float(pos["level"]), pos.get("stopLevel")
    if stop is None:
        return float(pos["size"]) * lvl * 0.02            # بدون وقف: تقدير محافظ
    buy = pos["direction"] == "BUY"
    loss_per = (lvl - float(stop)) if buy else (float(stop) - lvl)
    return max(0.0, loss_per) * float(pos["size"])


def secure(cap, p):
    """تأمين الصفقة: نقل الوقف لسعر الدخول عندما يصل الربح لـ BE_TRIGGER × مسافة الوقف"""
    pos, mk = p["position"], p["market"]
    lvl, stop, buy = float(pos["level"]), pos.get("stopLevel"), pos["direction"] == "BUY"
    if stop is None or pos_risk(p) == 0:
        return
    dist = abs(lvl - float(stop))
    now = float(mk["bid"] if buy else mk["offer"])
    gain = (now - lvl) if buy else (lvl - now)
    if gain >= BE_TRIGGER * dist:
        spread = abs(float(mk["offer"]) - float(mk["bid"]))
        new_stop = round(lvl + spread if buy else lvl - spread, 5)
        log(f"{mk['epic']}: 🔒 تأمين الصفقة — نقل الوقف إلى {new_stop} (ربح حالي {gain:.2f})")
        event(type="secure", epic=mk["epic"], name=mk.get("instrumentName") or disp_name({}, mk["epic"]), dir=pos["direction"],
              price=new_stop, why=f"نقل الوقف لسعر الدخول (ربح حالي {gain:.2f})")
        if not DRY_RUN:
            body = {"stopLevel": new_stop}
            if pos.get("profitLevel") is not None:
                body["profitLevel"] = float(pos["profitLevel"])
            cap.call("PUT", f"/positions/{pos['dealId']}", body)
            pos["stopLevel"] = new_stop


def day_pnl(state, bal):
    """نتيجة اليوم = الرصيد الآن − رصيد بداية اليوم (يبدأ من الصفر كل يوم، ولا يتأثر بخسائر قديمة)"""
    today = datetime.now(BERLIN).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"], state["start_bal"] = today, bal
        save_state(state)
        log(f"📅 يوم جديد — رصيد البداية {bal:.2f}")
    return bal - state["start_bal"]


def nav(cap, node=None):
    return cap.call("GET", "/marketnavigation" + (f"/{node}?limit=500" if node else ""))


def build_universe(cap, state):
    """كل أسهم Capital.com المتاحة (أوروبية وأمريكية وغيرها). تُحدَّث مرة كل 24 ساعة وتُحفظ في state."""
    u = state.get("universe") or {}
    if u and u.get("time", 0) > time.time() - 24 * 3600:
        return u.get("epics", {})
    epics, calls = {}, [0]

    def walk(node_id, depth):
        if not node_id or calls[0] >= MAX_NODE_CALLS or depth > 4 or len(epics) >= MAX_UNIVERSE:
            return
        calls[0] += 1
        try:
            d = nav(cap, node_id)
        except Exception as e:
            log("⚠️ تصفّح قسم الأسواق:", e)
            return
        time.sleep(0.12)
        for m in d.get("markets") or []:
            if str(m.get("instrumentType", "")).upper() == "SHARES" and m.get("epic"):
                epics[m["epic"]] = m.get("instrumentName", "")
        for n in d.get("nodes") or []:
            walk(n.get("id"), depth + 1)

    try:
        roots = nav(cap).get("nodes") or []
        log("📂 أقسام Capital.com: " + " | ".join(f"{n.get('name')} [{n.get('id')}]" for n in roots))
        hit = [n for n in roots if re.search(STOCK_NODES_RE, f"{n.get('name', '')} {n.get('id', '')}", re.I)]
        if not hit:   # لا قسم باسم أسهم: نمسح كل الأقسام عدا ما هو واضح أنه ليس أسهماً (نأخذ فقط ما نوعه SHARES)
            skip = re.compile(r"crypto|currenc|forex|commodit|index|indic|metal|bond|option|fx", re.I)
            hit = [n for n in roots if not skip.search(f"{n.get('name', '')} {n.get('id', '')}")]
            log("⚠️ لا قسم باسم أسهم — أمسح هذه الأقسام: " + ", ".join(str(n.get("name")) for n in hit))
        for n in hit:
            walk(n.get("id"), 1)
    except Exception as e:
        log("⚠️ تعذّر قراءة قائمة الأسواق:", e)
    for e in STOCK_EPICS_EXTRA:
        epics.setdefault(e, "")
    log(f"📚 قائمة الأسهم: {len(epics)} سهم ({calls[0]} طلب)")
    if not epics:
        log("⚠️ لم أجد أسهماً — أضف رموزاً يدوياً في STOCK_EPICS أو عدّل STOCK_NODES_RE")
    # إذا فشل البحث نعيد المحاولة بعد ساعة فقط (لا نكرر الطلبات كل دقيقة)
    state["universe"] = {"time": time.time() if epics else time.time() - 23 * 3600, "epics": epics}
    save_state(state)
    return epics


def scan_stocks(cap, universe):
    """يمسح كل الأسهم دفعات (50 رمزاً/طلب) ويرجع الأنشط (حركة اليوم + مدى الحركة) والأرخص سبريداً، المفتوحة الآن فقط."""
    ep = [e for e in universe if e not in EPICS]
    rows = []
    for i in range(0, len(ep), 50):
        chunk = ep[i:i + 50]
        try:
            d = cap.call("GET", "/markets?epics=" + urllib.parse.quote(",".join(chunk), safe=","))
        except Exception as e:
            log("⚠️ مسح دفعة أسهم:", e)
            continue
        time.sleep(0.12)
        for m in d.get("marketDetails") or []:
            s, ins = m.get("snapshot") or {}, m.get("instrument") or {}
            if s.get("marketStatus") != "TRADEABLE" or not ins.get("epic"):
                continue
            try:
                bid, ask = float(s["bid"]), float(s["offer"])
                hi, lo = float(s.get("high") or 0), float(s.get("low") or 0)
                chg = abs(float(s.get("percentageChange") or 0))
            except (KeyError, TypeError, ValueError):
                continue
            mp = (bid + ask) / 2
            if mp <= 0:
                continue
            spread = (ask - bid) / mp * 100
            if spread > STOCK_MAX_SPREAD_PCT:
                continue
            rng = (hi - lo) / mp * 100 if hi > lo > 0 else 0
            rows.append((chg + 0.5 * rng - 3 * spread, ins["epic"]))
    rows.sort(reverse=True)
    log(f"🔭 مسح الأسهم: {len(ep)} سهم، المفتوح والمناسب منها {len(rows)} → أحلل أفضل {min(STOCK_TOP, len(rows))}")
    return [e for _, e in rows[:STOCK_TOP]]


# ===== لوحة التطبيق: تسجيل العمليات ونشرها (فرع bot-data، لقطة وحيدة بلا تاريخ) =====
_STATE = {}
_PUB_ERR = [False]


def event(**kw):
    """يسجّل عملية (فتح/إغلاق/تأمين) ضمن state ليعرضها التطبيق"""
    ev = {"t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **kw}
    _STATE["events"] = (_STATE.get("events", []) + [ev])[-200:]


def disp_name(names, epic):
    return NAMES_AR.get(epic) or (names or {}).get(epic) or epic


def open_view(p, names):
    pos, mk = p["position"], p["market"]
    buy = pos["direction"] == "BUY"
    try:
        now = float(mk["bid"] if buy else mk["offer"])
    except Exception:
        now = None
    return {"epic": mk["epic"], "name": mk.get("instrumentName") or disp_name(names, mk["epic"]), "dir": pos["direction"],
            "size": pos.get("size"), "entry": pos.get("level"), "stop": pos.get("stopLevel"), "target": pos.get("profitLevel"),
            "now": now, "upl": pos.get("upl"), "secured": pos_risk(p) == 0, "opened": pos.get("createdDateUTC")}


def mkt(state, names, epic, t, a, d, final, why, r=None):
    nv = (state.get("news") or {}).get(epic) or {}
    sl = ((state.get("sent") or {}).get(epic) or {}).get("long")
    return {"epic": epic, "name": disp_name(names, epic), "price": round(t["price"], 4), "tech": t["score"], "final": final,
            "verdict": d, "why": why, "rebound": r["why"] if r else None,
            "ctx": ({"bias": a.get("bias"), "conf": a.get("confidence"), "reason": a.get("reason")} if a else None),
            "news": nv.get("top") or [], "news_score": nv.get("score"), "news_n": nv.get("n"), "long": sl}


def history_rows(cap, state):
    h = state.get("hist") or {}
    if time.time() - h.get("t", 0) < 300:
        return h.get("rows", [])
    rows = h.get("rows", [])
    try:
        now = datetime.now(timezone.utc)
        q = urllib.parse.urlencode({"from": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"),
                                    "to": now.strftime("%Y-%m-%dT%H:%M:%S"), "type": "TRADE"})
        rows = [{"t": x.get("dateUtc") or x.get("date"), "name": x.get("instrumentName"), "pnl": float(x.get("size") or 0),
                 "note": x.get("note")} for x in cap.call("GET", "/history/transactions?" + q).get("transactions", [])]
        rows.sort(key=lambda r: r["t"] or "", reverse=True)
        rows = rows[:60]
    except Exception as e:
        log("⚠️ تعذّر قراءة سجل المعاملات:", e)
    state["hist"] = {"t": time.time(), "rows": rows}
    return rows


def _scrub(s):
    return s.replace(GH_TOKEN, "***") if GH_TOKEN else s


def publish(state, view):
    """يكتب bot.json ويرفعه (force-push) لفرع bot-data. التطبيق يقرأه من هناك. لا يؤثر على الفرع الرئيسي ولا يترك تاريخاً."""
    if not PUBLISH_ON:
        return
    remote = PUBLISH_REMOTE or (f"https://x-access-token:{GH_TOKEN}@github.com/{GH_REPO}.git" if GH_TOKEN and GH_REPO else "")
    if not remote:
        if not _PUB_ERR[0]:
            log("ℹ️ نشر اللوحة معطّل (لا GITHUB_TOKEN/GITHUB_REPOSITORY)")
            _PUB_ERR[0] = True
        return
    mk = sorted(view.get("markets", []), key=lambda m: -abs(m.get("final") or 0))[:30]
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "live" if LIVE else "demo",
            "dry_run": DRY_RUN,
            "settings": {"max_open": MAX_OPEN, "daily_limit_eur": MAX_DAILY_LOSS_EUR, "daily_limit_pct": MAX_DAILY_LOSS_PCT,
                         "risk_pct": RISK_PCT, "trade_risk_eur": TRADE_RISK_EUR, "stocks": STOCKS, "ai": USE_AI,
                         "markets": EPICS, "max_new": MAX_NEW_PER_CYCLE},
            **{k: v for k, v in view.items() if k != "markets"}, "markets": mk,
            "events": state.get("events", [])[-120:][::-1], "log": list(LOG_BUF)}
    tmp = tempfile.mkdtemp(prefix="pub_")
    try:
        with open(os.path.join(tmp, "bot.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for args in (["init", "-q"], ["checkout", "-q", "-b", PUBLISH_BRANCH], ["add", "bot.json"], ["commit", "-q", "-m", "bot data"],
                     ["push", "-q", "--force", remote, f"HEAD:refs/heads/{PUBLISH_BRANCH}"]):
            r = subprocess.run(["git", "-c", "user.name=bot", "-c", "user.email=bot@users.noreply.github.com", *args],
                               cwd=tmp, env=env, capture_output=True, text=True, timeout=40)
            if r.returncode:
                raise RuntimeError(_scrub((r.stderr or r.stdout or "")[:300]))
        _PUB_ERR[0] = False
    except Exception as e:
        if not _PUB_ERR[0]:
            log("⚠️ تعذّر نشر اللوحة:", _scrub(str(e))[:300])
            _PUB_ERR[0] = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cycle(cap, state):
    global _STATE
    _STATE = state
    view = {"balance": None, "open": [], "markets": [], "scan": {}}
    try:
        _cycle(cap, state, view)
    finally:
        try:
            view["history"] = history_rows(cap, state)
            publish(state, view)
        except Exception as e:
            log("⚠️ نشر:", e)


def _cycle(cap, state, view):
    bal, cur = balance(cap)
    today = day_pnl(state, bal)
    view.update(balance=round(bal, 2), currency=cur, day_pnl=round(today, 2), day_start=round(state.get("start_bal", bal), 2))
    allpos = cap.call("GET", "/positions").get("positions", [])
    ours, ours_ep = set(state.get("ours", [])), set(state.get("ours_epics", []))
    # الروبوت يدير فقط: صفقات أسواقه (EPICS) وصفقات الأسهم التي فتحها هو. لا يلمس صفقاتك اليدوية.
    positions = [p for p in allpos if p["market"]["epic"] in ours_ep or p["position"]["dealId"] in ours]
    foreign_eps = {p["market"]["epic"] for p in allpos} - {p["market"]["epic"] for p in positions}
    if len(allpos) > len(positions):
        log(f"(تجاهل {len(allpos) - len(positions)} صفقة ليست من الروبوت)")
    live_ids = {p["position"]["dealId"] for p in allpos}
    live_eps = {p["market"]["epic"] for p in allpos}
    state["ours"] = [i for i in ours if i in live_ids]
    state["ours_epics"] = [e for e in ours_ep if e in live_eps]
    # إغلاق بالوقت (معطّل افتراضياً) + تأمين الصفقات الرابحة
    for p in positions:
        pos = p["position"]
        opened = datetime.fromisoformat(pos["createdDateUTC"].replace("Z", "")).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        if MAX_HOLD_MIN > 0 and age > MAX_HOLD_MIN:
            log(f"{p['market']['epic']}: إغلاق بعد {age:.0f} دقيقة (النتيجة {pos.get('upl')})")
            event(type="close", epic=p["market"]["epic"], name=p["market"].get("instrumentName") or p["market"]["epic"],
                  dir=pos["direction"], size=pos.get("size"), price=pos.get("level"), why=f"إغلاق بعد {age:.0f} دقيقة (MAX_HOLD_MIN)")
            if not DRY_RUN:
                cap.call("DELETE", f"/positions/{pos['dealId']}")
            continue
        try:
            secure(cap, p)
        except Exception as e:
            log(f"{p['market']['epic']}: تعذّر التأمين", e)
    names_all = (state.get("universe") or {}).get("epics", {})
    view["open"] = [open_view(p, names_all) for p in positions]
    now_ids = {p["position"]["dealId"] for p in positions}
    for did, pv in (state.get("prev_open") or {}).items():
        if did not in now_ids:
            event(type="close", epic=pv["epic"], name=pv["name"], dir=pv["dir"], size=pv["size"], price=pv["entry"],
                  why="انتهت الصفقة (هدف أو وقف أو إغلاق يدوي) — النتيجة في سجل المعاملات")
    state["prev_open"] = {p["position"]["dealId"]: {"epic": p["market"]["epic"], "dir": p["position"]["direction"],
                                                    "name": p["market"].get("instrumentName") or disp_name(names_all, p["market"]["epic"]),
                                                    "size": p["position"].get("size"), "entry": p["position"].get("level")}
                          for p in positions}
    # حد الخسارة اليومي (اختياري): خسارة اليوم + خطر الصفقات المفتوحة لا يتجاوزه. إذا كان كله 0 فلا حد.
    cap_open = MAX_OPEN if MAX_OPEN > 0 else 10 ** 9
    if MAX_DAILY_LOSS_EUR > 0:
        limit = MAX_DAILY_LOSS_EUR
    elif MAX_DAILY_LOSS_PCT > 0:
        limit = bal * MAX_DAILY_LOSS_PCT / 100
    else:
        limit = None
    open_risk = sum(pos_risk(p) for p in positions)
    budget = 1e12 if limit is None else limit + min(today, 0) - open_risk
    log(f"الرصيد {bal:.2f} {cur} | اليوم {today:+.2f} | مفتوحة {len(positions)}/{MAX_OPEN or '∞'} | خطر مفتوح {open_risk:.2f} | "
        + (f"المتبقي من حد {limit:.0f}: {budget:.2f}" if limit is not None else "بلا حد يومي"))
    if budget <= 0 or len(positions) >= cap_open:
        if budget <= 0:
            log("⛔ لا مجال لصفقات جديدة (حد الخسارة)")
        return
    # سوق مسموح فيه صفقة جديدة: لا صفقات فيه، أو كل صفقاته مؤمّنة، وبحد MAX_PER_EPIC (سهم واحد = صفقة واحدة)
    by_epic = {}
    for p in positions:
        by_epic.setdefault(p["market"]["epic"], []).append(p)
    stocks, names = [], (state.get("universe") or {}).get("epics", {})
    if STOCKS:
        try:
            names = build_universe(cap, state)
            stocks = scan_stocks(cap, names)
        except Exception as e:
            log("⚠️ تعذّر مسح الأسهم:", e)
    view["scan"] = {"universe": len(names), "analyzed": len(stocks), "stocks_on": STOCKS}
    ai = ai_view(state, EPICS + stocks, names) if USE_AI else {}
    candidates = []
    for epic in EPICS + stocks:
        mine = by_epic.get(epic, [])
        max_here = MAX_PER_EPIC if epic in EPICS else 1
        if len(mine) >= max_here or any(pos_risk(p) > 0 for p in mine):
            continue
        if epic in foreign_eps:                     # عندك صفقة يدوية في هذا السوق: لا نفتح فيه ولا نلمسها
            log(f"{epic}: عندك صفقة يدوية فيه — تخطي")
            continue
        label = epic if epic in EPICS or not names.get(epic) else f"{epic} ({names[epic]})"
        try:
            t = tech(cap, epic)
            time.sleep(0.15)
            if not t:
                continue
            a = ai_fresh(ai.get(epic)) if USE_AI else None
            if not USE_AI and (abs(t["score"]) >= 0.6 * ENTRY_THR or t.get("rebound")):
                a = market_ctx(cap, state, epic, names.get(epic, ""))   # أخبار + ميول المتداولين فقط للمرشّحين
            r = t.get("rebound")
            if r and not (a and a.get("bias") == "wait"):
                log(f"{label}: ⚡ {r['why']} → ✅ {r['dir']}")
                view["markets"].append(mkt(state, names, epic, t, a, r["dir"], 101, r["why"], r))
                candidates.append((101, epic, r["dir"], t, r["why"], r))
                continue
            d, final, why = decide(t, a)
            log(f"{label}: فني {t['score']:+d}" + (f" | {'AI' if USE_AI else 'سياق'} {a.get('bias')} {a.get('confidence')}% {('(' + a['reason'] + ')') if a.get('reason') and not USE_AI else ''}" if a else "")
                + f" → {final:+d} {('✅ ' + d) if d else why}" + (f" | صفقات مؤمّنة {len(mine)}" if mine else ""))
            view["markets"].append(mkt(state, names, epic, t, a, d, final, why))
            if d:
                candidates.append((abs(final), epic, d, t, (a or {}).get("reason", ""), None))
        except Exception as e:
            log(f"{label}: خطأ", e)
    slots = max(0, min(MAX_NEW_PER_CYCLE, cap_open - len(positions)))
    for sc, epic, d, t, reason, lv in sorted(candidates, key=lambda x: -x[0])[:slots]:
        if budget <= 0:
            break
        try:
            res = open_trade(cap, epic, d, t, bal, reason, budget, lv)
            if res:
                budget -= res["loss"]
                event(type="open", epic=epic, name=disp_name(names, epic), dir=d, size=res["size"], price=res["price"],
                      stop=res["stop"], target=res["target"], risk=round(res["loss"], 2), kind="مرتدة" if lv else "إشارة",
                      score=sc, why=reason, news=(state.get("news", {}).get(epic, {}).get("top") or []))
                if epic not in EPICS:
                    state.setdefault("ours_epics", []).append(epic)
                state.setdefault("ours", []).extend(res["ids"])
                save_state(state)
        except Exception as e:
            log(f"{epic}: خطأ في فتح الصفقة", e)
        time.sleep(0.15)


def discover(cap):
    """مرة واحدة: يعرض كل أدوات الذهب والنفط المتاحة في حسابك (لنعرف هل منتجات KO متاحة عبر الـ API)"""
    for term in ("gold", "oil", "knock"):
        try:
            ms = cap.call("GET", "/markets?" + urllib.parse.urlencode({"searchTerm": term})).get("markets", [])
            log(f"🔎 بحث '{term}': {len(ms)} نتيجة")
            for m in ms[:25]:
                log(f"   {m.get('epic')} | {m.get('instrumentName')} | {m.get('instrumentType')} | {m.get('marketStatus')}")
        except Exception as e:
            log(f"🔎 بحث '{term}' فشل:", e)
    for epic in EPICS:
        try:
            m = cap.call("GET", f"/markets/{epic}")
            inst, rules = m.get("instrument", {}), m.get("dealingRules", {})
            log(f"🔎 {epic}: type={inst.get('type')} lot={inst.get('lotSize')} margin={inst.get('marginFactor')}"
                f"{inst.get('marginFactorUnit', '')} minSize={rules.get('minDealSize')} guaranteed={inst.get('guaranteedStopAllowed')}")
        except Exception as e:
            log(f"🔎 {epic} فشل:", e)


def main():
    if not (API_KEY and IDENTIFIER and PASSWORD):
        raise SystemExit("أضف CAP_API_KEY و CAP_IDENTIFIER و CAP_PASSWORD في GitHub Secrets")
    if E("CAP_ENV") == "live" and not LIVE:
        log("⚠️ CAP_ENV=live لكن بدون LIVE_CONFIRM=I_UNDERSTAND — سأعمل على الحساب التجريبي")
    unlimited = MAX_OPEN <= 0 or (MAX_DAILY_LOSS_EUR <= 0 and MAX_DAILY_LOSS_PCT <= 0)
    if LIVE and unlimited and not LIVE_NO_LIMITS:
        raise SystemExit("⛔ الحساب الحقيقي بدون حدود (MAX_OPEN=0 أو بلا حد خسارة يومي) يحتاج تأكيداً صريحاً: "
                         "LIVE_NO_LIMITS: \"I_ACCEPT_UNLIMITED_RISK\" في bot.yml. أو ضع حدوداً.")
    log(f"الإعدادات: مخاطرة {RISK_PCT}%/صفقة (حتى {TRADE_RISK_EUR:.0f}€) | حد الخسارة اليومي "
        + (f"{MAX_DAILY_LOSS_EUR:.0f}€" if MAX_DAILY_LOSS_EUR > 0 else (f"{MAX_DAILY_LOSS_PCT}%" if MAX_DAILY_LOSS_PCT > 0 else "بلا حد"))
        + f" | أقصى صفقات {MAX_OPEN or 'بلا حد'} (حتى {MAX_NEW_PER_CYCLE} جديدة معاً) | مرتدات {'نعم' if REBOUND else 'لا'} "
        f"| أسهم {'نعم' if STOCKS else 'لا'} | أسواق {', '.join(EPICS)}")
    cap, state = Capital(), load_state()
    cap.login()
    if not state.get("discovered"):
        discover(cap)
        state["discovered"] = True
        save_state(state)
    end = time.time() + LOOP_MINUTES * 60
    while True:
        try:
            cycle(cap, state)
        except Exception as e:
            log("خطأ عام:", e)
        if time.time() + 60 > end:
            break
        time.sleep(60)
    save_state(state)
    log("انتهى هذا التشغيل")


if __name__ == "__main__":
    main()
