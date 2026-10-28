"""
Сповіщення про графік відключень ДТЕК (dtek-krem) для однієї черги.
Запускається GitHub Actions кожні 30 хв (одноразовий запуск, не постійна програма).

Що робить за один запуск:
  1. Відкриває https://www.dtek-krem.com.ua/ua/shutdowns браузером Chromium
     (сайт за захистом Incapsula) і бере window.DisconSchedule.fact.
  2. Якщо графік черги змінився або з'явився новий день — надсилає повідомлення.
  3. Раз на день (після DAILY_HOUR за Києвом) надсилає графік на сьогодні.
  4. Тримає закріплене повідомлення з актуальним графіком (дивитися будь-коли).
  5. Зберігає стан у state.json (workflow комітить його в репозиторій).

Формат fact: data -> {"<unix_ts 00:00 Київ>": {"GPV5.2": {"1".."24": стан}}}
"1" = 00:00–01:00. Стани: yes / no / first (нема перші 30 хв) / second (нема другі 30 хв).
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

URL = "https://www.dtek-krem.com.ua/ua/shutdowns"
KYIV = ZoneInfo("Europe/Kyiv")
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GROUP = os.getenv("GROUP", "5.2")
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "7"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
FAIL_ALERT_AFTER = 6  # 6 невдалих запусків поспіль (~3 год) -> одне попередження
GKEY = f"GPV{GROUP}"
TG_ERRORS: list[str] = []
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

ON, OFF, POSSIBLE = 0, 1, 2
HALVES = {
    "yes": (ON, ON), "no": (OFF, OFF), "first": (OFF, ON), "second": (ON, OFF),
    # у тижневому шаблоні preset; у fact не підтверджені
    "maybe": (POSSIBLE, POSSIBLE), "mfirst": (POSSIBLE, ON), "msecond": (ON, POSSIBLE),
}


# ---------- Telegram ----------
def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def send(text: str, silent=False) -> dict:
    r = tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML",
           disable_notification=str(silent).lower())
    if not r.get("ok"):
        print("Telegram error:", r, file=sys.stderr)
        TG_ERRORS.append(r.get("description", str(r)))
    return r


# ---------- ДТЕК ----------
HEADFUL = os.getenv("HEADFUL") == "1"          # у GitHub запускаємо "видимий" браузер під Xvfb
BROWSER_STATE = Path(os.getenv("BROWSER_STATE", ".cache/browser_state.json"))
MIRROR_URL = ("https://raw.githubusercontent.com/mrkaktuz/outages-data/data/dtek-krem.json")
SOURCE_USED = "сайт ДТЕК"

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['uk-UA', 'uk', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
if (!window.chrome) window.chrome = {runtime: {}};
"""

READ_FACT_JS = """() => {
  const f = window.DisconSchedule && window.DisconSchedule.fact;
  return f && f.data && Object.keys(f.data).length ? f : null;
}"""


def fact_from_html(html: str):
    """Запасний розбір: вирізати DisconSchedule.fact = {...} з HTML."""
    i = html.find("DisconSchedule.fact")
    if i < 0:
        return None
    i = html.find("{", i)
    depth, j, in_str, esc = 0, i, None, False
    while 0 <= j < len(html):
        c = html[j]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == in_str: in_str = None
        elif c in "\"'": in_str = c
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    f = json.loads(html[i:j + 1])
                    return f if f.get("data") else None
                except ValueError:
                    return None
        j += 1
    return None


async def fetch_from_site() -> dict:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not HEADFUL,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=UA, locale="uk-UA", timezone_id="Europe/Kyiv",
            viewport={"width": 1366, "height": 768},
            storage_state=str(BROWSER_STATE) if BROWSER_STATE.exists() else None)
        await ctx.add_init_script(STEALTH_JS)
        try:
            for attempt in range(1, 3):
                page = await ctx.new_page()
                try:
                    await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + 90
                    reloads = 0
                    while loop.time() < deadline:
                        fact = await page.evaluate(READ_FACT_JS)
                        if not fact:
                            fact = fact_from_html(await page.content())
                        if fact:
                            BROWSER_STATE.parent.mkdir(parents=True, exist_ok=True)
                            await ctx.storage_state(path=str(BROWSER_STATE))
                            print(f"Сайт ДТЕК: OK (спроба {attempt}, перезавантажень {reloads})")
                            return fact
                        html = (await page.content()).lower()
                        stub = "incapsula" in html or "imperva" in html or len(html) < 5000
                        await page.wait_for_timeout(3000 if stub else 1500)
                        if stub:
                            reloads += 1
                            await page.reload(wait_until="domcontentloaded", timeout=60_000)
                    print(f"Сайт ДТЕК: спроба {attempt} — дані не з'явились за 90 с", file=sys.stderr)
                except Exception as e:
                    print(f"Сайт ДТЕК: спроба {attempt} — {e}", file=sys.stderr)
                finally:
                    await page.close()
                await asyncio.sleep(5)
        finally:
            await browser.close()
    raise RuntimeError("сайт ДТЕК не віддав графік")


def fetch_from_mirror() -> dict:
    """Публічна копія тих самих даних ДТЕК (github.com/mrkaktuz/outages-data)."""
    with urllib.request.urlopen(MIRROR_URL, timeout=30) as r:
        d = json.load(r)
    fact = (d.get("raw") or {}).get("fact") or {}
    if not (d.get("status") or {}).get("ok") or not fact.get("data"):
        raise RuntimeError("дзеркало без даних")
    print(f"Дзеркало: OK (ДТЕК оновив {fact.get('update')})")
    return fact


