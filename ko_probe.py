"""فحص للقراءة فقط: هل منتجات Knock-Out (KO) ظاهرة عبر API كابيتال؟ لا يفتح ولا يغلق ولا يعدّل أي صفقة."""
import json
import urllib.parse

import bot
from bot import log

TERMS = ["KO Gold", "KO", "Knock", "Knock-out", "Gold KO", "Turbo"]


def short(m):
    return f"{m.get('epic')} | {m.get('instrumentName')} | type={m.get('instrumentType')} | {m.get('marketStatus')}"


def main():
    if not (bot.API_KEY and bot.IDENTIFIER and bot.PASSWORD):
        raise SystemExit("أضف مفاتيح كابيتال في Secrets")
    cap = bot.Capital()
    cap.login()
    log("✓ تم تسجيل الدخول —", "حقيقي" if bot.LIVE else "تجريبي", "(قراءة فقط)")
    seen, kos = set(), []
    for t in TERMS:
        try:
            d = cap.call("GET", "/markets?searchTerm=" + urllib.parse.quote(t))
        except Exception as e:
            log(f"بحث '{t}': خطأ {str(e)[:120]}"); continue
        ms = d.get("markets") or []
        log(f"🔎 بحث '{t}': {len(ms)} نتيجة")
        for m in ms[:12]:
            if m.get("epic") in seen:
                continue
            seen.add(m.get("epic"))
            log("   " + short(m))
            blob = json.dumps(m, ensure_ascii=False).lower()
            if "knock" in blob or "ko" in str(m.get("instrumentName", "")).lower().split() or "KNOCK" in str(m.get("instrumentType", "")).upper():
                kos.append(m)
    types = {}
    for e in seen:
        pass
    try:
        roots = cap.call("GET", "/marketnavigation").get("nodes") or []
        log("📂 أقسام الأسواق: " + " | ".join(f"{n.get('name')}" for n in roots))
    except Exception as e:
        log("أقسام الأسواق: خطأ", str(e)[:100])
    log(f"🎯 مرشّحات KO: {len(kos)}")
    for m in kos[:5]:
        try:
            d = cap.call("GET", f"/markets/{m['epic']}")
            ins, rules, snap = d.get("instrument", {}), d.get("dealingRules", {}), d.get("snapshot", {})
            log(f"   تفاصيل {m['epic']}: type={ins.get('type')} margin={ins.get('marginFactor')}{ins.get('marginFactorUnit', '')} "
                f"minSize={rules.get('minDealSize')} bid/offer={snap.get('bid')}/{snap.get('offer')} status={snap.get('marketStatus')}")
            log("   حقول instrument: " + ", ".join(sorted(ins.keys())))
        except Exception as e:
            log(f"   تفاصيل {m.get('epic')}: خطأ {str(e)[:100]}")
    if not kos:
        log("النتيجة: لم تظهر منتجات KO عبر البحث. غالباً غير متاحة عبر هذا الـ API (أو تحتاج نوع حساب/نقطة وصول أخرى).")
    else:
        log("النتيجة: ظهرت منتجات KO — يمكن بحث إمكانية التداول بها عبر API.")


if __name__ == "__main__":
    main()
