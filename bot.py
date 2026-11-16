"""
روبوت التداول — النسخة الكاملة (الحجم حسب المخاطرة + الذكاء الاصطناعي + أسواق متعددة)

يعمل على الحساب التجريبي افتراضياً. للحساب الحقيقي يجب ضبط الاثنين معاً في bot.yml:
    CAP_ENV: "live"   و   LIVE_CONFIRM: "I_UNDERSTAND"

كيف يقرر:
1) الذكاء الاصطناعي (مرة كل ساعة تقريباً) يبحث في الأخبار وآراء البنوك ويعطي لكل سوق: شراء/بيع/انتظار + ثقة.
2) التحليل الفني (كل دقيقة) يعطي نتيجة من -100 إلى +100 لاتجاه الدقائق القادمة.
3) النتيجة النهائية = 60% فني + 40% ذكاء اصطناعي. يدخل فقط إذا:
   |النتيجة| >= ENTRY_THR، والفني والذكاء الاصطناعي غير متعاكسين.
4) الحجم = (الرصيد × RISK_PCT%) ÷ (مسافة الوقف × حجم العقد) → أقصى خسارة للصفقة = RISK_PCT% من الرصيد.

الحماية:
- لا أكثر من MAX_OPEN صفقات مفتوحة، وصفقة واحدة لكل سوق.
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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
STATE_FILE = "state.json"
BERLIN = ZoneInfo("Europe/Berlin")
WEIGHTS = {"trend": 2, "cross": 1.5, "slope": 1, "rsi": 1, "macd": 1, "mom": 1.5, "live": 0.5}


def log(*a):
    print(datetime.now(BERLIN).strftime("%H:%M:%S"), *a, flush=True)


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
    body = {"model": "claude-sonnet-5-5", "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}],
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


def ai_view(state):
    if not CLAUDE_KEY:
        return {}
    ai = state.get("ai", {})
    if time.time() - ai.get("time", 0) < AI_EVERY_MIN * 60:
        return ai.get("data", {})
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M")
    prompt = f"""أنت محلل أسواق للمضاربة قصيرة المدى. الوقت الآن {now} بتوقيت برلين.