async def fetch_fact() -> dict:
    global SOURCE_USED
    try:
        return await fetch_from_site()
    except Exception as e:
        print(f"{e}; пробую запасне джерело", file=sys.stderr)
    fact = await asyncio.to_thread(fetch_from_mirror)
    SOURCE_USED = "копія даних ДТЕК (outages-data)"
    return fact


# ---------- обробка ----------
def day_slots(hours: dict) -> list[int]:
    out = []
    for h in range(1, 25):
        out.extend(HALVES.get(hours.get(str(h), "yes"), (POSSIBLE, POSSIBLE)))
    return out


def extract_group(fact: dict) -> dict[str, list[int]]:
    midnight = int(datetime.now(KYIV).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today = max(int(fact.get("today", 0)), midnight)
    days = {str(int(ts)): day_slots(g[GKEY])
            for ts, g in fact["data"].items() if int(ts) >= today and GKEY in g}
    return dict(sorted(days.items(), key=lambda kv: int(kv[0])))


def intervals(slots):
    res, i = [], 0
    while i < 48:
        if slots[i] == ON:
            i += 1
            continue
        j = i
        while j < 48 and slots[j] == slots[i]:
            j += 1
        res.append((i, j, slots[i]))
        i = j
    return res


def hm(i): return f"{i // 2:02d}:{30 * (i % 2):02d}"


def dur(n):
    h, m = divmod(n * 30, 60)
    return " ".join(x for x in (f"{h} год" if h else "", f"{m} хв" if m else "") if x)


def day_date(ts: str):
    return datetime.fromtimestamp(int(ts), KYIV).date()


def format_day(ts: str, slots: list[int]) -> str:
    d = day_date(ts)
    word = {0: "Сьогодні", 1: "Завтра"}.get((d - datetime.now(KYIV).date()).days)
    label = f"{word}, {d:%d.%m.%Y}" if word else f"{d:%d.%m.%Y}"
    lines = [f"📅 <b>{label}</b>"]
    ivs = intervals(slots)
    if not ivs:
        lines.append("✅ Відключень не заплановано")
    for a, b, k in ivs:
        lines.append(f"{'🔴' if k == OFF else '🟡 можливо'} {hm(a)}–{hm(b)} ({dur(b - a)})")
    off = sum(s == OFF for s in slots)
    if off:
        lines.append(f"Без світла: {dur(off)}")
    return "\n".join(lines)


# ---------- стан ----------
def load_state() -> dict:
    st = {"days": {}, "update": None, "pinned_id": None, "pinned_text": None,
          "daily_sent": None, "fails": 0}
    if STATE_FILE.exists():
        st.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return st


def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def update_pinned(st: dict, text: str):
    if text == st.get("pinned_text"):
        return
    if st.get("pinned_id"):
        r = tg("editMessageText", chat_id=CHAT_ID, message_id=st["pinned_id"],
               text=text, parse_mode="HTML")
        if r.get("ok") or "not modified" in r.get("description", ""):
            st["pinned_text"] = text
            return
    r = send(text, silent=True)
    if r.get("ok"):
        mid = r["result"]["message_id"]
        tg("pinChatMessage", chat_id=CHAT_ID, message_id=mid, disable_notification="true")
        st["pinned_id"], st["pinned_text"] = mid, text


# ---------- головне ----------
def main():
    st = load_state()
    try:
        fact = asyncio.run(fetch_fact())
    except Exception as e:
        print(e, file=sys.stderr)
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] == FAIL_ALERT_AFTER:
            send("⚠️ Вже ~3 год не вдається отримати графік із сайту ДТЕК. "
                 "Показую останній відомий (закріплене повідомлення).")
        save_state(st)
        print("::error::Ні сайт ДТЕК, ні запасне джерело не віддали графік (див. лог вище)")
        sys.exit(1)
    if st.get("fails", 0) >= FAIL_ALERT_AFTER:
        send("✅ Зв'язок із сайтом ДТЕК відновлено.", silent=True)
    st["fails"] = 0

    upd = fact.get("update", "N/A")
    days = extract_group(fact)

    # 1) нові дні та зміни
    for ts, slots in days.items():
        old = st["days"].get(ts)
        if old != slots:
            head = "🆕 Новий графік" if old is None else "🔄 Графік змінено"
            send(f"{head} — черга {GROUP}\n\n{format_day(ts, slots)}\n\nДТЕК оновив: {upd}\nДжерело: {SOURCE_USED}")

    # 2) щоденне повідомлення
    now = datetime.now(KYIV)
    today = now.date().isoformat()
    if now.hour >= DAILY_HOUR and st.get("daily_sent") != today:
        tday = next((ts for ts in days if day_date(ts) == now.date()), None)
        body = format_day(tday, days[tday]) if tday else "Графік на сьогодні: N/A (ДТЕК не опублікував)"
        send(f"☀️ Графік на сьогодні — черга {GROUP}\n\n{body}")
        st["daily_sent"] = today

    # 3) закріплене повідомлення
    body = "\n\n".join(format_day(ts, s) for ts, s in days.items()) or "Даних немає (N/A)"
    update_pinned(st, f"📌 <b>Актуальний графік — черга {GROUP}</b>\n\n{body}\n\nДТЕК оновив: {upd}\nДжерело: {SOURCE_USED}")

    if TG_ERRORS:
        # не запам'ятовуємо графік, щоб наступний запуск надіслав його повторно
        print(f"::error::Telegram не прийняв повідомлення: {TG_ERRORS[0]}. "
              "Перевірте секрети BOT_TOKEN і CHAT_ID та чи натиснули ви /start у боті.")
        save_state(st)
        sys.exit(1)
    st["days"], st["update"] = days, upd
    save_state(st)
    print(f"OK: черга {GROUP}, днів у графіку: {len(days)}, ДТЕК оновив: {upd}")


if __name__ == "__main__":
    main()
