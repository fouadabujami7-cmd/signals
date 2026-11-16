"""
روبوت تداول الذهب والنفط على حساب Capital.com التجريبي (Demo فقط).
يشتغل عبر GitHub Actions: كل تشغيل يدور LOOP_MINUTES دقيقة ويفحص السوق كل دقيقة.

القواعد:
- نفس نموذج التوقع في التطبيق (8 عوامل → نتيجة من -100 إلى +100 لاتجاه الدقائق الخمس القادمة).
- يفتح صفقة فقط إذا |النتيجة| >= ENTRY_THR، وصفقة واحدة كحد أقصى لكل سوق.
- كل صفقة لها وقف خسارة وهدف عند الوسيط (ATR × SL_MULT، والهدف = الوقف × RR).
- يغلق الصفقة إذا بقيت مفتوحة أكثر من MAX_HOLD_MIN دقيقة.
- يتوقف عن فتح صفقات جديدة لبقية اليوم بعد MAX_LOSSES صفقات خاسرة.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ===== الإعدادات (تُغيَّر من ملف bot.yml) =====
BASE = "https://demo-api-capital.backend-capital.com/api/v1"   # Demo فقط — مقصود
API_KEY = os.environ.get("CAP_API_KEY", "")
IDENTIFIER = os.environ.get("CAP_IDENTIFIER", "")
PASSWORD = os.environ.get("CAP_PASSWORD", "")
EPICS = [e.strip() for e in os.environ.get("EPICS", "GOLD,OIL_CRUDE").split(",") if e.strip()]
ENTRY_THR = float(os.environ.get("ENTRY_THR", "40"))
MAX_LOSSES = int(os.environ.get("MAX_LOSSES", "5"))
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "14"))
MAX_HOLD_MIN = float(os.environ.get("MAX_HOLD_MIN", "30"))
SIZE_MULT = float(os.environ.get("SIZE_MULT", "1"))
SL_MULT = float(os.environ.get("SL_MULT", "1.5"))
RR = float(os.environ.get("RR", "1.5"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
BERLIN = ZoneInfo("Europe/Berlin")
WEIGHTS = {"trend": 2, "cross": 1.5, "slope": 1, "rsi": 1, "macd": 1, "mom": 1.5, "live": 0.5}


def log(*a):
    print(datetime.now(BERLIN).strftime("%H:%M:%S"), *a, flush=True)


# ===== الاتصال بـ Capital.com =====
class Capital:
    def __init__(self):
        self.cst = self.token = None

    def _req(self, method, path, body=None, auth=True):
        headers = {"Content-Type": "application/json", "X-CAP-API-KEY": API_KEY.strip()}
        if auth:
            headers.update({"CST": self.cst or "", "X-SECURITY-TOKEN": self.token or ""})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.headers, json.loads(raw)

    def login(self):
        try:
            h, _ = self._req("POST", "/session",
                             {"identifier": IDENTIFIER.strip(), "password": PASSWORD.strip(), "encryptedPassword": False}, auth=False)
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            log(f"❌ فشل تسجيل الدخول: HTTP {e.code} {body}")
            log("تأكد: المفتاح فعّال في Capital.com، الإيميل صحيح، وكلمة السر هي كلمة سر الـ API (وليست كلمة سر الحساب)")
            raise SystemExit(1)
        self.cst, self.token = h.get("CST"), h.get("X-SECURITY-TOKEN")
        if not self.cst:
            raise RuntimeError("فشل تسجيل الدخول")
        log("✓ تم تسجيل الدخول (Demo)")

    def call(self, method, path, body=None):
        try:
            return self._req(method, path, body)[1]
        except urllib.error.HTTPError as e:
            if e.code == 401:          # انتهت الجلسة → دخول من جديد ومحاولة ثانية
                self.login()
                return self._req(method, path, body)[1]
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {e.read().decode()[:300]}")


# ===== المؤشرات (نفس منطق التطبيق) =====
def ema(v, n):
    k, o = 2 / (n + 1), []
    for i, x in enumerate(v):
        o.append(x if i == 0 else x * k + o[-1] * (1 - k))
    return o


def rsi(c, n=14):
    o, g, l = [50.0] * len(c), 0.0, 0.0
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        u, w = max(d, 0), max(-d, 0)
        if i <= n:
            g += u; l += w
            if i == n:
                g /= n; l /= n
                o[i] = 100 - 100 / (1 + g / l) if l else 100
        else:
            g = (g * (n - 1) + u) / n; l = (l * (n - 1) + w) / n
            o[i] = 100 - 100 / (1 + g / l) if l else 100
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


def clip(x):
    return max(-1.0, min(1.0, x))


def mid(p):
    return (float(p["bid"]) + float(p["ask"])) / 2


def bars(cap, epic, res, n):
    d = cap.call("GET", f"/prices/{epic}?resolution={res}&max={n}")
    return [{"o": mid(p["openPrice"]), "h": mid(p["highPrice"]), "l": mid(p["lowPrice"]), "c": mid(p["closePrice"])}
            for p in d.get("prices", [])]


def score(cap, epic):
    allb = bars(cap, epic, "MINUTE", 300)
    h15 = bars(cap, epic, "MINUTE_15", 200)
    if len(allb) < 60 or len(h15) < 60:
        return None
    b, live = allb[:-1], allb[-1]
    c = [x["c"] for x in b]
    eF, eS, R, A = ema(c, 9), ema(c, 21), rsi(c), atr(b)
    macd = [x - y for x, y in zip(ema(c, 12), ema(c, 26))]
    hist = [x - y for x, y in zip(macd, ema(macd, 9))]
    trend = ema([x["c"] for x in h15[:-1]], 50)[-1]
    n, a = len(b) - 1, A[-1]
    fx = {
        "trend": clip((c[n] - trend) / (2 * a)),
        "cross": clip((eF[n] - eS[n]) / (0.5 * a)),
        "slope": clip((eF[n] - eF[n - 3]) / a),
        "rsi": clip((R[n] - 50) / 20),
        "macd": clip(hist[n] / (0.3 * a)),
        "mom": clip((c[n] - c[n - 5]) / (1.5 * a)),
        "live": clip((live["c"] - live["o"]) / a),
    }
    s = round(sum(fx[k] * WEIGHTS[k] for k in fx) / sum(WEIGHTS.values()) * 100)
    return {"score": s, "atr": a, "fx": fx}


# ===== حد الخسارة اليومي =====
def losses_today(cap):
    start = datetime.now(BERLIN).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    q = urllib.parse.urlencode({"from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                                "to": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "type": "TRADE"})
    try:
        d = cap.call("GET", "/history/transactions?" + q)
        return sum(1 for t in d.get("transactions", []) if float(t.get("size", 0) or 0) < 0)
    except Exception as e:
        log("⚠️ تعذّر قراءة سجل اليوم:", e)
        return 0


# ===== التداول =====
def stop_distance(market, price, want):
    rule = (market.get("dealingRules") or {}).get("minStopOrProfitDistance") or {}
    v = float(rule.get("value") or 0)
    minimum = price * v / 100 if rule.get("unit") == "PERCENTAGE" else v
    return max(want, minimum * 1.1)


def manage(cap, epic, positions, stopped):
    mine = [p for p in positions if p["market"]["epic"] == epic]
    if mine:
        p = mine[0]["position"]
        opened = datetime.fromisoformat(p["createdDateUTC"].replace("Z", "")).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        if age > MAX_HOLD_MIN:
            log(f"{epic}: إغلاق بعد {age:.0f} دقيقة (الربح/الخسارة {p.get('upl')})")
            if not DRY_RUN:
                cap.call("DELETE", f"/positions/{p['dealId']}")
        return
    if stopped:
        return
    m = cap.call("GET", f"/markets/{epic}")
    snap = m.get("snapshot", {})
    if snap.get("marketStatus") != "TRADEABLE":
        log(f"{epic}: السوق مغلق"); return
    s = score(cap, epic)
    if not s:
        log(f"{epic}: بيانات غير كافية"); return
    log(f"{epic}: النتيجة {s['score']:+d}")
    if abs(s["score"]) < ENTRY_THR:
        return
    buy = s["score"] > 0
    price = float(snap["offer"] if buy else snap["bid"])
    dist = stop_distance(m, price, s["atr"] * SL_MULT)
    dec = 3 if price < 10 else 2
    stop = round(price - dist if buy else price + dist, dec)
    target = round(price + dist * RR if buy else price - dist * RR, dec)
    size = round(float(m["dealingRules"]["minDealSize"]["value"]) * SIZE_MULT, 4)
    order = {"epic": epic, "direction": "BUY" if buy else "SELL", "size": size,
             "stopLevel": stop, "profitLevel": target, "guaranteedStop": False}
    log(f"{epic}: {'🟢 شراء' if buy else '🔴 بيع'} حجم {size} @ {price} وقف {stop} هدف {target}")
    if DRY_RUN:
        return
    ref = cap.call("POST", "/positions", order).get("dealReference")
    time.sleep(1)
    c = cap.call("GET", f"/confirms/{ref}")
    log(f"{epic}: {c.get('dealStatus')} {c.get('reason', '')}")


def main():
    if not (API_KEY and IDENTIFIER and PASSWORD):
        raise SystemExit("أضف CAP_API_KEY و CAP_IDENTIFIER و CAP_PASSWORD في GitHub Secrets")
    cap = Capital()
    cap.login()
    end = time.time() + LOOP_MINUTES * 60
    while True:
        try:
            lost = losses_today(cap)
            stopped = lost >= MAX_LOSSES
            if stopped:
                log(f"⛔ {lost} صفقات خاسرة اليوم — لا صفقات جديدة حتى الغد")
            positions = cap.call("GET", "/positions").get("positions", [])
            for epic in EPICS:
                try:
                    manage(cap, epic, positions, stopped)
                except Exception as e:
                    log(f"{epic}: خطأ", e)
        except Exception as e:
            log("خطأ عام:", e)
        if time.time() + 60 > end:
            break
        time.sleep(60)
    log("انتهى هذا التشغيل")


if __name__ == "__main__":
    main()
