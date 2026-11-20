"""
روبوت الذهب (حساب تجريبي فقط): تحليل 3 فريمات (ساعة = الاتجاه، 15 دقيقة = الفرصة، 5 دقائق = الدخول) + الشموع + المستويات المدوّرة + الجلسات.
يعيد استخدام Capital وأدوات bot.py. لا يعمل على الحساب الحقيقي أبداً.
الاستراتيجيات: (1) ارتداد من مستوى  (2) كسر وإعادة اختبار  (3) تراجع داخل الاتجاه  (4) كسر كاذب لنطاق آسيا.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import bot
from bot import log

E = os.environ.get
EPIC = "GOLD"                                           # السوق الحالي (يتبدّل في الحلقة)
MARKETS = [x.strip() for x in E("MARKETS", "GOLD,OIL_CRUDE,SILVER,DOLLAR").split(",") if x.strip()]
CANDS = {"DOLLAR": ["DXY", "USDOLLAR", "EURUSD"], "OIL_CRUDE": ["OIL_CRUDE", "CRUDE_OIL"]}   # أسماء بديلة إذا الاسم الأول غير موجود
STEPS = {"GOLD": 50.0, "SILVER": 1.0, "OIL_CRUDE": 1.0, "DOLLAR": 0.5}                         # المستويات المدوّرة لكل سوق
LEVEL_STEP = 50.0
NAMES = {"GOLD": "الذهب", "SILVER": "الفضة", "OIL_CRUDE": "النفط", "DOLLAR": "الدولار"}
RISK_EUR = float(E("RISK_EUR", "50"))                 # الوقف = خسارة بهذا المبلغ (يورو)
TP_EUR = float(E("TP_EUR", "50"))                     # الهدف = ربح بهذا المبلغ (يورو)
MIN_TRADE_EUR = float(E("TRADE_EUR", "500"))          # أقل قيمة للصفقة
MAX_TRADE_EUR = float(E("MAX_TRADE_EUR", "8000"))     # أعلى قيمة للصفقة
MIN_OPEN = int(E("MIN_OPEN", "10"))                    # يحاول إبقاء هذا العدد من الصفقات مفتوحاً
G_MAX_OPEN = int(E("G_MAX_OPEN", "20"))               # سقف الصفقات المفتوحة (0 = بلا حد)
G_MAX_NEW = int(E("G_MAX_NEW", "5"))                  # أقصى صفقات جديدة بالدورة
MIN_SCORE = int(E("G_MIN_SCORE", "35"))
RELAX_SCORE = int(E("G_RELAX_SCORE", "20"))           # إذا الصفقات أقل من MIN_OPEN يقبل قوة أقل
RR = TP_EUR / RISK_EUR
NO_TP = E("NO_TP", "0") == "1"                         # 1 = بلا هدف ربح ثابت (الصفقة تكمل حتى يضربها الوقف المتحرّك)
LADDER_STEP = float(E("LADDER_STEP", "0"))             # كل كم يورو ربح يرتفع مستوى الوقف (0 = معطّل)
LADDER_LOCK = float(E("LADDER_LOCK", "0.6"))
def _tbl(x):
    out = []
    for part in x.split(","):
        if ":" in part:
            a, b = part.split(":")[:2]
            out.append((float(a), float(b)))
    return sorted(out)
LADDER_TABLE = _tbl(E("LADDER_TABLE", ""))               # مثال "3:2,5:3,7:5,10:7,12:9" = (الربح:الوقف المقفول بالربح)
LADDER_EXTRA_STEP = float(E("LADDER_EXTRA_STEP", "0"))   # بعد آخر درجة: كل زيادة بالربح بهذا المقدار
LADDER_EXTRA_LOCK = float(E("LADDER_EXTRA_LOCK", "0"))   # ... يرتفع الوقف المقفول بهذا المقدار           # نسبة الربح المقفلة: ربح 5 → وقف عند +3، 10 → +6، 15 → +9 ...
LEVEL_STEP_DEFAULT = 50.0
MARGIN_EUR = float(E("MARGIN_EUR", "0"))               # هامش كل صفقة (0 = معطّل): قيمة الصفقة = الهامش × الرافعة المتاحة للسوق
MARGIN_MIN = float(E("MARGIN_MIN", "0"))                # أقل هامش مقبول للصفقة (0 = بلا حد أدنى)
MARGIN_MAX = float(E("MARGIN_MAX", "0"))                # أعلى هامش مقبول (0 = MARGIN_EUR × 1.3)
REACH_ATR = float(E("REACH_ATR", "0"))                 # يتجاهل الصفقة إذا الهدف أبعد من هذا العدد من ATR 15 دقيقة (0 = معطّل)
NEWS_MODE = E("NEWS_MODE", "off")                      # off | boost (يرفع/يخفض القوة) | require (لا يدخل ضد الأخبار)
NEWS_MIN = float(E("NEWS_MIN", "0.15"))                # أقل قوة خبر تعتبر اتجاهاً
ALLOW_WEEKEND = E("ALLOW_WEEKEND", "0") == "1"           # السماح بالدخول السبت والأحد (الأسواق المفتوحة فقط، مثل الكريبتو)
MAX_PER_TYPE = int(E("MAX_PER_TYPE", "0"))             # أقصى صفقات من نفس النوع (أسهم/سلع/عملات/مؤشرات/كريبتو) لتنويع الصفقات (0 = معطّل)
PRIORITY = [x.strip() for x in E("PRIORITY", "").split(",") if x.strip()]   # أسواق تُحلَّل كل دورة وتأخذ أفضلية (مثل GOLD,OIL_CRUDE)
SPEED_BONUS = float(E("SPEED_BONUS", "0"))             # مكافأة للأسواق الأسرع حركة: قوة += min(15, ATR5% × هذا الرقم)
MAX_PER_EPIC = int(E("MAX_PER_EPIC", "0"))             # أقصى صفقات مفتوحة على نفس السوق (1 = صفقة واحدة لكل سهم/سوق)
MAX_HOLD_MIN = float(E("MAX_HOLD_MIN", "0"))           # أغلق الصفقة إذا مرّ هذا الوقت (0 = بلا إغلاق بالوقت)
DAILY_LOSS_EUR = float(E("DAILY_LOSS_EUR", "0"))       # بعد هذه الخسارة اليومية يتوقف عن فتح صفقات جديدة (0 = معطّل)
STREAK_N = int(E("STREAK_N", "0"))                     # بعد هذا العدد من الخسائر المتتالية يتوقف STREAK_PAUSE_MIN دقيقة (0 = معطّل)
STREAK_PAUSE_MIN = float(E("STREAK_PAUSE_MIN", "30"))
STOP_FILE = E("STOP_FILE", "STOP.txt")                 # إذا أنشأت ملف بهذا الاسم في المستودع يتوقف البوت عن فتح صفقات جديدة
MIN_ATR_PCT = float(E("MIN_ATR_PCT", "0"))             # فلتر السرعة: أقل ATR (5 دقائق) كنسبة من السعر
SPREAD_STOP_FRAC = float(E("SPREAD_STOP_FRAC", "0.4")) # السبريد يجب ألا يتجاوز هذه النسبة من مسافة الوقف
ONLY_EPICS = [x.strip() for x in E("ONLY_EPICS", "").split(",") if x.strip()]   # قائمة أسواق محددة (فارغ = كل الأسواق)
GH_REPO = E("GITHUB_REPOSITORY", "")
SCAN_PER_CYCLE = int(E("SCAN_PER_CYCLE", "24"))        # كم سوق يحلل بكل دورة (يدور على كل الأسواق)
SPREAD_MAX_PCT = float(E("SPREAD_MAX_PCT", "0.4"))     # يتجاهل الأسواق التي سبريدها أكبر من هذه النسبة
DISP = dict(NAMES)                                     # رمز السوق -> اسم للعرض
FX_CACHE = {}
BE_AT_R = float(E("BE_AT_R", "0"))                    # 0 = بلا تأمين: الهدف والوقف ثابتان 50/50
SESSION_FROM, SESSION_TO = (int(x) for x in E("SESSION_GMT", "0-24").split("-"))
BEST_FROM, BEST_TO = (int(x) for x in E("BEST_GMT", "13-17").split("-"))
NEWS_BLACKOUT = [x.strip() for x in E("NEWS_BLACKOUT", "13:25-13:40,14:55-15:10").split(",") if x.strip()]
COOLDOWN_MIN = float(E("G_COOLDOWN_MIN", "5"))
LOOP_MINUTES = float(E("LOOP_MINUTES", "14"))
CYCLE_SECONDS = float(E("CYCLE_SECONDS", "20"))
GOLD_BRANCH = E("GOLD_BRANCH", "gold-data")
STATE_FILE = E("STATE_FILE", "gold_state.json")
DRY_RUN = E("DRY_RUN", "0") == "1"
ACCOUNT_NAME = E("ACCOUNT_NAME", "").strip()            # اسم الحساب داخل كابيتال (مثل "Account 2") — فارغ = الحساب المختار حالياً
ACC_ID = [None]
def _sb(x):
    out = {}
    for part in x.split(","):
        if ":" in part:
            k, v = part.split(":")[:2]
            out[k.strip()] = float(v)
    return out
STRAT_BONUS = _sb(E("STRAT_BONUS", ""))                  # مثال "bounce:20,retest:15,pullback:15,trend:-10" = تفضيل المرتدات
STRAT = {"trend": "اتجاه مؤكد", "bounce": "ارتداد من مستوى", "retest": "كسر وإعادة اختبار", "pullback": "تراجع داخل الاتجاه", "asia": "كسر كاذب لنطاق آسيا"}


# ===== بيانات =====
def parse_t(s):
    d = datetime.fromisoformat(str(s).replace("Z", ""))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def candles(cap, res, n):
    d = cap.call("GET", f"/prices/{EPIC}?resolution={res}&max={n}")
    out = []
    for p in d.get("prices", []):
        out.append({"t": parse_t(p.get("snapshotTimeUTC") or p.get("snapshotTime")), "o": bot.mid(p["openPrice"]),
                    "h": bot.mid(p["highPrice"]), "l": bot.mid(p["lowPrice"]), "c": bot.mid(p["closePrice"])})
    return out[:-1]            # آخر شمعة غير مغلقة: نتجاهلها


def prep(cs):
    c = [x["c"] for x in cs]
    return {"cs": cs, "c": c, "e9": bot.ema(c, 9), "e20": bot.ema(c, 20), "e21": bot.ema(c, 21), "e50": bot.ema(c, 50),
            "r": bot.rsi(c), "a": bot.atr(cs)}


# ===== الاتجاه والشموع =====
def trend_h1(P):
    i = len(P["c"]) - 1
    if i < 60:
        return 0
    c, e20, e50 = P["c"][i], P["e20"][i], P["e50"][i]
    if c > e50 and e20 > e50 and e50 > P["e50"][i - 3]:
        return 1
    if c < e50 and e20 < e50 and e50 < P["e50"][i - 3]:
        return -1
    return 0


def dir_m15(P):
    i = len(P["c"]) - 1
    if i < 30:
        return 0
    if P["e9"][i] > P["e21"][i] and 40 < P["r"][i] < 82:
        return 1
    if P["e9"][i] < P["e21"][i] and 18 < P["r"][i] < 60:
        return -1
    return 0


def pattern(cs, i, atr):
    """نموذج الشمعة المغلقة رقم i: (اتجاه، اسم) أو None"""
    if i < 1 or not atr:
        return None
    c, p = cs[i], cs[i - 1]
    rng = c["h"] - c["l"]
    if rng <= 0:
        return None
    body = abs(c["c"] - c["o"])
    up, lo = c["h"] - max(c["o"], c["c"]), min(c["o"], c["c"]) - c["l"]
    if c["c"] > c["o"] and p["c"] < p["o"] and c["c"] >= p["o"] and c["o"] <= p["c"] and body >= 0.5 * atr:
        return 1, "ابتلاعية صاعدة"
    if c["c"] < c["o"] and p["c"] > p["o"] and c["c"] <= p["o"] and c["o"] >= p["c"] and body >= 0.5 * atr:
        return -1, "ابتلاعية هابطة"
    if rng >= 0.5 * atr and lo >= 2 * body and lo >= 0.55 * rng and c["c"] >= c["l"] + 0.6 * rng:
        return 1, "شمعة رفض سفلية (مطرقة)"
    if rng >= 0.5 * atr and up >= 2 * body and up >= 0.55 * rng and c["c"] <= c["h"] - 0.6 * rng:
        return -1, "شمعة رفض علوية"
    if body >= 0.6 * rng and body >= 0.8 * atr:
        return (1 if c["c"] > c["o"] else -1), "شمعة اندفاع"
    return None


def nearest_level(price):
    return round(price / LEVEL_STEP) * LEVEL_STEP


# ===== الاستراتيجيات: كل واحدة ترجع (إشارة | None، سبب الانتظار) =====
def sig(strategy, d, sl, why, pat=None, level=None, bonus=0):
    return {"strategy": strategy, "dir": d, "sl": sl, "why": why, "pattern": pat, "level": level, "bonus": bonus}


def s_pullback(H, M, S, h1, m15):
    """تراجع لمتوسط 21 على 5 دقائق داخل اتجاه الساعة ثم شمعة تأكيد"""
    i = len(S["c"]) - 1
    if i < 40:
        return None, "بيانات 5 دقائق غير كافية"
    if h1 == 0 or m15 != h1:
        return None, "الساعة و15 دقيقة غير متفقتين على اتجاه"
    d, cs, a = h1, S["cs"], S["a"][i]
    if not a:
        return None, "لا ATR"
    touched = any((cs[i - k]["l"] <= S["e21"][i - k]) if d > 0 else (cs[i - k]["h"] >= S["e21"][i - k]) for k in (1, 2, 3, 4))
    b = cs[i]
    ok = (b["c"] > S["e9"][i] and b["c"] > b["o"] and S["r"][i] > 50) if d > 0 else (b["c"] < S["e9"][i] and b["c"] < b["o"] and S["r"][i] < 50)
    if not touched:
        return None, "ما في تراجع لمتوسط 21 بعد"
    if not ok:
        return None, "بانتظار شمعة تأكيد " + ("صاعدة" if d > 0 else "هابطة")
    look = cs[i - 5:i + 1]
    sl = min(x["l"] for x in look) - 0.2 * a if d > 0 else max(x["h"] for x in look) + 0.2 * a
    p = pattern(cs, i, a)
    return sig("pullback", d, sl, "تراجع إلى متوسط 21 ثم شمعة تأكيد على 5 دقائق", p[1] if p and p[0] == d else None), ""


def s_bounce(H, M, S, h1, m15):
    """شمعة رفض/ابتلاع على 15 دقيقة عند مستوى مدوّر، مع الاتجاه"""
    i = len(M["c"]) - 1
    if i < 30:
        return None, "بيانات 15 دقيقة غير كافية"
    a, cs = M["a"][i], M["cs"]
    p = pattern(cs, i, a)
    if not p:
        return None, "لا شمعة رفض أو ابتلاع على 15 دقيقة"
    d, name = p
    if name == "شمعة اندفاع":
        return None, "شمعة اندفاع ليست ارتداداً"
    if h1 == -d:
        return None, "ضد اتجاه الساعة"
    c = cs[i]
    lv = nearest_level(c["l"] if d > 0 else c["h"])
    near = abs((c["l"] if d > 0 else c["h"]) - lv) <= 0.3 * a
    held = (c["c"] > lv) if d > 0 else (c["c"] < lv)
    if not (near and held):
        return None, f"الشمعة ليست عند مستوى مدوّر قريب ({lv:.0f})"
    sl = (c["l"] - 0.3 * a) if d > 0 else (c["h"] + 0.3 * a)
    return sig("bounce", d, sl, f"رفض المستوى {lv:.0f} بـ{name} على 15 دقيقة", name, lv, 5), ""


def s_retest(H, M, S, h1, m15):
    """كسر مستوى مدوّر بإغلاق 15 دقيقة ثم عودة لاختباره وثبات + شمعة تأكيد"""
    n = len(M["c"])
    i = n - 1
    if i < 30:
        return None, "بيانات 15 دقيقة غير كافية"
    a, cs = M["a"][i], M["cs"]
    if not a:
        return None, "لا ATR"
    for d in (1, -1):
        if h1 == -d:
            continue
        for k in range(i - 2, max(i - 13, 2), -1):                   # شمعة الكسر
            for lv in {nearest_level(cs[k]["c"]), nearest_level(cs[k - 1]["c"])}:
                broke = (cs[k]["c"] > lv + 0.15 * a and cs[k - 1]["c"] <= lv) if d > 0 else (cs[k]["c"] < lv - 0.15 * a and cs[k - 1]["c"] >= lv)
                if not broke:
                    continue
                after = cs[k + 1:i + 1]
                if any((x["c"] < lv) if d > 0 else (x["c"] > lv) for x in after):
                    continue                                          # رجع وأغلق تحته: فشل الكسر
                retested = any((x["l"] <= lv + 0.25 * a) if d > 0 else (x["h"] >= lv - 0.25 * a) for x in after[:-1] or after)
                last = cs[i]
                confirm = (last["c"] > last["o"] and last["c"] > cs[i - 1]["h"]) if d > 0 else (last["c"] < last["o"] and last["c"] < cs[i - 1]["l"])
                if retested and confirm:
                    ext = min(x["l"] for x in after) if d > 0 else max(x["h"] for x in after)
                    sl = min(lv, ext) - 0.5 * a if d > 0 else max(lv, ext) + 0.5 * a
                    return sig("retest", d, sl, f"كسر {lv:.0f} بإغلاق 15 دقيقة ثم إعادة اختبار ناجحة", None, lv, 5), ""
    return None, "ما في كسر مستوى مع إعادة اختبار"


def s_asia(H, M, S, h1, m15, now):
    """كسر كاذب لنطاق آسيا (00:00-08:00 غرينتش): اختراق ثم إغلاق داخل النطاق = عكس الاتجاه"""
    if not (8 <= now.hour < 13):
        return None, "خارج نافذة لندن الصباحية (08-13 غرينتش)"
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    asia = [x for x in M["cs"] if day <= x["t"] < day + timedelta(hours=8)]
    if len(asia) < 20:
        return None, "نطاق آسيا غير مكتمل"
    hi, lo = max(x["h"] for x in asia), min(x["l"] for x in asia)
    after = [x for x in M["cs"] if x["t"] >= day + timedelta(hours=8)]
    if len(after) < 2:
        return None, "لندن لم تبدأ بعد"
    a = M["a"][len(M["c"]) - 1]
    last = after[-1]
    swept_up = any(x["h"] > hi for x in after[-6:])
    swept_dn = any(x["l"] < lo for x in after[-6:])
    if swept_up and last["c"] < hi and last["c"] < last["o"]:
        ext = max(x["h"] for x in after[-6:])
        return sig("asia", -1, ext + 0.2 * a, f"اختراق كاذب لأعلى نطاق آسيا ({hi:.1f}) ثم إغلاق داخله", None, hi, 0), ""
    if swept_dn and last["c"] > lo and last["c"] > last["o"]:
        ext = min(x["l"] for x in after[-6:])
        return sig("asia", 1, ext - 0.2 * a, f"اختراق كاذب لأسفل نطاق آسيا ({lo:.1f}) ثم إغلاق داخله", None, lo, 0), ""
    return None, f"نطاق آسيا {lo:.1f}–{hi:.1f}: لا كسر كاذب"


def s_trend(H, M, S, h1, m15):
    """اتجاه الساعة و15 دقيقة متفقان + 5 دقائق بنفس الاتجاه وشمعة بنفس الاتجاه: دخول مباشر بلا تردد"""
    i = len(S["c"]) - 1
    if i < 40 or not S["a"][i]:
        return None, "بيانات 5 دقائق غير كافية"
    d = h1 if h1 else m15
    if d == 0 or (m15 and m15 != d):
        return None, "لا اتجاه واضح على الساعة/15 دقيقة"
    cs, a = S["cs"], S["a"][i]
    aligned = S["e9"][i] > S["e21"][i] if d > 0 else S["e9"][i] < S["e21"][i]
    candle = cs[i]["c"] > cs[i]["o"] if d > 0 else cs[i]["c"] < cs[i]["o"]
    relaxed = MIN_SCORE_NOW[0] < MIN_SCORE            # أقل من العدد المطلوب من الصفقات: لا تردد، ادخل مع الاتجاه
    if not relaxed and (not aligned or not candle):
        return None, "5 دقائق لا تؤكد الاتجاه بعد"
    if not relaxed and ((d > 0 and S["r"][i] > 88) or (d < 0 and S["r"][i] < 12)):
        return None, "السعر متمدد (RSI متطرف)"
    look = cs[i - 5:i + 1]
    sl = min(x["l"] for x in look) - 0.2 * a if d > 0 else max(x["h"] for x in look) + 0.2 * a
    return sig("trend", d, sl, "الاتجاه متفق على الفريمات" + ("" if not relaxed else " (دخول مباشر: الصفقات أقل من المطلوب)"), None, None, 0), ""


# ===== الجلسة والأخبار =====
def session_info(now):
    h, wd = now.hour, now.weekday()
    mins = h * 60 + now.minute
    blackout = None
    if wd < 5:
        for w in NEWS_BLACKOUT:
            a, b = w.split("-")
            ah, am = (int(x) for x in a.split(":")); bh, bm = (int(x) for x in b.split(":"))
            if ah * 60 + am <= mins < bh * 60 + bm:
                blackout = w
    if wd >= 5 and not ALLOW_WEEKEND:
        q, ok = "إغلاق نهاية الأسبوع", False
    elif BEST_FROM <= h < BEST_TO:
        q, ok = "ممتازة (تداخل لندن ونيويورك)", True
    elif SESSION_FROM <= h < SESSION_TO:
        q, ok = "جيدة", True
    else:
        q, ok = "هادئة (خارج ساعات الدخول)", False
    return {"quality": q, "ok": ok and not blackout, "blackout": blackout, "best": BEST_FROM <= h < BEST_TO and wd < 5}


# ===== التقييم =====
MIN_SCORE_NOW = [MIN_SCORE]


def evaluate(H, M, S, now):
    h1, m15 = trend_h1(H), dir_m15(M)
    sess = session_info(now)
    out, status = [], []
    for key, fn in (("trend", s_trend), ("bounce", s_bounce), ("retest", s_retest), ("pullback", s_pullback), ("asia", None)):
        s, why = (s_asia(H, M, S, h1, m15, now) if key == "asia" else fn(H, M, S, h1, m15))
        if s:
            score = 50 + (15 if h1 == s["dir"] else 0 if h1 == 0 else -30) + (10 if m15 == s["dir"] else 0) + (10 if s["pattern"] else 0) + s["bonus"] + (10 if sess["best"] else 0)
            i15 = len(M["c"]) - 1
            if (s["dir"] > 0 and M["r"][i15] > 75) or (s["dir"] < 0 and M["r"][i15] < 25):
                score -= 10
            score += STRAT_BONUS.get(key, 0)
            s["score"] = score
            if score < MIN_SCORE_NOW[0]:
                status.append({"id": key, "name": STRAT[key], "state": "weak", "text": f"إشارة {'شراء' if s['dir'] > 0 else 'بيع'} لكن القوة {score} < {MIN_SCORE_NOW[0]}: {s['why']}"})
            else:
                status.append({"id": key, "name": STRAT[key], "state": "signal", "text": f"{'شراء' if s['dir'] > 0 else 'بيع'} ({score}): {s['why']}"})
                out.append(s)
        else:
            status.append({"id": key, "name": STRAT[key], "state": "wait", "text": why})
    return {"h1": h1, "m15": m15, "sess": sess, "signals": sorted(out, key=lambda x: -x["score"]), "status": status}


def tf_view(P, name):
    i = len(P["c"]) - 1
    if i < 0:
        return {"name": name}
    pat = pattern(P["cs"], i, P["a"][i]) if P["a"][i] else None
    return {"name": name, "close": round(P["c"][i], 2), "ema9": round(P["e9"][i], 2), "ema21": round(P["e21"][i], 2), "ema50": round(P["e50"][i], 2),
            "rsi": round(P["r"][i]), "atr": round(P["a"][i] or 0, 2), "pattern": pat[1] if pat else None, "pattern_dir": pat[0] if pat else 0,
            "t": P["cs"][i]["t"].strftime("%H:%M")}




# ===== التنفيذ =====
def resolve_markets(cap):
    """يحوّل الأسماء (الذهب/النفط/الفضة/الدولار) لرموز كابيتال الفعلية"""
    out = {}
    for name in MARKETS:
        for ep in CANDS.get(name, [name]):
            try:
                m = cap.call("GET", f"/markets/{ep}")
                if m.get("snapshot"):
                    out[name] = ep
                    log(f"✓ {DISP.get(name, name)} = {ep}")
                    break
            except Exception:
                continue
        else:
            log(f"⚠️ ما لقيت سوق لـ {name} (جرّبت {CANDS.get(name, [name])}) — تخطي")
    return out


def eurusd(cap):
    try:
        s = cap.call("GET", "/markets/EURUSD").get("snapshot", {})
        return (float(s["bid"]) + float(s["offer"])) / 2
    except Exception as e:
        log("⚠️ تعذّر سعر EURUSD، أفترض 1.08:", e)
        return 1.08


def fx_for(cap, cur):
    """كم وحدة من عملة السوق = 1 يورو (None إذا غير معروف)"""
    cur = (cur or "USD").upper()
    if cur == "EUR":
        return 1.0
    c = FX_CACHE.get(cur)
    if c and time.time() - c[0] < 600:
        return c[1]
    v = None
    try:
        if cur == "USD":
            v = eurusd(cap)
        else:
            sn = cap.call("GET", f"/markets/EUR{cur}").get("snapshot", {})
            v = (float(sn["bid"]) + float(sn["offer"])) / 2
    except Exception:
        v = None
    FX_CACHE[cur] = (time.time(), v)
    return v


def open_trade(cap, state, s, now, avail=None):
    m = cap.call("GET", f"/markets/{EPIC}")
    snap, rules, inst = m.get("snapshot", {}), m.get("dealingRules", {}), m.get("instrument", {})
    if snap.get("marketStatus") != "TRADEABLE":
        log(f"{EPIC}: السوق مغلق"); return None
    fx = fx_for(cap, inst.get("currency") or ((inst.get("currencies") or [{}])[0].get("code")))
    if not fx:
        log(f"{EPIC}: عملة السوق غير مدعومة — تخطي"); return None
    buy = s["dir"] > 0
    bid, ask = float(snap["bid"]), float(snap["offer"])
    price = ask if buy else bid
    spread = ask - bid
    lot = float(inst.get("lotSize") or 1)
    min_size = float(rules["minDealSize"]["value"])
    step = float((rules.get("minSizeIncrement") or {}).get("value") or min_size)
    # قيمة الصفقة من بُعد الوقف الفني (مقيّد 0.25%–1%) بحيث تكون خسارة الوقف ≈ RISK_EUR
    d0 = min(max(abs(price - s["sl"]), 0.0025 * price), 0.01 * price)
    mf = float(inst.get("marginFactor") or 0)                     # نسبة الهامش % (5 = رافعة 20:1)
    if MARGIN_EUR and mf > 0:
        notional = min(MARGIN_EUR * 100 / mf, MAX_TRADE_EUR)      # أكبر رافعة يسمح بها السوق على هذا الهامش
    else:
        notional = min(max(RISK_EUR / (d0 / price), MIN_TRADE_EUR), MAX_TRADE_EUR)
    size = round(round(notional * fx / (price * lot) / step) * step, 6)
    size = max(size, min_size)
    size = min(size, float((rules.get("maxDealSize") or {}).get("value") or 1e12))
    value = size * price * lot / fx
    if MARGIN_EUR and mf > 0:
        margin = value * mf / 100
        if margin > (MARGIN_MAX or MARGIN_EUR * 1.3) or margin < MARGIN_MIN:
            log(f"{EPIC}: هامش الصفقة {margin:.0f} € خارج المجال المطلوب ({MARGIN_MIN:.0f}–{MARGIN_MAX or MARGIN_EUR * 1.3:.0f}) — تخطي"); return None
    if value < MIN_TRADE_EUR * 0.9 or value > MAX_TRADE_EUR * 1.5:
        log(f"{EPIC}: قيمة الصفقة {value:.0f} € خارج الحدود (أصغر حجم {min_size}) — تخطي"); return None
    need = value * mf / 100 if (MARGIN_EUR and mf > 0) else value * 0.25     # الهامش المطلوب فعلياً
    if avail is not None and avail < need:
        log(f"{EPIC}: الرصيد المتاح {avail:.0f} قليل لصفقة {value:.0f} € (هامش {need:.0f} €) — تخطي"); return None
    dist = RISK_EUR * fx / (size * lot)                          # مسافة تعطي خسارة = RISK_EUR بالضبط
    tdist = TP_EUR * fx / (size * lot)
    mind = bot.min_dist(rules, "minStopOrProfitDistance", price) * 1.1
    if REACH_ATR and s.get("atr") and tdist > REACH_ATR * s["atr"]:
        log(f"{EPIC}: الهدف {tdist:.3f} بعيد (>{REACH_ATR:g}×ATR={s['atr']:.3f}) — تخطي"); return None
    if tdist < spread * 2:
        log(f"{EPIC}: الهدف أقرب من ضعف السبريد — تخطي"); return None
    if spread > SPREAD_STOP_FRAC * dist:
        log(f"{EPIC}: السبريد {spread:.4f} كبير مقارنة بالوقف {dist:.4f} — تخطي"); return None
    if dist < max(spread * 2.5, mind):
        log(f"{EPIC}: وقف {RISK_EUR:.0f} € = {dist:.3f} أضيق من السبريد/الحد الأدنى — تخطي"); return None
    dec = int(snap.get("decimalPlacesFactor") or 2)
    stop = round(price - dist if buy else price + dist, dec)
    target = round(price + tdist if buy else price - tdist, dec)
    log(f"{EPIC}: {'🟢 شراء' if buy else '🔴 بيع'} [{STRAT[s['strategy']]}] حجم {size} (≈{value:.0f} €) @ {price} | وقف {stop} (-{RISK_EUR:.0f}€) | {('هدف ' + str(target) + ' (+' + format(TP_EUR, '.0f') + '€)') if not NO_TP else 'بلا هدف: وقف متحرّك'} | {s['why']}")
    if DRY_RUN:
        return {"id": "dry", "need": need, "size": size, "price": price, "stop": stop, "target": None if NO_TP else target, "value": value, "risk": RISK_EUR, "fx": fx, "lot": lot}
    order = {"epic": EPIC, "direction": "BUY" if buy else "SELL", "size": size, "stopLevel": stop, "guaranteedStop": False}
    if not NO_TP:
        order["profitLevel"] = target
    if not on_my_account(cap):
        log(f"{EPIC}: ⛔ الجلسة مش على حساب {ACCOUNT_NAME} — ما فتحت الصفقة"); return None
    try:
        ref = cap.call("POST", "/positions", order).get("dealReference")
        time.sleep(1)
        c = cap.call("GET", f"/confirms/{ref}")
    except Exception as e:
        log(f"{EPIC}: رفض الطلب:", e); return None
    log(f"{EPIC}: {c.get('dealStatus')} {c.get('reason', '')}")
    if c.get("dealStatus") != "ACCEPTED":
        return None
    ids = [c.get("dealId")] + [a.get("dealId") for a in c.get("affectedDeals", []) or []]
    ids = [i for i in dict.fromkeys(ids) if i]
    return {"id": ids[0] if ids else ref, "ids": ids, "need": need, "size": size, "price": price, "stop": stop, "target": None if NO_TP else target, "value": value, "risk": RISK_EUR, "fx": fx, "lot": lot}


def event(state, **kw):
    ev = {"t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **kw}
    state["events"] = (state.get("events", []) + [ev])[-150:]


def account(state, pnl):
    """يحسب ربح/خسارة اليوم والخسائر المتتالية"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = state.setdefault("day", {})
    if d.get("date") != today:
        d.clear(); d["date"] = today; d["pnl"] = 0.0
    d["pnl"] = round(d["pnl"] + pnl, 2)
    if pnl < 0:
        state["streak"] = state.get("streak", 0) + 1
        if STREAK_N and state["streak"] >= STREAK_N:
            state["pause_until"] = time.time() + STREAK_PAUSE_MIN * 60
            state["streak"] = 0
    elif pnl > 0:
        state["streak"] = 0


