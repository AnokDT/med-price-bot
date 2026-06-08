"""
Telegram bot: compares medicine prices across 10 major Indian e-pharmacies
Commands
    /start  – help
    /price <medicine>
Dependencies (already in requirements.txt)
    aiohttp, lxml, rapidfuzz, python-telegram-bot==20.*
Environment variables
    BOT_TOKEN      – the token from @BotFather   (required)
    PRACTO_TOKEN   – optional bearer token, see fetch_practo()
"""
import os, asyncio, re, html, json, time, aiohttp, lxml.html
from rapidfuzz import fuzz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

HEADERS = {"User-Agent": "Mozilla/5.0 (MedPriceBot/1.0; +https://t.me/yourbot)"}
TIMEOUT = aiohttp.ClientTimeout(total=25)
RATE_LIMIT = asyncio.Semaphore(4)          # polite: max 4 concurrent requests
CACHE_TTL = 300                            # 5-minute in-memory cache
_cache: dict[str, tuple[float, list]] = {} # {query: (timestamp, data)}

# ───────────────────────────── Helper utilities
async def get_json(session, url, **kw):
    async with RATE_LIMIT, session.get(url, headers=HEADERS, **kw) as r:
        print("DBG", r.method, r.status, url[:60], flush=True)   # <─ add
        return await r.json()

async def get_text(session, url, **kw):
    async with RATE_LIMIT, session.get(url, headers=HEADERS, **kw) as r:
        print("DBG", r.method, r.status, url[:60], flush=True)   # <─ add
        return await r.text()

def money(txt: str) -> float:
    """extract first number like ₹23.50 -> 23.5"""
    m = re.search(r"\d+[\d.,]*", txt.replace(",", ""))
    return float(m.group(0)) if m else 0.0

# ───────────────────────────── Site-specific fetchers
async def fetch_tata1mg(s, q):
    url = f"https://www.1mg.com/search/v2?name={q}&size=10&page=1"
    data = await get_json(s, url)
    return [{"store": "Tata 1mg",
             "name": p["name"],
             "price": float(p["price"]),
             "url": "https://www.1mg.com" + p["slug"]}
            for p in data.get("data", {}).get("products", [])]

async def fetch_apollo(s, q):
    url = f"https://www.apollopharmacy.in/search-medicines/{q.replace(' ','-')}"
    root = lxml.html.fromstring(await get_text(s, url))
    out = []
    for card in root.cssselect("div.product-card"):
        name = card.cssselect("h3")[0].text_content().strip()
        price = money(card.cssselect("span.productCard__price")[0].text_content())
        link  = "https://www.apollopharmacy.in" + card.cssselect("a")[0].get("href")
        out.append({"store": "Apollo", "name": name, "price": price, "url": link})
    return out

async def fetch_pharmeasy(s, q):
    url = "https://pharmeasy.in/api/otc/v2/search/sku"
    payload = {"query": q, "page": 1, "size": 10}
    async with RATE_LIMIT, s.post(url, json=payload, headers=HEADERS) as r:
        data = await r.json()
    return [{"store": "PharmEasy",
             "name": p["name"],
             "price": float(p["price"]),
             "url": "https://pharmeasy.in" + p["urlSlug"]}
            for p in data.get("data", {}).get("products", [])]

async def fetch_netmeds(s, q):
    url = f"https://www.netmeds.com/prescriptions/search?q={q}"
    root = lxml.html.fromstring(await get_text(s, url))
    out = []
    for card in root.cssselect("div.list-card"):
        name = card.cssselect("a")[0].get("title")
        price = money(card.cssselect("span.final-price")[0].text_content())
        link  = "https://www.netmeds.com" + card.cssselect("a")[0].get("href")
        out.append({"store": "NetMeds", "name": name, "price": price, "url": link})
    return out

async def fetch_flipkart(s, q):
    url = ("https://www.flipkart.com/api/4/page/fetch"
           "?pageUri=/search?q=" + q.replace(" ", "%20"))
    data = await get_json(s, url)
    cards = (data.get("pageData", {}).get("page", {})
                .get("data", {}).get("10002", []))
    out = []
    for c in cards:
        if c.get("type") != "product":
            continue
        info = c["metadata"]["productInfo"]["value"]
        out.append({"store": "Flipkart Health+",
                    "name": info["title"],
                    "price": float(info["pricing"]["final_price"]["value"]),
                    "url": "https://www.flipkart.com" + info["url"]})
    return out

