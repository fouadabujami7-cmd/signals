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
EPIC = E("GOLD_EPIC", "GOLD")
TRADE_EUR = float(E("TRADE_EUR", "500"))             # قيمة كل صفقة (الحجم × السعر) باليورو
G_MAX_OPEN = int(E("G_MAX_OPEN", "5"))                # أقصى صفقات مفتوحة معاً (0 = بلا حد)
G_MAX_NEW = int(E("G_MAX_NEW", "2"))                  # أقصى صفقات جديدة في الدورة الواحدة
RR = float(E("G_RR", "2"))                            # الهدف = الوقف × RR
MIN_SCORE = int(E("G_MIN_SCORE", "60"))
LEVEL_STEP = float(E("LEVEL_STEP", "50"))             # المستويات المدوّرة كل كم دولار
BE_AT_R = float(E("BE_AT_R", "1"))                    # نقل الوقف لنقطة الدخول عند ربح = هذا × مسافة الوقف (0 = معطّل)
SESSION_FROM, SESSION_TO = (int(x) for x in E("SESSION_GMT", "7-20").split("-"))   # ساعات السماح بالدخول (غرينتش)
BEST_FROM, BEST_TO = (int(x) for x in E("BEST_GMT", "13-17").split("-"))           # أنشط وقت (تداخل لندن ونيويورك)
NEWS_BLACKOUT = [x.strip() for x in E("NEWS_BLACKOUT", "13:15-13:45,15:00-15:15").split(",") if x.strip()]   # لا دخول (غرينتش، أيام العمل)
COOLDOWN_MIN = float(E("G_COOLDOWN_MIN", "20"))       # نفس الاستراتيجية والاتجاه لا تتكرر قبل هذا
MAX_RISK_EUR = float(E("G_MAX_RISK_EUR", "100"))      # سقف خسارة الصفقة الواحدة (أمان)
LOOP_MINUTES = float(E("LOOP_MINUTES", "14"))
CYCLE_SECONDS = float(E("CYCLE_SECONDS", "45"))
GOLD_BRANCH = E("GOLD_BRANCH", "gold-data")
STATE_FILE = E("STATE_FILE", "gold_state.json")
DRY_RUN = E("DRY_RUN", "0") == "1"
STRAT = {"bounce": "ارتداد من مستوى", "retest": "كسر وإعادة اختبار", "pullback": "تراجع داخل الاتجاه", "asia": "كسر كاذب لنطاق آسيا"}


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
    if P["e9"][i] > P["e21"][i] and 45 < P["r"][i] < 72:
        return 1
    if P["e9"][i] < P["e21"][i] and 28 < P["r"][i] < 55:
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
    if wd >= 5:
        q, ok = "إغلاق نهاية الأسبوع", False
    elif BEST_FROM <= h < BEST_TO:
        q, ok = "ممتازة (تداخل لندن ونيويورك)", True
    elif SESSION_FROM <= h < SESSION_TO:
        q, ok = "جيدة", True
    else:
        q, ok = "هادئة (خارج ساعات الدخول)", False
    return {"quality": q, "ok": ok and not blackout, "blackout": blackout, "best": BEST_FROM <= h < BEST_TO and wd < 5}


# ===== التقييم =====
def evaluate(H, M, S, now):
    h1, m15 = trend_h1(H), dir_m15(M)
    sess = session_info(now)
    out, status = [], []
    for key, fn in (("bounce", s_bounce), ("retest", s_retest), ("pullback", s_pullback), ("asia", None)):
        s, why = (s_asia(H, M, S, h1, m15, now) if key == "asia" else fn(H, M, S, h1, m15))
        if s:
            score = 50 + (15 if h1 == s["dir"] else 0 if h1 == 0 else -30) + (10 if m15 == s["dir"] else 0) + (10 if s["pattern"] else 0) + s["bonus"] + (10 if sess["best"] else 0)
            i15 = len(M["c"]) - 1
            if (s["dir"] > 0 and M["r"][i15] > 75) or (s["dir"] < 0 and M["r"][i15] < 25):
                score -= 10
            s["score"] = score
            if score < MIN_SCORE:
                status.append({"id": key, "name": STRAT[key], "state": "weak", "text": f"إشارة {'شراء' if s['dir'] > 0 else 'بيع'} لكن القوة {score} < {MIN_SCORE}: {s['why']}"})
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
def eurusd(cap):
    try:
        s = cap.call("GET", "/markets/EURUSD").get("snapshot", {})
        return (float(s["bid"]) + float(s["offer"])) / 2
    except Exception as e:
        log("⚠️ تعذّر سعر EURUSD، أفترض 1.08:", e)
        return 1.08


