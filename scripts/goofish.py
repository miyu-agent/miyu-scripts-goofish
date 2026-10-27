#!/usr/bin/env python3
# Display name: Goofish
# 显示名称：闲鱼
# Description: Search Goofish (闲鱼) second-hand listings without login, or watch a keyword for new listings; a search drives a headless browser and takes 10-40 s.
# 描述：免登录搜闲鱼二手：价格、成色、地区、想要人数和链接；也能盯住一个关键词看有没有新上的。
# Timeout: 300
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "action": {"type": "string", "enum": ["search", "monitor-init", "monitor-query", "monitor-list", "monitor-remove", "doctor"], "description": "search (default): browser search, 10-40 s. monitor-init: one browser run that saves a watch for the keyword. monitor-query: re-run a saved watch over plain HTTP in seconds and mark listings not seen before. monitor-list / monitor-remove: manage watches. doctor: check playwright and the browser."},
#   "keyword": {"type": "string", "description": "What to search, e.g. 'RTX 4060'. Required except for monitor-list and doctor."},
#   "pages": {"type": "integer", "description": "search only: result pages to fetch, 1-5, 30 listings each; every extra page adds about 10 s. Default 1."},
#   "sort": {"type": "string", "enum": ["default", "price_asc", "price_desc", "newest", "most_wanted"], "description": "Order of the fetched listings. Sorting and price filters apply to what was fetched, not the whole site. Default keeps Goofish's order."},
#   "min_price": {"type": "number", "description": "Drop listings cheaper than this, in CNY."},
#   "max_price": {"type": "number", "description": "Drop listings dearer than this, in CNY."},
#   "limit": {"type": "integer", "description": "At most this many listings in the output, 1-150. Default 30."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."}
# }}
"""Goofish (闲鱼) search for Miyu, as one script.

The search page signs its API calls with tokens a plain HTTP client cannot
reproduce (fresh signatures from outside the page are refused with RGV587), so
search drives a headless Chromium and captures the page's own API responses.
A watch (monitor-init) stores that captured request; monitor-query replays it
byte for byte over urllib, which works for a while without a browser.

Watches and their seen ids live in MIYU_SCRIPT_STATE_DIR (kept per account,
survives reboots); the browser session snapshot is a cache in
MIYU_SCRIPT_CACHE_DIR. It writes nowhere else.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
APP_KEY = "34839810"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SESSION_TTL = 25 * 60
FIRST_PAGE_WAIT = 40
NEXT_PAGE_WAIT = 12
BUDGET = 240  # seconds of browser work before giving up on further pages
MAX_PAGES = 5
MAX_LIMIT = 150
SORTS = ("default", "price_asc", "price_desc", "newest", "most_wanted")
ACTIONS = ("search", "monitor-init", "monitor-query", "monitor-list", "monitor-remove", "doctor")

STARTED = time.monotonic()


class Fail(Exception):
    def __init__(self, error, fix=""):
        super().__init__(error)
        self.error = error
        self.fix = fix


# ------------------------------------------------------------------ storage

CACHE = os.environ.get("MIYU_SCRIPT_CACHE_DIR") or ""


def cache_dir():
    """Where state lives. Without MIYU_SCRIPT_CACHE_DIR a throwaway temp dir: search works, watches don't."""
    global CACHE
    if not CACHE:
        CACHE = tempfile.mkdtemp(prefix="goofish-")
        cache_dir.temporary = True
    os.makedirs(CACHE, exist_ok=True)
    return CACHE


cache_dir.temporary = False


def state_dir():
    """监控存在这里：按账号分开、重启还在。"""
    return os.environ.get("MIYU_SCRIPT_STATE_DIR") or ""


def need_persistent_cache():
    if not state_dir():
        raise Fail("没有状态目录（MIYU_SCRIPT_STATE_DIR 没设），监控存不下来",
                   "在 Miyu 的会话里调用；手动跑时自己设 MIYU_SCRIPT_STATE_DIR")


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def watch_key(keyword):
    return hashlib.sha1(keyword.strip().lower().encode()).hexdigest()[:16]


def watch_path(keyword):
    return os.path.join(state_dir(), "watches", watch_key(keyword) + ".json")


def session_path():
    return os.path.join(cache_dir(), "session.json")


# ------------------------------------------------------------------ browser

