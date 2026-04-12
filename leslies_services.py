"""
Leslie's Pool portal integration.

Confirmed endpoints from HAR capture + waterTest.js analysis (Feb 2026):
  - Login POST: Account-Login?rurl=1
  - Fields: loginEmail, loginPassword, loginRememberMe, csrf_token

  Water test API flow (reverse-engineered from waterTest.js):
    1. GET WaterTest-Landing?poolProfileId=...&poolName=...  (sets session state)
    2. GET WaterTest-ProfileById?poolProfileId=...           (get pool_name)
    3. POST WaterTest-GetWaterTest (form-encoded, NOT JSON)
       data: poolProfileName=<name>&poolSanitizer=Saltwater&count=<n>
       response: {"success": true, "response": "<html table fragment>"}

  Key bugs to avoid:
    - GetWaterTest MUST be called with form-encoded data (jQuery $.ajax default)
    - WaterTest-Landing MUST be visited first to activate server-side session state
    - Table rows use <th> for date and PDF cells (not <td>) — use td|th xpath

Pulls:
  - Water test history (chemistry + salt + overall score)
  - Order history (chemicals purchased)
"""
import json
import logging
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from lxml import html

from config import LESLIES_EMAIL, LESLIES_PASSWORD, LESLIES_POOL_ID
from memory import DB_PATH, db_connect
import aiosqlite

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")

BASE           = "https://lesliespool.com"
LOGIN_SHOW     = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/Login-Show"
LOGIN_POST     = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/Account-Login"
CSRF_API       = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/CSRF-GenerateToken"
WATER_TEST     = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/WaterTest-Landing"
PROFILE_BY_ID  = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/WaterTest-ProfileById"
GET_WATER_TEST = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/WaterTest-GetWaterTest"
ORDER_HIST     = f"{BASE}/on/demandware.store/Sites-lpm_site-Site/en_US/Account-Orders"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE,
}

_NULL_VALUES = {"N/A", "", "-", "n/a", "None", "none", "null"}