ابحث في الإنترنت عن أحدث الأخبار (آخر 24 ساعة) والأحداث القادمة اليوم: البنوك المركزية، التضخم والوظائف، الجيوسياسة، أوبك، آراء البنوك الكبرى، ومزاج المتداولين.
ثم أعطِ الاتجاه الأرجح للساعات القليلة القادمة لكل سوق من هذه الرموز (رموز Capital.com): {", ".join(EPICS)}.
اختر buy أو sell عندما يوجد سبب واضح، و wait إذا كان هناك خبر قوي خلال 30 دقيقة أو الصورة متضاربة.
أجب في النهاية بـ JSON فقط بدون أي نص بعده، بهذا الشكل:
{{"GOLD":{{"bias":"buy|sell|wait","confidence":0-100,"reason":"جملة قصيرة"}}, ...}}"""
    try:
        data = ask_claude(prompt)
        state["ai"] = {"time": time.time(), "data": data}
        save_state(state)
        log("🤖 تحليل الذكاء الاصطناعي:")
        for k, v in data.items():
            if isinstance(v, dict):
                log(f"   {k}: {v.get('bias')} {v.get('confidence')}% — {v.get('reason', '')}")
        return data
    except Exception as e:
        log("⚠️ تعذّر تحليل الذكاء الاصطناعي:", e)
        return ai.get("data", {})


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
def decide(t, ai):
    """يرجع (الاتجاه، النتيجة النهائية، السبب)"""
    if ai and ai.get("bias") == "wait":
        return None, 0, "الذكاء الاصطناعي ينصح بالانتظار (خبر قوي أو صورة متضاربة)"
    if ai and ai.get("bias") in ("buy", "sell"):
        a = {"buy": 1, "sell": -1}[ai["bias"]] * (float(ai.get("confidence", 50)) / 100)
        if a * t["score"] < 0:
            return None, 0, "الفني والذكاء الاصطناعي متعاكسان"
        final = round(0.6 * t["score"] + 40 * a)
        thr = ENTRY_THR
    else:
        final, thr = t["score"], ENTRY_THR + 10    # بدون ذكاء اصطناعي: نطلب قوة أعلى
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
    if epic in SIZE_RANGES:                         # حدودك أنت لها الأولوية
        lo, hi = SIZE_RANGES[epic]
        size = min(max(size, lo), hi)
    elif size < min_size:
        if min_size * dist * lot > risk_money * 1.5:
            log(f"{epic}: أصغر حجم ({min_size}) خطره أكبر من {RISK_PCT}% — تخطي"); return
        size = min_size
    size = max(size, min_size)
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
                return loss
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


def cycle(cap, state):
    bal, cur = balance(cap)
    today = day_pnl(state, bal)
    allpos = cap.call("GET", "/positions").get("positions", [])
    positions = [p for p in allpos if p["market"]["epic"] in EPICS]   # الروبوت لا يلمس صفقات الأسواق الأخرى
    if len(allpos) > len(positions):
        log(f"(تجاهل {len(allpos) - len(positions)} صفقة في أسواق أخرى)")
    # إغلاق بالوقت (معطّل افتراضياً) + تأمين الصفقات الرابحة
    for p in positions:
        pos = p["position"]
        opened = datetime.fromisoformat(pos["createdDateUTC"].replace("Z", "")).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        if MAX_HOLD_MIN > 0 and age > MAX_HOLD_MIN:
            log(f"{p['market']['epic']}: إغلاق بعد {age:.0f} دقيقة (النتيجة {pos.get('upl')})")
            if not DRY_RUN:
                cap.call("DELETE", f"/positions/{pos['dealId']}")
            continue
        try:
            secure(cap, p)
        except Exception as e:
            log(f"{p['market']['epic']}: تعذّر التأمين", e)
    # حد الخسارة: خسارة اليوم + خطر الصفقات المفتوحة لا يتجاوز الحد
    limit = MAX_DAILY_LOSS_EUR if MAX_DAILY_LOSS_EUR > 0 else bal * MAX_DAILY_LOSS_PCT / 100
    open_risk = sum(pos_risk(p) for p in positions)
    budget = limit + min(today, 0) - open_risk
    log(f"الرصيد {bal:.2f} {cur} | اليوم {today:+.2f} | مفتوحة {len(positions)}/{MAX_OPEN} | "
        f"خطر مفتوح {open_risk:.2f} | المتبقي من حد {limit:.0f}: {budget:.2f}")
    if budget <= 0 or len(positions) >= MAX_OPEN:
        if budget <= 0:
            log("⛔ لا مجال لصفقات جديدة (حد الخسارة)")
        return
    # سوق مسموح فيه صفقة جديدة: لا صفقات فيه، أو كل صفقاته مؤمّنة، وبحد MAX_PER_EPIC
    by_epic = {}
    for p in positions:
        by_epic.setdefault(p["market"]["epic"], []).append(p)
    ai = ai_view(state)
    candidates = []
    for epic in EPICS:
        mine = by_epic.get(epic, [])
        if len(mine) >= MAX_PER_EPIC or any(pos_risk(p) > 0 for p in mine):
            continue
        try:
            t = tech(cap, epic)
            if not t:
                continue
            a = ai.get(epic) if isinstance(ai.get(epic), dict) else None
            r = t.get("rebound")
            if r and not (a and a.get("bias") == "wait"):
                log(f"{epic}: ⚡ {r['why']} → ✅ {r['dir']}")
                candidates.append((101, epic, r["dir"], t, r["why"], r))
                continue
            d, final, why = decide(t, a)
            log(f"{epic}: فني {t['score']:+d}" + (f" | AI {a.get('bias')} {a.get('confidence')}%" if a else "")
                + f" → {final:+d} {('✅ ' + d) if d else why}" + (f" | صفقات مؤمّنة {len(mine)}" if mine else ""))
            if d:
                candidates.append((abs(final), epic, d, t, (a or {}).get("reason", ""), None))
        except Exception as e:
            log(f"{epic}: خطأ", e)
    for _, epic, d, t, reason, lv in sorted(candidates, key=lambda x: -x[0])[: MAX_OPEN - len(positions)]:
        if budget <= 0:
            break
        try:
            used = open_trade(cap, epic, d, t, bal, reason, budget, lv)
            if used:
                budget -= used
        except Exception as e:
            log(f"{epic}: خطأ في فتح الصفقة", e)


def main():
    if not (API_KEY and IDENTIFIER and PASSWORD):
        raise SystemExit("أضف CAP_API_KEY و CAP_IDENTIFIER و CAP_PASSWORD في GitHub Secrets")
    if E("CAP_ENV") == "live" and not LIVE:
        log("⚠️ CAP_ENV=live لكن بدون LIVE_CONFIRM=I_UNDERSTAND — سأعمل على الحساب التجريبي")
    log(f"الإعدادات: مخاطرة {RISK_PCT}% | حد الخسارة {MAX_DAILY_LOSS_EUR:.0f}€ | أقصى صفقات {MAX_OPEN} "
        f"({MAX_PER_EPIC} لكل سوق) | مرتدات {'نعم' if REBOUND else 'لا'} | أسواق {', '.join(EPICS)}")
    cap, state = Capital(), load_state()
    cap.login()
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