def open_trade(cap, state, s, now, fx):
    m = cap.call("GET", f"/markets/{EPIC}")
    snap, rules, inst = m.get("snapshot", {}), m.get("dealingRules", {}), m.get("instrument", {})
    if snap.get("marketStatus") != "TRADEABLE":
        log("الذهب: السوق مغلق"); return None
    buy = s["dir"] > 0
    bid, ask = float(snap["bid"]), float(snap["offer"])
    price = ask if buy else bid
    spread = ask - bid
    dist = abs(price - s["sl"])
    dist = max(dist, spread * 3, bot.min_dist(rules, "minStopOrProfitDistance", price) * 1.1)
    lot = float(inst.get("lotSize") or 1)
    min_size = float(rules["minDealSize"]["value"])
    step = float((rules.get("minSizeIncrement") or {}).get("value") or min_size)
    size = round(round(TRADE_EUR * fx / (price * lot) / step) * step, 6)
    size = max(size, min_size)
    size = min(size, float((rules.get("maxDealSize") or {}).get("value") or 1e12))
    value = size * price * lot / fx
    if value > TRADE_EUR * 1.5:
        log(f"الذهب: أصغر حجم ({min_size}) قيمته {value:.0f} € أكبر بكثير من {TRADE_EUR:.0f} — تخطي"); return None
    risk_eur = size * dist * lot / fx
    if MAX_RISK_EUR and risk_eur > MAX_RISK_EUR:
        log(f"الذهب: خطر الصفقة {risk_eur:.1f} € أكبر من الحد {MAX_RISK_EUR:.0f} — تخطي"); return None
    dec = int(snap.get("decimalPlacesFactor") or 2)
    stop = round(price - dist if buy else price + dist, dec)
    target = round(price + dist * RR if buy else price - dist * RR, dec)
    log(f"الذهب: {'🟢 شراء' if buy else '🔴 بيع'} [{STRAT[s['strategy']]}] حجم {size} (≈{value:.0f} €) @ {price} | وقف {stop} | هدف {target} | خطر ≈{risk_eur:.1f} € | {s['why']}")
    if DRY_RUN:
        return {"id": "dry", "size": size, "price": price, "stop": stop, "target": target, "value": value, "risk": risk_eur}
    order = {"epic": EPIC, "direction": "BUY" if buy else "SELL", "size": size, "stopLevel": stop, "profitLevel": target, "guaranteedStop": False}
    try:
        ref = cap.call("POST", "/positions", order).get("dealReference")
        time.sleep(1)
        c = cap.call("GET", f"/confirms/{ref}")
    except Exception as e:
        log("الذهب: رفض الطلب:", e); return None
    log(f"الذهب: {c.get('dealStatus')} {c.get('reason', '')}")
    if c.get("dealStatus") != "ACCEPTED":
        return None
    ids = [c.get("dealId")] + [a.get("dealId") for a in c.get("affectedDeals", []) or []]
    ids = [i for i in dict.fromkeys(ids) if i]
    return {"id": ids[0] if ids else ref, "size": size, "price": price, "stop": stop, "target": target, "value": value, "risk": risk_eur}