def find_browser(chromium):
    """Launch options for the first usable Chromium: playwright's own build, else a system one."""
    try:
        bundled = chromium.executable_path
    except Exception:
        bundled = ""
    if bundled and os.path.exists(bundled):
        return {}, f"playwright 自带的 Chromium（{bundled}）"
    names = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
             "microsoft-edge-stable", "microsoft-edge", "brave", "brave-browser")
    for name in names:
        path = shutil.which(name)
        if path:
            return {"executable_path": path}, f"系统的 {name}（{path}）"
    fixed = []
    if sys.platform == "darwin":
        fixed = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    elif os.name == "nt":
        for base in filter(None, (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                                  os.environ.get("LOCALAPPDATA"))):
            fixed += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    for path in fixed:
        if os.path.exists(path):
            return {"executable_path": path}, path
    return None, ""


INSTALL_FIX = ("装 playwright：`pip install --user playwright`（Arch：`pacman -S python-playwright`）；"
               "再给它一个 Chromium：`python3 -m playwright install chromium`，或者装系统的 Chromium / Chrome / Edge。"
               "装好后用 action=doctor 复查")


def load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Fail("没装 playwright，搜索要用它开无头浏览器", INSTALL_FIX)
    return sync_playwright


def launch(p):
    options, _ = find_browser(p.chromium)
    if options is None:
        raise Fail("找不到能用的 Chromium 系浏览器", INSTALL_FIX)
    args = ["--disable-dev-shm-usage"]  # /dev/shm is not writable in Miyu's sandbox
    if sys.platform.startswith("linux"):
        args.append("--no-sandbox")  # Chromium's own sandbox cannot nest inside Miyu's
    # Miyu's sandbox covers ~/.config/chromium (and Chrome's) with an empty read-only folder; the crash
    # handler cannot make its folder there and the browser dies with SIGTRAP. Give it a home of its own.
    home = os.path.join(cache_dir(), "browser-home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, XDG_CONFIG_HOME=home, XDG_CACHE_HOME=home)
    try:
        return p.chromium.launch(headless=True, args=args, env=env, **options)
    except Exception as exc:
        raise Fail(f"浏览器起不来：{str(exc).splitlines()[0][:300]}",
                   "用 action=doctor 看是哪一环；缺系统库的照发行版装 Chromium 的依赖")


def progress(text):
    """进度行：Miyu 边跑边给人看，不进交给 AI 的结果。"""
    print(f"miyu-progress: {text}", file=sys.stderr, flush=True)


def browser_search(keyword, pages):
    """Drive the search page; return (payloads by page, captured first request, total found)."""
    sync_playwright = load_playwright()
    payloads = {}
    captured = {}
    with sync_playwright() as p:
        browser = launch(p)
        session = read_json(session_path())
        state = None
        if session and time.time() - session.get("saved_at", 0) < SESSION_TTL:
            state = session.get("state")
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, locale="zh-CN",
                                  timezone_id="Asia/Shanghai", storage_state=state)
        page = ctx.new_page()
        pending = {}

        def on_request(req):
            if SEARCH_API + "/1.0" not in req.url:
                return
            try:
                data = urllib.parse.parse_qs(req.post_data or "")["data"][0]
                number = int(json.loads(data).get("pageNumber", 1))
            except (KeyError, ValueError, TypeError):
                number = 1
            pending[req.url] = number
            if number == 1:
                captured.update({"url": req.url, "post_data": req.post_data,
                                 "user_agent": req.headers.get("user-agent", UA),
                                 "cookies": {c["name"]: c["value"] for c in ctx.cookies()}})

        def on_response(resp):
            if SEARCH_API + "/1.0" not in resp.url:
                return
            number = pending.pop(resp.url, 1)
            try:
                text = resp.text()
            except Exception:
                return
            if '"SUCCESS' in text:
                try:
                    payloads[number] = json.loads(text)
                except ValueError:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)
        progress("打开闲鱼搜索页")
        try:
            page.goto("https://www.goofish.com/search?q=" + urllib.parse.quote(keyword),
                      timeout=60000, wait_until="domcontentloaded")
        except Exception as exc:
            browser.close()
            raise Fail(f"打不开闲鱼：{str(exc).splitlines()[0][:200]}", "检查网络后重试")

        # The first load usually trips risk control or a login popup; close it and search again.
        deadline = time.monotonic() + FIRST_PAGE_WAIT
        progress("等第一页结果（碰上风控弹窗会关掉重搜）")
        page.wait_for_timeout(2000)
        while 1 not in payloads and time.monotonic() < deadline:
            try:
                close = page.locator(".login-pop-close")
                if close.count() and close.first.is_visible():
                    close.first.click(timeout=2000)
            except Exception:
                pass
            try:
                box = page.locator("input").first
                box.fill(keyword)
                box.press("Enter")
            except Exception:
                pass
            waited = 0
            while 1 not in payloads and waited < 3000 and time.monotonic() < deadline:
                page.wait_for_timeout(500)
                waited += 500

        got = 1 if 1 in payloads else 0
        while got and got < pages and time.monotonic() - STARTED < BUDGET:
            info = payloads[got].get("data", {}).get("resultInfo", {})
            if not info.get("hasNextPage"):
                break
            target = got + 1
            progress(f"翻到第 {target}/{pages} 页")
            try:
                page.wait_for_selector("[class*='search-pagination-page-box']", timeout=5000)
                button = page.locator("[class*='search-pagination-page-box']").filter(
                    has_text=re.compile(f"^{target}$"))
                if not button.count():
                    break
                button.first.evaluate("e => e.click()")
            except Exception:
                break
            until = time.monotonic() + NEXT_PAGE_WAIT
            while target not in payloads and time.monotonic() < until:
                page.wait_for_timeout(500)
            if target not in payloads:
                break
            got = target

        if 1 in payloads:
            try:
                write_json(session_path(), {"saved_at": int(time.time()), "state": ctx.storage_state()})
            except OSError:
                pass
        browser.close()

    if 1 not in payloads:
        raise Fail("闲鱼的风控拦下了这次搜索（RGV587「被挤爆啦」），或者没等到结果",
                   "等 3 分钟左右再试；连着失败的话换个时段")
    total = (payloads[1].get("data", {}).get("resultInfo", {})
             .get("searchResControlFields", {}).get("numFound"))
    return payloads, captured, total