def gate(cap, state):
    """هل مسموح فتح صفقات جديدة الآن؟ يرجع سبب المنع أو None"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = state.get("day") or {}
    if DAILY_LOSS_EUR and d.get("date") == today and d.get("pnl", 0) <= -DAILY_LOSS_EUR:
        return f"وصلت الخسارة اليومية {d['pnl']:.0f} € (الحد {DAILY_LOSS_EUR:.0f}) — متوقف لباقي اليوم"
    if state.get("pause_until", 0) > time.time():
        return f"{STREAK_N} خسائر متتالية — استراحة حتى {datetime.fromtimestamp(state['pause_until'], timezone.utc).strftime('%H:%M')} غرينتش"
    if GH_REPO and STOP_FILE:
        import urllib.request
        try:
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/{GH_REPO}/main/{STOP_FILE}?t={int(time.time())}", timeout=10) as r:
                if r.status == 200:
                    return f"زر الإيقاف: وُجد ملف {STOP_FILE} في المستودع"
        except Exception:
            pass                                          # 404 = لا يوجد ملف إيقاف
    return None


def reconcile(state, allpos):
    """يربط صفقات البوت بصفقات الحساب الحقيقية حتى لو اختلف رقم الصفقة (dealId) بين الطلب والقائمة"""
    trades = state.setdefault("trades", {})
    live = {p["position"]["dealId"]: p for p in allpos}
    taken = set(live) & set(trades)
    for did, tr in list(trades.items()):
        if did in live:
            continue
        match = next((i for i in (tr.get("ids") or []) if i in live and i not in taken), None)
        if not match:                                     # احتياطي: نفس السوق والاتجاه والحجم وسعر الدخول القريب
            for i, p in live.items():
                pos = p["position"]
                if i in taken or p["market"]["epic"] != tr.get("epic") or pos.get("direction") != tr["dir"]:
                    continue
                try:
                    if abs(float(pos["size"]) - float(tr.get("size0") or tr.get("size") or 0)) < 1e-9 and abs(float(pos["level"]) - tr["entry"]) <= 0.003 * tr["entry"]:
                        match = i; break
                except Exception:
                    continue
        if match:
            log(f"🔗 ربطت صفقة {tr.get('name')} برقم الحساب {match} (كان {did})")
            trades[match] = trades.pop(did)
            taken.add(match)


def lad_calc(pnl):
    """يرجع (رقم الدرجة، الربح المقفول بالوقف) لهذا الربح الحالي"""
    if LADDER_TABLE:
        n, lock = 0, 0.0
        for i, (p, l) in enumerate(LADDER_TABLE):
            if pnl >= p:
                n, lock = i + 1, l
        if n == len(LADDER_TABLE) and LADDER_EXTRA_STEP > 0:
            k = int((pnl - LADDER_TABLE[-1][0]) // LADDER_EXTRA_STEP)
            n, lock = n + k, lock + k * LADDER_EXTRA_LOCK
        return n, lock
    if LADDER_STEP > 0:
        n = int(pnl // LADDER_STEP)
        return n, n * LADDER_STEP * LADDER_LOCK
    return 0, 0.0


def ladder(cap, state, tr, pos, mk, buy, now_p, lvl):
    """سلّم الوقف: ربح n×STEP → الوقف يُقفل عند n×STEP×LOCK (5→3، 10→6، 15→9 ...) ولا يتراجع أبداً"""
    fx = tr.get("fx") or 1.08
    size = float(pos["size"])
    lot = tr.get("lot")
    if lot:   # الربح من حركة السعر نفسها (لا يعتمد على عملة/وحدة حقل upl)
        pnl = ((now_p - lvl) if buy else (lvl - now_p)) * size * lot / fx
    else:
        pnl = float(pos.get("upl") or 0) / fx
    tr["pnl_eur"] = round(pnl, 2)
    n, lock = lad_calc(pnl)
    if n < 1 or n <= tr.get("lad", 0):
        return
    if not lot:
        gain = (now_p - lvl) if buy else (lvl - now_p)
        if gain <= 0 or not float(pos.get("upl") or 0): return
        lot = float(pos["upl"]) / (gain * size)
    dist = lock * fx / (size * lot)
    want = lvl + dist if buy else lvl - dist
    m = cap.call("GET", f"/markets/{tr['epic']}")
    mind = bot.min_dist(m.get("dealingRules", {}), "minStopOrProfitDistance", now_p) * 1.1
    limit = now_p - mind if buy else now_p + mind          # أقرب مستوى مسموح للوقف من السعر الحالي
    new = min(want, limit) if buy else max(want, limit)
    txt = str(mk.get("bid"))
    dec = max(len(txt.split(".")[1]) if "." in txt else 0, 2)
    new = round(new, dec)
    cur = pos.get("stopLevel")
    tick = 10 ** -dec
    if cur is not None and ((buy and new <= float(cur) + tick / 2) or ((not buy) and new >= float(cur) - tick / 2)):
        return                                              # لا تحسّن عن الوقف الحالي
    if not DRY_RUN:
        body = {"stopLevel": new}
        if pos.get("profitLevel") is not None:
            body["profitLevel"] = float(pos["profitLevel"])
        cap.call("PUT", f"/positions/{pos['dealId']}", body)
    reached = (new >= round(want, dec) - tick / 2) if buy else (new <= round(want, dec) + tick / 2)
    if reached:
        tr["lad"] = n
    event(state, type="secure", market=tr.get("name"), dir=pos["direction"], price=new,
          why=f"الربح ≈ +{pnl:.1f}€ → الوقف عند ربح +{lock:.0f}€" + ("" if reached else " (أقرب مستوى مسموح الآن، سيُحدَّث لاحقاً)"))
    log(f"{tr.get('name')}: 🔒 الربح ≈ {pnl:+.1f}€ → وقف عند {new} (يضمن ≈ +{lock:.0f}€){'' if reached else ' [مؤقت]'}")


def manage(cap, state, positions):
    """يتتبّع صفقاتنا ويسجّل الإغلاقات؛ التأمين اختياري (BE_AT_R>0)"""
    trades = state.setdefault("trades", {})
    live = {p["position"]["dealId"]: p for p in positions}
    for did, tr in list(trades.items()):
        p = live.get(did)
        if not p:
            last = tr.get("last_price") or tr["entry"]
            known = tr.get("upl") is not None
            pnl = round((tr.get("upl") or 0) / (tr.get("fx") or 1.08), 2)       # آخر ربح/خسارة مسجّلة (تقريبية) — وليس رقماً مفترضاً
            hit_tp = pnl >= TP_EUR * 0.5
            if tr.get("forced") is not None:
                pnl = tr["forced"]; hit_tp = pnl > 0
            if not known:
                log(f"⚠️ {tr.get('name')}: اختفت الصفقة قبل أول قراءة. المعرّفات المتتبَّعة {tr.get('ids')} — راجع سجل كابيتال للنتيجة الفعلية")
            account(state, pnl)
            event(state, type="close", market=tr.get("name"), dir=tr["dir"], strategy=STRAT.get(tr["strategy"]), price=last, pnl=round(pnl, 2),
                  why="⏱ أُغلقت بالوقت" if tr.get("forced") is not None else ("أُغلقت — النتيجة غير معروفة بدقة، راجع كابيتال" if not known else ("أُغلقت بربح (≈ آخر قراءة) ✅" if hit_tp else "أُغلقت بخسارة (≈ آخر قراءة)")))
            log(f"{tr.get('name')}: أُغلقت صفقة {tr['dir']} ≈ {pnl:+.0f} €")
            del trades[did]
            continue
        pos, mk = p["position"], p["market"]
        buy = pos["direction"] == "BUY"
        now_p = float(mk["bid"] if buy else mk["offer"])
        lvl = float(pos["level"])
        tr["last_price"], tr["upl"] = now_p, float(pos.get("upl") or 0)
        if MAX_HOLD_MIN and tr.get("t"):
            age = (datetime.now(timezone.utc) - parse_t(tr["t"])).total_seconds() / 60
            if age >= MAX_HOLD_MIN and not DRY_RUN:
                try:
                    cap.call("DELETE", f"/positions/{pos['dealId']}")
                    tr["forced"] = round(tr["upl"] / (tr.get("fx") or 1.08), 2)
                    log(f"{tr.get('name')}: ⏱ أُغلقت بعد {age:.0f} د ({tr['forced']:+.2f} €)")
                    continue
                except Exception as e:
                    log("تعذّر الإغلاق بالوقت:", str(e)[:100])
        if BE_AT_R and not tr.get("be") and pos.get("stopLevel") is not None:
            dist = abs(lvl - tr["sl"])
            gain = (now_p - lvl) if buy else (lvl - now_p)
            if dist > 0 and gain >= BE_AT_R * dist:
                spread = abs(float(mk["offer"]) - float(mk["bid"]))
                new_stop = round(lvl + spread if buy else lvl - spread, 5)
                if not DRY_RUN:
                    body = {"stopLevel": new_stop}
                    if pos.get("profitLevel") is not None:
                        body["profitLevel"] = float(pos["profitLevel"])
                    try:
                        cap.call("PUT", f"/positions/{pos['dealId']}", body)
                        tr["be"] = True
                    except Exception as e:
                        log("تعذّر التأمين:", e); continue
                event(state, type="secure", market=tr.get("name"), dir=pos["direction"], price=new_stop, why="نقل الوقف لسعر الدخول")
        if LADDER_STEP > 0 or LADDER_TABLE:
            try:
                ladder(cap, state, tr, pos, mk, buy, now_p, lvl)
            except Exception as e:
                log("تعذّر تحريك الوقف:", str(e)[:200])
                event(state, type="warn", market=tr.get("name"), dir=pos["direction"], price=now_p, why="تعذّر رفع الوقف: " + str(e)[:160])


def view_open(state, positions, fx):
    out = []
    trades = state.get("trades", {})
    for p in positions:
        pos, mk = p["position"], p["market"]
        tr = trades.get(pos["dealId"])
        if not tr:
            continue
        buy = pos["direction"] == "BUY"
        out.append({"id": pos["dealId"], "market": tr.get("name"), "dir": pos["direction"], "size": float(pos["size"]), "entry": float(pos["level"]),
                    "price": float(mk["bid"] if buy else mk["offer"]), "stop": float(pos["stopLevel"]) if pos.get("stopLevel") is not None else None,
                    "target": float(pos["profitLevel"]) if pos.get("profitLevel") is not None else None, "pnl": round(float(pos.get("upl") or 0), 2),
                    "strategy": STRAT.get(tr["strategy"], tr["strategy"]), "why": tr.get("why"), "t": tr.get("t"), "be": bool(tr.get("be")),
                    "value_eur": round(float(pos["size"]) * float(pos["level"]) / (tr.get("fx") or fx))})
    return out


def auto_step(price):
    import math
    return 0.5 * 10 ** (math.floor(math.log10(max(price, 1e-9))) - 1)


def analyze_market(cap, name, ep, now, price0=None):
    global EPIC, LEVEL_STEP
    EPIC, LEVEL_STEP = ep, STEPS.get(name) or (auto_step(price0) if price0 else LEVEL_STEP_DEFAULT)
    H, M, S = prep(candles(cap, "HOUR", 200)), prep(candles(cap, "MINUTE_15", 200)), prep(candles(cap, "MINUTE_5", 150))
    ev = evaluate(H, M, S, now)
    ev.update(name=name, ep=ep, H=H, M=M, S=S, price=S["c"][-1], step=LEVEL_STEP)
    return ev


def pick_markets(cap, state, ctx):
    """يدور على كل الأسواق القابلة للتداول: كل دورة يحلل دفعة، ويتجاهل المغلق وذا السبريد الكبير. (اسم، رمز، سعر)"""
    uni = ctx["uni"]
    order = ctx.setdefault("order", sorted(uni, key=lambda e: (uni[e]["t"] == "SHARES", e)))
    ptr = state.get("ptr", 0) % max(len(order), 1)
    chunk = [order[(ptr + i) % len(order)] for i in range(min(SCAN_PER_CYCLE, len(order)))]
    state["ptr"] = (ptr + len(chunk)) % max(len(order), 1)
    chunk = [e for e in PRIORITY if e in uni] + [e for e in chunk if e not in PRIORITY]
    state["scan_total"] = len(order)
    out = []
    for i in range(0, len(chunk), 50):
        part = chunk[i:i + 50]
        try:
            det = cap.call("GET", "/markets?epics=" + ",".join(part)).get("marketDetails", [])
        except Exception as e:
            log("⚠️ فحص دفعة الأسواق:", str(e)[:100]); continue
        for d in det:
            sn, ep = d.get("snapshot", {}), (d.get("instrument") or {}).get("epic")
            try:
                bid, ask = float(sn["bid"]), float(sn["offer"])
            except Exception:
                continue
            if sn.get("marketStatus") != "TRADEABLE" or not ep or bid <= 0:
                continue
            if (ask - bid) / bid * 100 > (0.15 if uni.get(ep, {}).get("t") == "SHARES" else SPREAD_MAX_PCT):
                continue
            DISP[ep] = DISP.get(ep) or uni.get(ep, {}).get("n") or ep
            out.append((ep, ep, bid))
    return out


def use_account(cap):
    """يثبّت البوت على الحساب المحدد بالاسم (ACCOUNT_NAME) حتى لا يتداول على حساب آخر بالغلط."""
    if not ACCOUNT_NAME:
        return
    accs = cap.call("GET", "/accounts").get("accounts", [])
    a = next((x for x in accs if str(x.get("accountName", "")).strip().lower() == ACCOUNT_NAME.lower()), None)
    if not a:
        names = ", ".join(str(x.get("accountName")) for x in accs)
        log(f"⛔ ما لقيت حساب باسم '{ACCOUNT_NAME}'. الحسابات الموجودة: {names}")
        raise SystemExit(1)
    ACC_ID[0] = a.get("accountId")
    if not a.get("preferred"):
        try:
            cap.call("PUT", "/session", {"accountId": ACC_ID[0]})
        except RuntimeError as e:
            if "not-different" not in str(e).lower():
                raise
        log(f"🔀 تم التحويل إلى الحساب: {ACCOUNT_NAME}")


def on_my_account(cap):
    """تأكد قبل كل صفقة أن الجلسة على حساب البوت."""
    if not ACC_ID[0]:
        return True
    for _ in range(2):
        cur = cap.call("GET", "/session").get("accountId")
        if cur == ACC_ID[0]:
            return True
        use_account(cap)
    return False


def cycle(cap, state, now, ctx):
    global EPIC, LEVEL_STEP
    use_account(cap)
    acc = cap.call("GET", "/accounts").get("accounts", [])
    a = next((x for x in acc if ACC_ID[0] and x.get("accountId") == ACC_ID[0]), None) \
        or next((x for x in acc if x.get("preferred")), acc[0] if acc else {})
    bal = float((a.get("balance") or {}).get("balance") or 0)
    allpos = cap.call("GET", "/positions").get("positions", [])
    trades = state.setdefault("trades", {})
    reconcile(state, allpos)
    mine = [p for p in allpos if p["position"]["dealId"] in trades]
    manage(cap, state, mine)
    mine = [p for p in mine if p["position"]["dealId"] in trades]
    fx = fx_for(cap, "USD") or 1.08
    avail = float((a.get("balance") or {}).get("available") or bal)
    evs = []
    for name, ep, p0 in pick_markets(cap, state, ctx):
        try:
            evs.append(analyze_market(cap, name, ep, now, p0))
        except Exception as e:
            log(f"{name}: تعذّر التحليل:", str(e)[:120])
    book = list(allpos)                                # كل صفقات الحساب (حتى لو ضاعت ذاكرة البوت): الحدود تُحسب عليها
    n_open = len(book)
    if n_open > len(mine):
        log(f"ℹ️ في {n_open - len(mine)} صفقة بالحساب خارج تتبّع هذا البوت (يدوية أو من بوت آخر) — محسوبة ضمن حد {G_MAX_OPEN} صفقات")
    halt = gate(cap, state)
    if halt:
        log("⛔ " + halt)
    state["halt"] = halt
    MIN_SCORE_NOW[0] = RELAX_SCORE if n_open < MIN_OPEN else MIN_SCORE
    cands = []
    for ev in evs:                                    # إعادة التقييم بالحد الحالي (نخفّفه إذا الصفقات أقل من المطلوب)
        EPIC, LEVEL_STEP = ev["ep"], ev["step"]
        ev2 = evaluate(ev["H"], ev["M"], ev["S"], now)
        ev.update(signals=ev2["signals"], status=ev2["status"])
        pct5 = (ev["S"]["a"][-1] or 0) / ev["price"] * 100
        for s in ev["signals"]:
            if SPEED_BONUS:
                s["score"] += min(15, pct5 * SPEED_BONUS)
            if ev["ep"] in PRIORITY:
                s["score"] += 5
            cands.append((s["score"], ev, s))
    cands.sort(key=lambda x: -x[0])
    opened = 0
    seen = state.setdefault("seen", {})
    cool = state.setdefault("cool", {})
    for score, ev, s in cands:
        if halt or opened >= G_MAX_NEW or (G_MAX_OPEN and n_open + opened >= G_MAX_OPEN):
            break
        if MIN_ATR_PCT and (ev["S"]["a"][-1] or 0) / ev["price"] * 100 < MIN_ATR_PCT:
            continue                                      # السوق بطيء الآن
        name, ep = ev["name"], ev["ep"]

        def block(why):
            for st in ev["status"]:
                if st["id"] == s["strategy"]:
                    st["state"] = "blocked"; st["text"] += " — " + why
        if not ev["sess"]["ok"]:
            block("ممنوع الآن: " + ("وقت أخبار " + ev["sess"]["blackout"] if ev["sess"]["blackout"] else ev["sess"]["quality"])); continue
        key = f"{ep}:{s['strategy']}{s['dir']}"
        stamp = (ev["S"] if s["strategy"] in ("trend", "pullback") else ev["M"])["cs"][-1]["t"].isoformat()
        if seen.get(key) == stamp:
            continue
        if time.time() - cool.get(key, 0) < COOLDOWN_MIN * 60:
            block(f"نفس الإشارة قبل أقل من {COOLDOWN_MIN:g} د"); continue
        same = [p for p in book if p["market"]["epic"] == ep]
        ty = ctx["uni"].get(ep, {}).get("t")
        if MAX_PER_TYPE and ty and sum(1 for p in book if ctx["uni"].get(p["market"]["epic"], {}).get("t") == ty) >= MAX_PER_TYPE:
            block(f"وصلت الحد من هذا النوع ({ty}) — تنويع"); continue
        if MAX_PER_EPIC and len(same) >= MAX_PER_EPIC:
            block(f"في صفقة مفتوحة على هذا السوق (الحد {MAX_PER_EPIC})"); continue
        if any((1 if p["position"]["direction"] == "BUY" else -1) != s["dir"] for p in same):
            block("في صفقة معاكسة مفتوحة على نفس السوق"); continue
        atr = ev["M"]["a"][-1] or 1
        if any(abs(ev["price"] - float(p["position"]["level"])) < 0.4 * atr for p in same):
            block("في صفقة قريبة من هذا السعر"); continue
        EPIC, LEVEL_STEP = ep, ev["step"]
        s["atr"] = ev["M"]["a"][-1]
        news = None
        if NEWS_MODE != "off":
            if ep not in bot.NEWS_QUERY and ctx["uni"].get(ep, {}).get("t") not in (None, "SHARES"):
                bot.NEWS_QUERY[ep] = f"{bot._clean_name(DISP.get(name, name))} price"
            news = bot.news_view(state, ep, DISP.get(name, name))
            if news.get("block"):
                block("خبر خطير على هذا السوق — تخطي"); continue
            ns = news.get("score", 0)
            against = (ns * s["dir"]) < -NEWS_MIN
            if against and NEWS_MODE == "require":
                block(f"الأخبار ضد الاتجاه ({ns:+.2f}) — تخطي"); continue
            if NEWS_MODE == "require" and abs(ns) < NEWS_MIN and not news.get("n"):
                pass                                      # لا أخبار: لا نمنع
            if (ns * s["dir"]) > NEWS_MIN:
                s["score"] += 15; s["why"] += f" + الأخبار تؤيد ({ns:+.2f})"
            elif against:
                s["score"] -= 15
        res = open_trade(cap, state, s, now, avail)
        seen[key] = stamp
        if res:
            opened += 1
            avail -= res.get("need", res["value"] * 0.25)
            cool[key] = time.time()
            trades[res["id"]] = {"ids": res.get("ids") or [res["id"]], "size0": res["size"], "fx": res.get("fx"), "name": DISP.get(name, name), "epic": ep, "strategy": s["strategy"], "dir": "BUY" if s["dir"] > 0 else "SELL", "entry": res["price"],
                                 "sl": res["stop"], "tp": res["target"], "lot": res.get("lot"), "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "why": s["why"], "size": res["size"]}
            book.append({"position": {"direction": "BUY" if s["dir"] > 0 else "SELL", "level": res["price"]}, "market": {"epic": ep}})
            mine.append({"position": {"dealId": res["id"], "direction": "BUY" if s["dir"] > 0 else "SELL", "level": res["price"], "size": res["size"],
                                      "stopLevel": res["stop"], "profitLevel": res["target"], "upl": 0},
                         "market": {"epic": ep, "bid": res["price"], "offer": res["price"]}})
            event(state, type="open", market=DISP.get(name, name), dir="BUY" if s["dir"] > 0 else "SELL", strategy=STRAT[s["strategy"]], price=res["price"], size=res["size"],
                  value_eur=round(res["value"]), stop=res["stop"], target=res["target"], risk=RISK_EUR, score=s["score"], pattern=s["pattern"], why=s["why"], news=(news or {}).get("top"))
    an = [{"name": DISP.get(ev["name"], ev["name"]), "epic": ev["ep"], "price": round(ev["price"], 4), "h1": ev["h1"], "m15": ev["m15"],
                          "sess": ev["sess"], "status": ev["status"], "tf": [tf_view(ev["H"], "الساعة"), tf_view(ev["M"], "15 دقيقة"), tf_view(ev["S"], "5 دقائق")]} for ev in evs]
    held = {p["market"]["epic"] for p in mine}
    an.sort(key=lambda x: (not any(t["state"] == "signal" for t in x["status"]), x["epic"] not in held))
    state["analysis"] = an[:10]
    publish(cap, state, bal, mine, fx, now, n_open + opened)


def publish(cap, state, bal, mine, fx, now, n_open):
    hist = []
    try:
        rows = bot.history_rows(cap, state)
        keys = ("gold", "xau", "oil", "crude", "silver", "xag", "dollar", "dxy", "eur/usd", "eurusd")
        hist = [r for r in rows if any(k in str(r.get("name") or "").lower() for k in keys)][:40]
    except Exception as e:
        log("⚠️ سجل المعاملات:", e)
    today = now.strftime("%Y-%m-%d")
    realized = sum(r["pnl"] for r in hist if str(r.get("t") or "").startswith(today))
    open_v = view_open(state, mine, fx)
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "demo", "account": ACCOUNT_NAME, "balance": round(bal, 2),
            "day_pnl": round(realized + sum(o["pnl"] for o in open_v), 2), "open": open_v, "history": hist,
            "analysis": state.get("analysis"), "events": state.get("events", [])[::-1][:80], "log": list(bot.LOG_BUF)[-50:],
            "halt": state.get("halt"), "day": state.get("day"), "settings": {"hold_min": MAX_HOLD_MIN, "daily_loss": DAILY_LOSS_EUR, "min_trade_eur": MIN_TRADE_EUR, "risk_eur": RISK_EUR, "tp_eur": TP_EUR, "min_open": MIN_OPEN, "max_open": G_MAX_OPEN,
                         "min_score": MIN_SCORE, "relax_score": RELAX_SCORE, "markets": [f"كل الأسواق ({state.get('scan_total', 0)})"],
                         "news_blackout": NEWS_BLACKOUT, "open_now": n_open}}
    if bot.PUBLISH_ON:
        bot.push_file("gold.json", data, GOLD_BRANCH)


def main():
    if bot.LIVE:
        raise SystemExit("⛔ gold.py للحساب التجريبي فقط (CAP_ENV يجب أن يكون demo)")
    if not (bot.API_KEY and bot.IDENTIFIER and bot.PASSWORD):
        raise SystemExit("أضف المفاتيح في GitHub Secrets (DEMO_CAP_API_KEY / DEMO_CAP_IDENTIFIER / DEMO_CAP_PASSWORD أو نفس المفاتيح العادية)")
    bot.STATE_FILE = STATE_FILE
    state = bot.load_state()
    cap = bot.Capital()
    _login = cap.login
    def login_and_pin():                               # بعد كل تسجيل دخول (حتى إعادة الدخول) يرجع لنفس الحساب
        _login()
        use_account(cap)
    cap.login = login_and_pin
    cap.login()
    uni = bot.build_rec_universe(cap, state)
    bot.save_state(state)
    if ONLY_EPICS:
        uni = {e: v for e, v in uni.items() if e in set(ONLY_EPICS)}
        log(f"🎯 قائمة محددة: {', '.join(uni)}")
    if not uni:                                        # لو فشل قراءة القوائم: الأسواق الأساسية فقط
        log("⚠️ تعذّر قراءة كل الأسواق، أكتفي بالذهب والنفط والفضة والدولار")
        uni = {ep: {"n": DISP.get(n, n), "t": "OTHER"} for n, ep in resolve_markets(cap).items()}
    ctx = {"uni": uni}
    if not uni:
        raise SystemExit("ما لقيت أي سوق")
    for ep, v in uni.items():
        DISP.setdefault(ep, v["n"])
    log(f"🌍 يراقب {len(uni)} سوقاً (كل شي قابل للتداول) | صفقة ≥{MIN_TRADE_EUR:.0f}€ | وقف -{RISK_EUR:.0f}€ هدف +{TP_EUR:.0f}€ | هدف: {MIN_OPEN}+ صفقات معاً")
    end = time.time() + LOOP_MINUTES * 60
    while True:
        now = datetime.now(timezone.utc)
        try:
            cycle(cap, state, now, ctx)
            bot.save_state(state)
        except Exception as e:
            log("خطأ عام:", e)
        if time.time() + CYCLE_SECONDS > end:
            break
        time.sleep(CYCLE_SECONDS)
    bot.save_state(state)
    log("انتهى هذا التشغيل")


if __name__ == "__main__":
    main()