async def fetch_medplus(s, q):
    url = f"https://www.medplusmart.com/productSearch?searchTerm={q}"
    root = lxml.html.fromstring(await get_text(s, url))
    out = []
    for li in root.cssselect("li.med-prod"):
        name  = li.cssselect("a")[0].get("title")
        price = money(li.cssselect("span.price")[0].text_content())
        link  = "https://www.medplusmart.com" + li.cssselect("a")[0].get("href")
        out.append({"store": "MedPlus", "name": name, "price": price, "url": link})
    return out

async def fetch_wellness(s, q):
    url = f"https://www.wellnessforever.com/api/search?term={q}"
    data = await get_json(s, url)
    return [{"store": "Wellness Forever",
             "name": p["name"],
             "price": float(p["selling_price"]),
             "url": "https://www.wellnessforever.com" + p["url"]}
            for p in data.get("data", [])]

async def fetch_truemeds(s, q):
    url = f"https://www.truemeds.in/api/v1/products/search?search={q}"
    data = await get_json(s, url)
    return [{"store": "TrueMeds",
             "name": p["product_name"],
             "price": float(p["our_price"]),
             "url": "https://www.truemeds.in" + p["url"]}
            for p in data.get("data", [])]

async def fetch_myra(s, q):
    url = f"https://www.myracare.in/v3/search?keyword={q}"
    data = await get_json(s, url)
    return [{"store": "Reliance (Myra)",
             "name": p["product_name"],
             "price": float(p["product_mrp"]["value"]),
             "url": "https://www.myracare.in" + p["product_url"]}
            for p in data.get("products", [])]

async def fetch_practo(s, q):
    token = os.getenv("PRACTO_TOKEN")
    if not token:
        return []          # silently skip if no token set
    headers = HEADERS | {"authorization": f"Bearer {token}"}
    url = f"https://pharmeasy.practo.com/search?name={q}&page=1&size=10"
    async with RATE_LIMIT, s.get(url, headers=headers) as r:
        data = await r.json()
    return [{"store": "Practo",
             "name": p["displayName"],
             "price": float(p["price"]),
             "url": p["deepLinkUrl"]}
            for p in data.get("data", [])]

# How to obtain PRACTO_TOKEN once:
#   1. open pharmeasy.practo.com in Chrome
#   2. F12 → Network → any request headers → copy the long "authorization: Bearer …"
#   3. export PRACTO_TOKEN="that-long-string" in Railway → Variables

FETCHERS = [
    fetch_tata1mg, fetch_apollo, fetch_pharmeasy, fetch_netmeds,
    fetch_flipkart, fetch_medplus, fetch_wellness, fetch_truemeds,
    fetch_myra, fetch_practo
]

# ───────────────────────────── Aggregator
async def aggregate(query: str) -> list:
    q = query.strip().lower()
    # simple 5-min cache to reduce load on sites
    if (q in _cache) and (time.time() - _cache[q][0] < CACHE_TTL):
        return _cache[q][1]

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        jobs = [f(session, q) for f in FETCHERS]
        results = await asyncio.gather(*jobs, return_exceptions=True)

    # flatten + drop failures
    raw = [item for sub in results if not isinstance(sub, Exception)
           for item in sub]

    # fuzzy de-duplicate
    out, seen = [], []
    for item in raw:
        key = item["name"].lower()
        if any(fuzz.token_set_ratio(key, s) > 90 for s in seen):
            continue
        seen.append(key)
        out.append(item)

    out.sort(key=lambda x: x["price"])
    _cache[q] = (time.time(), out)
    print("DBG total results", len(out), "for query", repr(query), flush=True)
    return out

# ───────────────────────────── Telegram layer
HELP = ("Send  /price <medicine name>\n"
        "Example: /price dolo 650\n"
        "I’ll return live prices from Tata 1mg, Apollo, PharmEasy, NetMeds, "
        "Flipkart Health+, and more.")

async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text(HELP)

async def cmd_price(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await upd.message.reply_text("Usage: /price <medicine>")
    query = " ".join(ctx.args)
    note = await upd.message.reply_text(f"Searching “{html.escape(query)}”…")
    try:
        data = await aggregate(query)
    except Exception as e:
        return await note.edit_text("⚠️ Error: " + str(e))

    if not data:
        return await note.edit_text("No stores returned a price.")

    cheapest = data[0]
    lines = [f"{d['store']}: ₹{d['price']}" for d in data[:10]]
    txt = "\n".join(lines) + f"\n\nLowest 👉 {cheapest['store']}\n{cheapest['url']}"
    await note.edit_text(txt)

# ───────────────────────────── Entrypoint
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN environment variable.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    print("Bot is running…")
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