class LesliesSession:
    """Authenticated httpx session for Leslie's Pool portal."""

    def __init__(self):
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers=_HEADERS,
        )
        await self._login()
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def _get_csrf(self) -> str:
        """Get a fresh CSRF token from Leslie's."""
        # Try dedicated CSRF token endpoint first (Demandware standard)
        try:
            r = await self.client.get(
                CSRF_API,
                headers={"Accept": "application/json, */*"},
            )
            if r.status_code == 200:
                data = r.json()
                token = data.get("csrf", {}).get("token") or data.get("token")
                if token:
                    logger.debug("Got CSRF via token endpoint")
                    return token
        except Exception:
            pass

        # Fall back: parse the Login-Show page for hidden csrf_token input
        r = await self.client.get(LOGIN_SHOW)
        tree = html.fromstring(r.text)
        for inp in tree.xpath('//input[@name="csrf_token"]'):
            token = inp.get("value", "")
            if token:
                logger.debug("Got CSRF from Login-Show HTML")
                return token

        # Last resort: check meta tag
        for meta in tree.xpath('//meta[@name="csrf-token"]'):
            token = meta.get("content", "")
            if token:
                return token

        logger.warning("Could not find CSRF token — proceeding without it")
        return ""

    async def _login(self):
        """Authenticate with Leslie's using email + password."""
        if not LESLIES_EMAIL or not LESLIES_PASSWORD:
            raise ValueError("LESLIES_EMAIL / LESLIES_PASSWORD not set in .env")

        # Warm up session cookies by hitting the main site
        await self.client.get(BASE)

        csrf = await self._get_csrf()

        payload = {
            "loginEmail":      LESLIES_EMAIL,
            "loginPassword":   LESLIES_PASSWORD,
            "loginRememberMe": "true",
        }
        if csrf:
            payload["csrf_token"] = csrf

        resp = await self.client.post(
            f"{LOGIN_POST}?rurl=1",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept":       "application/json, text/plain, */*",
                "Referer":      f"{BASE}/login",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp.raise_for_status()

        # Response is JSON: {"success": true, "redirectUrl": "..."}
        try:
            data = resp.json()
            if not data.get("success"):
                err = data.get("error", [data.get("errorMessage", "Unknown error")])
                raise RuntimeError(f"Leslie's login failed: {err}")
            logger.info("Leslie's login successful")
        except (json.JSONDecodeError, KeyError):
            # Some flows redirect instead of returning JSON
            if "login" in str(resp.url).lower() and "account" not in str(resp.url).lower():
                raise RuntimeError(
                    "Leslie's login failed — ended up back at login page. "
                    "Check LESLIES_EMAIL / LESLIES_PASSWORD."
                )
            logger.info("Leslie's login OK (redirect flow)")

    async def get_water_tests_html(self) -> str:
        """
        Fetch water test history HTML fragment from Leslie's AJAX API.

        Flow (reverse-engineered from waterTest.js):
          1. GET WaterTest-Landing → establishes session state needed for AJAX calls
          2. GET ProfileById → get pool_name (required for GetWaterTest)
          3. POST GetWaterTest (form-encoded) → returns HTML table in response["response"]
        """
        pool_id = LESLIES_POOL_ID or "6210168"

        # Step 1: Visit WaterTest-Landing — REQUIRED to activate server session state
        wtl_url = (
            f"{WATER_TEST}?poolProfileId={pool_id}"
            f"&poolName={quote('Pool & Spa - 1522 OLD RIVERS GATE RD')}"
        )
        await self.client.get(wtl_url)

        # Step 2: Get pool profile name via ProfileById (works after WaterTest-Landing visit)
        pool_name = "Pool & Spa - 1522 OLD RIVERS GATE RD"  # fallback
        try:
            r_prof = await self.client.get(
                f"{PROFILE_BY_ID}?poolProfileId={pool_id}",
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": wtl_url,
                },
            )
            prof_data = r_prof.json()
            if prof_data.get("success"):
                pool_name = prof_data["poolProfile"].get("pool_name", pool_name)
                logger.debug(f"Leslie's pool name: {pool_name!r}")
        except Exception as e:
            logger.warning(f"Leslie's ProfileById failed, using fallback name: {e}")

        # Step 3: POST GetWaterTest — MUST be form-encoded (jQuery $.ajax default, not JSON)
        r = await self.client.post(
            GET_WATER_TEST,
            data={
                "poolProfileName": pool_name,
                "poolSanitizer":   "Saltwater",
                "count":           "50",
            },
            headers={
                "Accept":           "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer":          wtl_url,
            },
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            raise RuntimeError(
                f"Leslie's GetWaterTest failed: {data.get('message', data)}"
            )

        html_fragment = data.get("response", "")
        if not html_fragment:
            logger.warning("Leslie's GetWaterTest returned empty response HTML")
        return html_fragment

    async def get_orders_html(self) -> str:
        r = await self.client.get(ORDER_HIST)
        r.raise_for_status()
        return r.text


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_water_tests(page_html: str) -> list[dict]:
    """
    Parse water test history table from Leslie's GetWaterTest API response.

    The HTML fragment has a <table> with columns:
      Date | Overall Score | View PDF | Free Chlorine | Total Chlorine |
      pH | Alkalinity | Calcium | Cyanuric Acid | Iron | Copper |
      Phosphates | Salt | Issues Reported | Instore Test

    IMPORTANT: Date and PDF cells use <th> not <td>.
    Use row.xpath('td|th') to capture all cells.
    """
    tree = html.fromstring(page_html)
    tests = []

    # Find the chemistry table
    target_table = None
    for t in tree.xpath('//table'):
        headers_text = " ".join(
            (th.text_content() or "").lower()
            for th in t.xpath('.//th')
        )
        if "chlorine" in headers_text or "alkalinity" in headers_text:
            target_table = t
            break

    if target_table is None:
        # Check for JSON data embedded in a script tag (fallback)
        for script in tree.xpath('//script/text()'):
            if "waterTest" in script or "freeChlorine" in script:
                match = re.search(r'\[[\s\S]*?"(?:freeChlorine|chlorine)"[\s\S]*?\]', script)
                if match:
                    try:
                        return _normalize_api_tests(json.loads(match.group(0)))
                    except json.JSONDecodeError:
                        pass
        logger.warning("Leslie's: no water test table found in HTML")
        return []

    # Map column headers to field names
    headers = [
        (th.text_content() or "").strip().lower()
        for th in target_table.xpath('.//thead/tr/th | .//tr[1]/th')
    ]
    col_map = {}
    for i, h in enumerate(headers):
        if "date" in h:                              col_map["date"]          = i
        elif "overall" in h or "score" in h:         col_map["overall_score"] = i
        elif "pdf" in h or "view" in h:              col_map["pdf_col"]       = i
        elif "free" in h and "chlor" in h:           col_map["fc"]            = i
        elif "total" in h and "chlor" in h:          col_map["tc"]            = i
        elif h.strip() == "ph":                      col_map["ph"]            = i
        elif "alkalinity" in h:                      col_map["ta"]            = i
        elif "calcium" in h:                         col_map["ch"]            = i
        elif "cyanuric" in h or "stabilizer" in h:  col_map["cya"]           = i
        elif "iron" in h:                            col_map["iron"]          = i
        elif "copper" in h:                          col_map["copper"]        = i
        elif "phosphate" in h or "phos" in h:        col_map["phosphate"]     = i
        elif h.strip() == "salt":                    col_map["salt"]          = i

    logger.debug(f"Leslie's column map: {col_map}")

    # IMPORTANT: rows use both <th> (date, PDF) and <td> (chemistry values)
    for row in target_table.xpath('.//tbody/tr'):
        cells = row.xpath('td|th')   # must include BOTH th AND td
        if len(cells) < 3:
            continue
        test = {"source": "leslies"}

        def cell_text(key):
            idx = col_map.get(key)
            if idx is None or idx >= len(cells):
                return None
            v = (cells[idx].text_content() or "").strip()
            return None if v in _NULL_VALUES else v

        raw_date = cell_text("date")
        if raw_date:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y"):
                try:
                    test["test_date"] = datetime.strptime(raw_date.strip(), fmt).isoformat()
                    break
                except ValueError:
                    pass
            else:
                test["test_date"] = raw_date

        for field in ("fc", "tc", "ph", "ta", "ch", "cya", "iron", "copper", "phosphate", "salt"):
            v = cell_text(field)
            if v is not None:
                try:
                    test[field] = float(v)
                except ValueError:
                    pass

        score = cell_text("overall_score")
        if score is not None:
            try:
                test["overall_score"] = int(score)
            except ValueError:
                pass

        pdf_idx = col_map.get("pdf_col")
        if pdf_idx is not None and pdf_idx < len(cells):
            links = cells[pdf_idx].xpath('.//a/@href')
            if links:
                href = links[0]
                test["pdf_url"] = href if href.startswith("http") else BASE + href

        if "test_date" in test:
            tests.append(test)

    return tests


def parse_orders(page_html: str) -> list[dict]:
    """Parse order history from Leslie's account page."""
    tree = html.fromstring(page_html)
    orders = []

    order_rows = (
        tree.xpath('//*[contains(@class,"order-history")]//li') or
        tree.xpath('//*[contains(@class,"order-row")]') or
        tree.xpath('//table[contains(@class,"order")]//tr[position()>1]') or
        tree.xpath('//*[contains(@class,"order-card")]')
    )

    for row in order_rows:
        text = (row.text_content() or "").strip()
        if not text or len(text) < 5:
            continue
        order = {}
        m = re.search(r'(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})', text)
        if m:
            order["date"] = m.group(1)
        m = re.search(r'(?:order|#)\s*[:#]?\s*(\d{5,})', text, re.IGNORECASE)
        if m:
            order["order_number"] = m.group(1)
        m = re.search(r'\$(\d+\.\d{2})', text)
        if m:
            order["total"] = float(m.group(1))
        order["raw"] = text[:200]
        orders.append(order)

    return orders


def _normalize_api_tests(raw: list) -> list[dict]:
    """Normalize Leslie's JSON API format to our schema."""
    tests = []
    key_map = [
        ("date", "test_date"), ("testDate", "test_date"), ("testdate", "test_date"),
        ("freeChlorine", "fc"), ("free_chlorine", "fc"), ("freechlorine", "fc"),
        ("totalChlorine", "tc"), ("total_chlorine", "tc"),
        ("ph", "ph"), ("pH", "ph"),
        ("alkalinity", "ta"), ("totalAlkalinity", "ta"),
        ("calcium", "ch"), ("calciumHardness", "ch"),
        ("cyanuricAcid", "cya"), ("cya", "cya"),
        ("iron", "iron"), ("copper", "copper"), ("phosphate", "phosphate"),
        ("salt", "salt"),
        ("overallScore", "overall_score"), ("score", "overall_score"),
    ]
    for item in raw:
        test = {"source": "leslies"}
        for src, dst in key_map:
            if src in item and item[src] not in (None, "N/A", ""):
                test[dst] = item[src]
        if test:
            tests.append(test)
    return tests


# ── DB Import ─────────────────────────────────────────────────────────────────

async def sync_leslies_tests() -> str:
    """Fetch water test history from Leslie's and import into pool_readings."""
    try:
        async with LesliesSession() as session:
            page_html = await session.get_water_tests_html()
    except Exception as e:
        logger.error(f"Leslie's fetch error: {e}")
        return f"Leslie's sync failed: {e}"

    tests = parse_water_tests(page_html)
    if not tests:
        return (
            "Leslie's sync: login OK but no tests parsed. "
            "Run /pool debug to inspect the raw HTML."
        )

    imported = skipped = 0
    async with db_connect() as db:
        for test in tests:
            test_date = test.get("test_date", datetime.utcnow().isoformat())
            if "T" not in str(test_date):
                try:
                    test_date = datetime.strptime(test_date[:10], "%Y-%m-%d").isoformat()
                except ValueError:
                    test_date = datetime.utcnow().isoformat()

            cur = await db.execute(
                "SELECT id FROM pool_readings WHERE source='leslies' AND date(logged_at)=date(?)",
                (test_date,),
            )
            if await cur.fetchone():
                skipped += 1
                continue

            await db.execute(
                """INSERT INTO pool_readings
                   (logged_at, source, fc, tc, ph, ta, ch, cya,
                    iron, copper, phosphate, salt, overall_score, pdf_url)
                   VALUES (?, 'leslies', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    test_date,
                    test.get("fc"),       test.get("tc"),       test.get("ph"),
                    test.get("ta"),       test.get("ch"),        test.get("cya"),
                    test.get("iron"),     test.get("copper"),    test.get("phosphate"),
                    test.get("salt"),     test.get("overall_score"), test.get("pdf_url"),
                ),
            )
            imported += 1

        await db.commit()

    return f"Leslie's sync: {imported} new test(s) imported, {skipped} already in DB."


async def sync_leslies_orders() -> str:
    """Fetch order history from Leslie's account page."""
    try:
        async with LesliesSession() as session:
            page_html = await session.get_orders_html()
    except Exception as e:
        return f"Leslie's orders fetch failed: {e}"

    orders = parse_orders(page_html)
    if not orders:
        return "No orders found (or parsing needs adjustment — try /pool debug orders)."

    lines = [f"Leslie's Orders ({len(orders)} found):"]
    for o in orders[:10]:
        parts = []
        if o.get("date"):         parts.append(o["date"])
        if o.get("order_number"): parts.append(f"#{o['order_number']}")
        if o.get("total"):        parts.append(f"${o['total']:.2f}")
        lines.append(f"  • {' — '.join(parts)}")
        first = o["raw"].split("\n")[0].strip()[:80]
        if first:
            lines.append(f"    {first}")

    return "\n".join(lines)


async def get_leslies_debug(target: str = "tests") -> str:
    """Return raw HTML snippet for debugging Leslie's parsing."""
    try:
        async with LesliesSession() as session:
            page_html = await session.get_orders_html() if target == "orders" else await session.get_water_tests_html()
    except Exception as e:
        return f"Leslie's fetch failed: {e}"

    snippet = page_html[:3500]
    return f"Leslie's {target} HTML (first 3500 chars):\n```\n{snippet}\n```"