# ------------------------------------------------------------------ listings

def number(text):
    try:
        return float(str(text).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return None


def parse_price(parts):
    digits, unit = "", 1
    for part in parts or []:
        text = part.get("text", "")
        if part.get("type") == "integer":
            digits = text
        elif part.get("type") == "decimal":
            digits += text
        elif part.get("type") == "unit" and text == "万":
            unit = 10000
    value = number(digits) if digits else None
    return None if value is None else round(value * unit, 2)


def listing(row):
    item = row.get("data", {}).get("item", {})
    main = item.get("main", {})
    ex = main.get("exContent", {})
    args = main.get("clickParam", {}).get("args", {})
    item_id = str(ex.get("itemId") or args.get("item_id") or "")
    if not item_id:
        return None
    price = parse_price(ex.get("price"))
    if price is None:
        price = number(args.get("price") or "")
    tags, want = [], None
    for line in ("r2", "r3", "r4"):
        for tag in ex.get("fishTags", {}).get(line, {}).get("tagList", []):
            text = str(tag.get("data", {}).get("content") or "").strip()
            wanted = re.fullmatch(r"(\d+)人想要", text)
            if wanted:
                want = int(wanted.group(1))
            elif text and not text.startswith("¥") and not re.search(r"icon", text, re.I):
                tags.append(text)
    published = None
    try:
        published = time.strftime("%Y-%m-%d", time.localtime(int(args["publishTime"]) / 1000))
    except (KeyError, ValueError, TypeError, OSError):
        pass
    return {
        "id": item_id,
        "title": re.sub(r"\s+", " ", str(ex.get("title") or "")).strip(),
        "price": price,
        "original_price": number(ex.get("oriPrice")) if ex.get("oriPrice") else None,
        "area": ex.get("area") or "",
        "want": want,
        "tags": tags,
        "published": published,
        "seller": ex.get("userNickName") or "",
        "image": ex.get("picUrl") or "",
        "url": f"https://www.goofish.com/item?id={item_id}",
        "ad": bool(ex.get("isAliMaMaAD")),
    }


def listings(payloads):
    out, seen = [], set()
    for page_no in sorted(payloads):
        for row in payloads[page_no].get("data", {}).get("resultList") or []:
            item = listing(row)
            if item and not item["ad"] and item["id"] not in seen:
                seen.add(item["id"])
                out.append(item)
    return out


def arrange(items, args):
    low, high = args.get("min_price"), args.get("max_price")
    if low is not None:
        items = [i for i in items if i["price"] is not None and i["price"] >= low]
    if high is not None:
        items = [i for i in items if i["price"] is not None and i["price"] <= high]
    sort = args["sort"]
    inf = float("inf")
    if sort == "price_asc":
        items = sorted(items, key=lambda i: i["price"] if i["price"] is not None else inf)
    elif sort == "price_desc":
        items = sorted(items, key=lambda i: -(i["price"] or 0))
    elif sort == "newest":
        items = sorted(items, key=lambda i: i["published"] or "", reverse=True)
    elif sort == "most_wanted":
        items = sorted(items, key=lambda i: -(i["want"] or 0))
    return items


# ------------------------------------------------------------------ watches

def replay(ticket):
    """Re-send the captured first-page request as it was. Fresh signatures are refused, an exact replay is not."""
    req = ticket["request"]
    cookie = "; ".join(f"{k}={v}" for k, v in req["cookies"].items())
    headers = {"User-Agent": req.get("user_agent") or UA, "Referer": "https://www.goofish.com/",
               "Origin": "https://www.goofish.com", "Accept": "application/json",
               "Accept-Language": "zh-CN", "Content-Type": "application/x-www-form-urlencoded",
               "Cookie": cookie}
    body = (req.get("post_data") or "").encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(req["url"], data=body, headers=headers),
                                    timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Fail(f"请求闲鱼失败：{exc}", "检查网络后重试")
    ret = (data.get("ret") or [""])[0]
    if not ret.startswith("SUCCESS"):
        raise Fail(f"监控的请求被拒了（{ret[:80]}），保存的会话大概过期了",
                   "用 action=monitor-init 重新开这个关键词的监控")
    return {1: data}


def watches():
    out = []
    folder = os.path.join(state_dir(), "watches") if state_dir() else ""
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            ticket = read_json(os.path.join(folder, name)) if name.endswith(".json") else None
            if ticket and ticket.get("keyword"):
                out.append(ticket)
    return out


# ------------------------------------------------------------------ actions

def do_search(args):
    payloads, _, total = browser_search(args["keyword"], args["pages"])
    items = listings(payloads)
    return {"action": "search", "keyword": args["keyword"], "total_found": total,
            "pages": len(payloads), "fetched": len(items), "items": arrange(items, args)}


def do_monitor_init(args):
    need_persistent_cache()
    payloads, captured, total = browser_search(args["keyword"], 1)
    if not captured.get("url"):
        raise Fail("搜到了结果，但没截到搜索请求，开不了监控", "再试一次 monitor-init")
    items = listings(payloads)
    write_json(watch_path(args["keyword"]), {
        "keyword": args["keyword"], "created": int(time.time()), "last_query": None,
        "request": captured, "seen": [i["id"] for i in items]})
    return {"action": "monitor-init", "keyword": args["keyword"], "total_found": total,
            "pages": 1, "fetched": len(items), "items": arrange(items, args),
            "note": "监控开好了：之后用 monitor-query 几秒就能查，没见过的会标出来"}


def do_monitor_query(args):
    need_persistent_cache()
    path = watch_path(args["keyword"])
    ticket = read_json(path)
    if not ticket:
        raise Fail(f"「{args['keyword']}」还没开监控", "先用 action=monitor-init 开，关键词要一字不差")
    payloads = replay(ticket)
    items = listings(payloads)
    total = (payloads[1].get("data", {}).get("resultInfo", {})
             .get("searchResControlFields", {}).get("numFound"))
    seen = set(ticket.get("seen") or [])
    for item in items:
        item["new"] = item["id"] not in seen
    fresh = [i for i in items if i["new"]]
    ticket["seen"] = list(seen | {i["id"] for i in items})[-3000:]
    previous = ticket.get("last_query")
    ticket["last_query"] = int(time.time())
    write_json(path, ticket)
    arranged = arrange(items, args)
    arranged = [i for i in arranged if i["new"]] + [i for i in arranged if not i["new"]]
    since = time.strftime("%m-%d %H:%M", time.localtime(previous or ticket["created"]))
    return {"action": "monitor-query", "keyword": args["keyword"], "total_found": total, "pages": 1,
            "fetched": len(items),
            "new": len(fresh), "since": since, "items": arranged}


def do_monitor_list(args):
    need_persistent_cache()
    rows = []
    for ticket in watches():
        rows.append({"keyword": ticket["keyword"],
                     "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(ticket.get("created", 0))),
                     "last_query": time.strftime("%Y-%m-%d %H:%M", time.localtime(ticket["last_query"]))
                     if ticket.get("last_query") else None,
                     "seen": len(ticket.get("seen") or [])})
    return {"action": "monitor-list", "watches": rows}


def do_monitor_remove(args):
    need_persistent_cache()
    path = watch_path(args["keyword"])
    if not os.path.exists(path):
        raise Fail(f"「{args['keyword']}」没有监控", "用 action=monitor-list 看有哪些")
    os.remove(path)
    return {"action": "monitor-remove", "keyword": args["keyword"], "removed": True}


def do_doctor(args):
    checks = []

    def check(name, ok, detail, warn=False):
        checks.append({"name": name, "ok": ok, "warn": warn, "detail": detail})

    check("python", sys.version_info >= (3, 8), sys.version.split()[0])
    try:
        sync_playwright = load_playwright()
        import playwright
        check("playwright", True, getattr(playwright, "__version__", "") or "已装")
    except Fail:
        sync_playwright = None
        check("playwright", False, "没装：" + INSTALL_FIX)
    if sync_playwright:
        with sync_playwright() as p:
            options, where = find_browser(p.chromium)
            check("浏览器", options is not None, where or "没找到：" + INSTALL_FIX)
            if options is not None:
                try:
                    started = time.monotonic()
                    browser = launch(p)
                    page = browser.new_page()
                    page.set_content("<p>ok</p>")
                    browser.close()
                    check("无头启动", True, f"起得来，{time.monotonic() - started:.1f} 秒")
                except Fail as exc:
                    check("无头启动", False, exc.error)
    cached = bool(os.environ.get("MIYU_SCRIPT_CACHE_DIR"))
    check("缓存目录", True, cache_dir() if cached else "没设 MIYU_SCRIPT_CACHE_DIR：每次搜索都重开浏览器会话",
          warn=not cached)
    if cached:
        session = read_json(session_path())
        if session:
            age = int(time.time() - session.get("saved_at", 0))
            check("浏览器会话", True, f"{age // 60} 分钟前存的" + ("（已过期，下次搜索重建）" if age > SESSION_TTL else ""))
        else:
            check("浏览器会话", True, "还没有，第一次搜索后建")
    persistent = bool(state_dir())
    check("状态目录", True, state_dir() if persistent else "没设 MIYU_SCRIPT_STATE_DIR：能搜，监控不可用",
          warn=not persistent)
    if persistent:
        check("监控", True, f"{len(watches())} 个")
    return {"action": "doctor", "ok": all(c["ok"] for c in checks), "checks": checks}


# ------------------------------------------------------------------ output

def money(value):
    if value is None:
        return "?"
    return f"¥{value:,.0f}" if float(value).is_integer() else f"¥{value:,.2f}"


def render_items(result, limit):
    items = result["items"][:limit]
    lines = []
    for n, item in enumerate(items, 1):
        bits = [money(item["price"]) + (f"（原 {money(item['original_price'])}）" if item["original_price"] else "")]
        if item["area"]:
            bits.append(item["area"])
        if item["want"]:
            bits.append(f"{item['want']}人想要")
        if item["published"]:
            bits.append(item["published"])
        if item["tags"]:
            bits.append("/".join(item["tags"][:4]))
        title = item["title"] if len(item["title"]) <= 60 else item["title"][:60] + "…"
        mark = "🆕 " if item.get("new") else ""
        lines.append(f"{n}. {mark}{' · '.join(bits)}\n   {title}  {item['url']}")
    return lines


def render_md(result, args):
    action = result["action"]
    if action == "doctor":
        lines = ["闲鱼环境自检：" + ("全部正常" if result["ok"] else "有问题")]
        lines += [f"- {'❌' if not c['ok'] else '⚠️' if c['warn'] else '✅'} {c['name']}：{c['detail']}"
                  for c in result["checks"]]
        return "\n".join(lines)
    if action == "monitor-list":
        if not result["watches"]:
            return "还没有监控。用 action=monitor-init 给关键词开一个。"
        lines = [f"{len(result['watches'])} 个监控："]
        for w in result["watches"]:
            last = f"上次查 {w['last_query']}" if w["last_query"] else "还没查过"
            lines.append(f"- 「{w['keyword']}」开于 {w['created']}，{last}，见过 {w['seen']} 条")
        return "\n".join(lines)
    if action == "monitor-remove":
        return f"已删掉「{result['keyword']}」的监控。"

    limit = args["limit"]
    shown = min(limit, len(result["items"]))
    head = f"闲鱼「{result['keyword']}」"
    if result.get("total_found"):
        head += f"：全站约 {int(result['total_found']):,} 条"
    head += f"，取了 {result['pages']} 页 {result['fetched']} 条（已去掉广告）"
    filters = []
    if args.get("min_price") is not None or args.get("max_price") is not None:
        filters.append(f"价格 {money(args.get('min_price')) if args.get('min_price') is not None else '不限'}"
                       f"–{money(args.get('max_price')) if args.get('max_price') is not None else '不限'}")
    if args["sort"] != "default":
        filters.append({"price_asc": "价格从低到高", "price_desc": "价格从高到低",
                        "newest": "发布从新到旧", "most_wanted": "想要的人从多到少"}[args["sort"]])
    if filters:
        head += "，" + "、".join(filters) + f"，剩 {len(result['items'])} 条"
    head += f"，列出 {shown} 条"
    lines = [head]
    if action == "monitor-query":
        lines.append(f"自 {result['since']} 以来新出现 {result['new']} 条" + ("，排在前面、标了 🆕" if result["new"] else ""))
    if action == "monitor-init":
        lines.append(result["note"])
    lines += render_items(result, limit)
    if not shown:
        lines.append("（没有合条件的）")
    return "\n".join(lines)


def emit(result, args):
    if args.get("format") == "json":
        if "items" in result:
            result = dict(result, items=result["items"][:args["limit"]])
        print(json.dumps(dict({"ok": True}, **result), ensure_ascii=False))
    else:
        print(render_md(result, args))


# ------------------------------------------------------------------ main

def parse_args():
    try:
        raw = json.load(sys.stdin)
    except ValueError as exc:
        raise Fail(f"参数不是 JSON：{exc}", '例：{"keyword": "RTX 4060"}')
    if not isinstance(raw, dict):
        raise Fail("参数要是一个 JSON 对象", '例：{"keyword": "RTX 4060"}')
    args = {"format": raw.get("format") if raw.get("format") in ("md", "json") else "md"}
    action = raw.get("action") or "search"
    if action not in ACTIONS:
        raise Fail(f"不认识的 action：{action}", "可选：" + "、".join(ACTIONS))
    args["action"] = action
    keyword = str(raw.get("keyword") or "").strip()
    if not keyword and action not in ("monitor-list", "doctor"):
        raise Fail(f"{action} 要给 keyword", '例：{"keyword": "RTX 4060"}')
    args["keyword"] = keyword

    def integer(name, default, low, high):
        value = raw.get(name, default)
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise Fail(f"{name} 要是整数", f"{name} 取 {low} 到 {high}")
        return max(low, min(high, int(value)))

    args["pages"] = integer("pages", 1, 1, MAX_PAGES)
    args["limit"] = integer("limit", 30, 1, MAX_LIMIT)
    sort = raw.get("sort") or "default"
    if sort not in SORTS:
        raise Fail(f"不认识的 sort：{sort}", "可选：" + "、".join(SORTS))
    args["sort"] = sort
    for name in ("min_price", "max_price"):
        value = raw.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise Fail(f"{name} 要是不小于 0 的数", f"例：{{\"{name}\": 1500}}")
        args[name] = value
    if (args["min_price"] is not None and args["max_price"] is not None
            and args["min_price"] > args["max_price"]):
        raise Fail("min_price 比 max_price 还大", "把两个换过来")
    return args


def main():
    args = {"format": "md"}
    try:
        args = parse_args()
        handler = {"search": do_search, "monitor-init": do_monitor_init, "monitor-query": do_monitor_query,
                   "monitor-list": do_monitor_list, "monitor-remove": do_monitor_remove,
                   "doctor": do_doctor}[args["action"]]
        result = handler(args)
        emit(result, args)
        if args["action"] == "doctor" and not result["ok"]:
            sys.exit(1)
    except Fail as exc:
        if args.get("format") == "json":
            print(json.dumps({"ok": False, "error": exc.error, "fix": exc.fix}, ensure_ascii=False))
        else:
            print(f"失败：{exc.error}" + (f"\n怎么办：{exc.fix}" if exc.fix else ""))
        sys.exit(1)
    finally:
        if cache_dir.temporary:
            shutil.rmtree(CACHE, ignore_errors=True)


if __name__ == "__main__":
    main()