def event(state, **kw):
    ev = {"t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **kw}
    state["events"] = (state.get("events", []) + [ev])[-150:]


def manage(cap, state, positions):
    """يتتبّع صفقاتنا: تأمين عند ربح BE_AT_R، وتسجيل الإغلاقات"""
    trades = state.setdefault("trades", {})
    live = {p["position"]["dealId"]: p for p in positions}
    for did, tr in list(trades.items()):
        p = live.get(did)
        if not p:
            d = 1 if tr["dir"] == "BUY" else -1
            last = tr.get("last_price") or tr["entry"]
            reason = "هدف" if abs(last - tr["tp"]) < abs(last - tr["sl"]) else ("وقف مؤمّن" if tr.get("be") else "وقف")
            event(state, type="close", dir=tr["dir"], strategy=tr["strategy"], price=last, pnl=round(tr.get("upl") or 0, 2), why=f"أُغلقت ({reason}) — النتيجة تقريبية من آخر قراءة", entry=tr["entry"])
            log(f"الذهب: أُغلقت صفقة {tr['dir']} [{STRAT.get(tr['strategy'])}] ≈ {tr.get('upl') or 0:+.2f}")
            del trades[did]
            continue
        pos, mk = p["position"], p["market"]
        buy = pos["direction"] == "BUY"
        now_p = float(mk["bid"] if buy else mk["offer"])
        lvl = float(pos["level"])
        tr["last_price"], tr["upl"] = now_p, float(pos.get("upl") or 0)
        stop = pos.get("stopLevel")
        dist = abs(lvl - tr["sl"])
        gain = (now_p - lvl) if buy else (lvl - now_p)
        if BE_AT_R and not tr.get("be") and stop is not None and dist > 0 and gain >= BE_AT_R * dist:
            spread = abs(float(mk["offer"]) - float(mk["bid"]))
            new_stop = round(lvl + spread if buy else lvl - spread, 2)
            log(f"الذهب: 🔒 نقل الوقف إلى الدخول {new_stop}")
            if not DRY_RUN:
                body = {"stopLevel": new_stop}
                if pos.get("profitLevel") is not None:
                    body["profitLevel"] = float(pos["profitLevel"])
                try:
                    cap.call("PUT", f"/positions/{pos['dealId']}", body)
                    tr["be"] = True
                except Exception as e:
                    log("الذهب: تعذّر التأمين:", e)
                    continue
            event(state, type="secure", dir=pos["direction"], strategy=tr["strategy"], price=new_stop, why=f"نقل الوقف لسعر الدخول (ربح {gain:.2f}$ = {BE_AT_R:g}R)")


def view_open(state, positions, fx):
    out = []
    trades = state.get("trades", {})
    for p in positions:
        pos, mk = p["position"], p["market"]
        tr = trades.get(pos["dealId"])
        if not tr:
            continue
        buy = pos["direction"] == "BUY"
        out.append({"id": pos["dealId"], "dir": pos["direction"], "size": float(pos["size"]), "entry": float(pos["level"]),
                    "price": float(mk["bid"] if buy else mk["offer"]), "stop": float(pos["stopLevel"]) if pos.get("stopLevel") is not None else None,
                    "target": float(pos["profitLevel"]) if pos.get("profitLevel") is not None else None, "pnl": round(float(pos.get("upl") or 0), 2),
                    "strategy": STRAT.get(tr["strategy"], tr["strategy"]), "why": tr.get("why"), "t": tr.get("t"), "be": bool(tr.get("be")),
                    "value_eur": round(float(pos["size"]) * float(pos["level"]) / fx)})
    return out


def blocked_reason(state, s, positions_dir, now, price_atr15):
    key = f"{s['strategy']}{s['dir']}"
    last = state.setdefault("cool", {}).get(key)
    if last and time.time() - last < COOLDOWN_MIN * 60:
        return f"نفس الاستراتيجية والاتجاه قبل أقل من {COOLDOWN_MIN:g} د"
    if positions_dir and any(d != s["dir"] for d in positions_dir):
        return "في صفقات مفتوحة بالاتجاه المعاكس (الحساب بيدمجها)"
    return None


def cycle(cap, state, now, fx_cache):
    acc = cap.call("GET", "/accounts").get("accounts", [])
    a = next((x for x in acc if x.get("preferred")), acc[0] if acc else {})
    bal = float((a.get("balance") or {}).get("balance") or 0)
    allpos = cap.call("GET", "/positions").get("positions", [])
    mine = [p for p in allpos if p["market"]["epic"] == EPIC and p["position"]["dealId"] in state.get("trades", {})]
    manage(cap, state, mine)
    mine = [p for p in mine if p["position"]["dealId"] in state.get("trades", {})]
    fx = fx_cache.get("v") or eurusd(cap)
    fx_cache["v"] = fx
    H, M, S = prep(candles(cap, "HOUR", 200)), prep(candles(cap, "MINUTE_15", 200)), prep(candles(cap, "MINUTE_5", 150))
    ev = evaluate(H, M, S, now)
    price = S["c"][-1]
    dirs = [1 if p["position"]["direction"] == "BUY" else -1 for p in mine]
    opened = 0
    for s in ev["signals"]:
        if opened >= G_MAX_NEW or (G_MAX_OPEN and len(mine) + opened >= G_MAX_OPEN):
            break
        if not ev["sess"]["ok"]:
            for st in ev["status"]:
                if st["id"] == s["strategy"]:
                    st["state"] = "blocked"
                    st["text"] += " — ممنوع الآن: " + ("وقت أخبار " + ev["sess"]["blackout"] + " غرينتش" if ev["sess"]["blackout"] else ev["sess"]["quality"])
            continue
        ck = state.setdefault("seen", {})
        ckey = f"{s['strategy']}{s['dir']}"
        stamp = (M if s["strategy"] in ("bounce", "retest", "asia") else S)["cs"][-1]["t"].isoformat()
        if ck.get(ckey) == stamp:
            continue                                   # نفس الشمعة سبق تقييمها
        why_not = blocked_reason(state, s, dirs + [s["dir"]] * 0 if not dirs else dirs, now, M["a"][-1])
        if not why_not and any(abs(price - float(p["position"]["level"])) < 0.5 * (M["a"][-1] or 1) and (1 if p["position"]["direction"] == "BUY" else -1) == s["dir"] for p in mine):
            why_not = "في صفقة بنفس الاتجاه قريبة من هذا السعر"
        if why_not:
            for st in ev["status"]:
                if st["id"] == s["strategy"]:
                    st["state"] = "blocked"; st["text"] += " — " + why_not
            continue
        res = open_trade(cap, state, s, now, fx)
        ck[ckey] = stamp
        if res:
            opened += 1
            state["cool"][ckey] = time.time()
            dirs.append(s["dir"])
            state.setdefault("trades", {})[res["id"]] = {"strategy": s["strategy"], "dir": "BUY" if s["dir"] > 0 else "SELL", "entry": res["price"],
                                                         "sl": res["stop"], "tp": res["target"], "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "why": s["why"], "size": res["size"]}
            event(state, type="open", dir="BUY" if s["dir"] > 0 else "SELL", strategy=STRAT[s["strategy"]], price=res["price"], size=res["size"],
                  value_eur=round(res["value"]), stop=res["stop"], target=res["target"], risk=round(res["risk"], 2), score=s["score"], pattern=s["pattern"],
                  why=s["why"], h1=ev["h1"], m15=ev["m15"])
    state["analysis"] = {"price": round(price, 2), "h1": ev["h1"], "m15": ev["m15"], "sess": ev["sess"], "status": ev["status"],
                         "tf": [tf_view(H, "الساعة"), tf_view(M, "15 دقيقة"), tf_view(S, "5 دقائق")],
                         "levels": {"near": nearest_level(price), "step": LEVEL_STEP}}
    publish(cap, state, bal, mine, fx, now)


def publish(cap, state, bal, mine, fx, now):
    hist = []
    try:
        rows = bot.history_rows(cap, state)
        hist = [r for r in rows if "gold" in str(r.get("name") or "").lower() or "xau" in str(r.get("name") or "").lower()][:40]
    except Exception as e:
        log("⚠️ سجل المعاملات:", e)
    today = now.strftime("%Y-%m-%d")
    realized = sum(r["pnl"] for r in hist if str(r.get("t") or "").startswith(today))
    open_v = view_open(state, mine, fx)
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "demo", "balance": round(bal, 2),
            "day_pnl": round(realized + sum(o["pnl"] for o in open_v), 2), "open": open_v, "history": hist,
            "analysis": state.get("analysis"), "events": state.get("events", [])[::-1][:80], "log": list(bot.LOG_BUF)[-50:],
            "settings": {"trade_eur": TRADE_EUR, "max_open": G_MAX_OPEN, "rr": RR, "min_score": MIN_SCORE, "session": f"{SESSION_FROM}-{SESSION_TO} GMT",
                         "best": f"{BEST_FROM}-{BEST_TO} GMT", "news_blackout": NEWS_BLACKOUT, "be_at_r": BE_AT_R}}
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
    cap.login()
    log(f"🥇 روبوت الذهب (تجريبي) | قيمة الصفقة {TRADE_EUR:.0f} € | أقصى صفقات {G_MAX_OPEN or '∞'} | RR {RR:g} | حد القوة {MIN_SCORE}")
    end, fx_cache = time.time() + LOOP_MINUTES * 60, {}
    while True:
        now = datetime.now(timezone.utc)
        try:
            cycle(cap, state, now, fx_cache)
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
