#!/usr/bin/env python3
"""Local server for 台灣50 / 中型100 / 富櫃50 scanner — serves HTML, proxies Yahoo, saves chartpacks."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PACKS_DIR = ROOT / "chartpacks"
LISTED_PATH = ROOT / "listed.json"
PORT = 8765
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TPEX50_URL = "https://www.tpex.org.tw/www/zh-tw/indexInfo/r50?type=cnst"
CELL_RE = re.compile(r"^(\d{4})\s+(.+)$")
MDJ_NAME_RE = re.compile(r'<td class="col05">([^<]+)</td>')
FINMIND_INFO_URL = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"
INDEX_IDS = ("tw50", "mid100", "otc50")
INDEX_LABELS = {"tw50": "台灣50", "mid100": "中型100", "otc50": "富櫃50"}
INDEX_MIN = {"tw50": 40, "mid100": 80, "otc50": 40}
ETF_FOR_INDEX = {"tw50": "0050", "mid100": "0051"}


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_tpex50(payload: dict) -> list[dict]:
    out = []
    seen = set()
    for table in payload.get("tables") or []:
        for row in table.get("data") or []:
            for i in range(1, len(row), 2):
                cell = unescape(str(row[i])).replace("&nbsp;", " ").replace("\xa0", " ").strip()
                m = CELL_RE.match(cell)
                if not m:
                    continue
                code, name = m.group(1), m.group(2).strip()
                if not name or code in seen:
                    continue
                seen.add(code)
                out.append({"code": code, "name": name, "symbol": f"{code}.TWO", "indexes": ["otc50"]})
    out.sort(key=lambda x: x["code"])
    return out


def names_from_moneydj(etf: str) -> list[str]:
    html = http_get(f"https://www.moneydj.com/etf/x/basic/basic0007a.xdjhtm?etfid={etf}.tw", timeout=60).decode(
        "utf-8", "replace"
    )
    names, seen = [], set()
    for raw in MDJ_NAME_RE.findall(html):
        name = unescape(raw).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def finmind_twse_by_name() -> dict[str, str]:
    data = json.loads(http_get(FINMIND_INFO_URL, timeout=60).decode("utf-8")).get("data") or []
    by_name: dict[str, str] = {}
    for x in data:
        name = x.get("stock_name") or ""
        sid = x.get("stock_id") or ""
        if x.get("type") == "twse" and re.fullmatch(r"\d{4}", sid) and not sid.startswith("00"):
            by_name.setdefault(name, sid)
    return by_name


def map_names_to_twse(names: list[str], by_name: dict[str, str], index_id: str) -> list[dict]:
    out = []
    for name in names:
        sid = by_name.get(name)
        if not sid:
            for k, v in by_name.items():
                if k == name or k.startswith(name) or name.startswith(k):
                    sid = v
                    break
        if not sid:
            continue
        out.append({"code": sid, "name": name, "symbol": f"{sid}.TW", "indexes": [index_id]})
    out.sort(key=lambda x: x["code"])
    return out


def fetch_otc50() -> list[dict]:
    payload = json.loads(http_get(TPEX50_URL, timeout=60).decode("utf-8"))
    return parse_tpex50(payload)


def fetch_index_etf(index_id: str) -> list[dict]:
    etf = ETF_FOR_INDEX[index_id]
    names = names_from_moneydj(etf)
    by_name = finmind_twse_by_name()
    return map_names_to_twse(names, by_name, index_id)


def index_counts(stocks: list[dict]) -> dict[str, int]:
    counts = {k: 0 for k in INDEX_IDS}
    for s in stocks:
        for i in universe_indexes(s.get("indexes")):
            counts[i] += 1
    return counts


def stocks_complete(stocks: list[dict]) -> bool:
    counts = index_counts(stocks)
    return all(counts[k] >= INDEX_MIN[k] for k in INDEX_IDS)


def cache_by_index(stocks: list[dict]) -> dict[str, list[dict]]:
    grouped = {k: [] for k in INDEX_IDS}
    for s in stocks:
        rec = normalize_stock(s)
        if not rec:
            continue
        for i in rec["indexes"]:
            grouped[i].append(
                {
                    "code": rec["code"],
                    "name": rec["name"],
                    "symbol": rec["symbol"],
                    "indexes": [i],
                }
            )
    return grouped


def merge_index_stocks(*groups: list[dict]) -> list[dict]:
    by_sym: dict[str, dict] = {}
    for rows in groups:
        for s in rows or []:
            rec = by_sym.setdefault(
                s["symbol"],
                {"code": s["code"], "name": s.get("name") or "", "symbol": s["symbol"], "indexes": []},
            )
            if s.get("name"):
                rec["name"] = s["name"]
            for i in universe_indexes(s.get("indexes")):
                if i not in rec["indexes"]:
                    rec["indexes"].append(i)
    return [x for x in sorted(by_sym.values(), key=lambda x: (x["symbol"], x["code"])) if x.get("indexes")]


def load_listed_file() -> list[dict]:
    if not LISTED_PATH.exists():
        return []
    try:
        data = json.loads(LISTED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for s in data.get("stocks") or []:
        rec = normalize_stock(s)
        if rec:
            out.append(rec)
    return out


def save_listed(stocks: list[dict]) -> None:
    counts = index_counts(stocks)
    payload = {
        "count": len(stocks),
        "indexes": {k: {"label": INDEX_LABELS[k], "count": counts[k]} for k in INDEX_IDS},
        "stocks": stocks,
    }
    LISTED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_listed_stocks(refresh: bool = False) -> list[dict]:
    cached = load_listed_file()
    if cached and not refresh and stocks_complete(cached):
        return cached
    cached_idx = cache_by_index(cached)
    groups: dict[str, list[dict]] = {}
    for index_id, fetcher in (
        ("tw50", lambda: fetch_index_etf("tw50")),
        ("mid100", lambda: fetch_index_etf("mid100")),
        ("otc50", fetch_otc50),
    ):
        try:
            rows = fetcher()
            if len(rows) >= INDEX_MIN[index_id]:
                groups[index_id] = rows
                continue
            print(f"{index_id} fetch too few ({len(rows)})", flush=True)
        except Exception as e:
            print(f"{index_id} fetch failed: {e}", flush=True)
        if len(cached_idx[index_id]) >= INDEX_MIN[index_id]:
            groups[index_id] = cached_idx[index_id]
    stocks = merge_index_stocks(*(groups.get(k) or [] for k in INDEX_IDS))
    if stocks_complete(stocks):
        save_listed(stocks)
        return stocks
    if stocks_complete(cached):
        return cached
    if stocks:
        save_listed(stocks)
        return stocks
    raise RuntimeError("no 台灣50 / 中型100 / 富櫃50 constituents")


def listed_payload() -> dict:
    stocks = fetch_listed_stocks()
    counts = index_counts(stocks)
    return {
        "count": len(stocks),
        "indexes": {k: {"label": INDEX_LABELS[k], "count": counts[k]} for k in INDEX_IDS},
        "stocks": stocks,
        "symbols": [s["symbol"] for s in stocks],
    }


def ensure_packs_dir() -> None:
    PACKS_DIR.mkdir(parents=True, exist_ok=True)


def pack_path(date: str) -> Path:
    return PACKS_DIR / f"{date}.json"


def canonical_index(index: str) -> str:
    s = str(index or "")
    if len(s) >= 11 and DATE_RE.match(s[-10:]) and s[-11] == "-":
        s = s[:-11]
    for key in INDEX_IDS:
        if s == key or s.startswith(key):
            return key
    return ""


def universe_indexes(raw) -> list[str]:
    out = []
    for i in raw or []:
        c = canonical_index(i)
        if c in INDEX_IDS and c not in out:
            out.append(c)
    return out


def normalize_stock(stock: dict) -> dict | None:
    idxs = universe_indexes(stock.get("indexes"))
    if not idxs:
        return None
    return {
        "code": stock.get("code") or "",
        "name": stock.get("name") or "",
        "symbol": stock.get("symbol") or "",
        "indexes": idxs,
    }


def pack_files_for_date(date: str) -> list[Path]:
    ensure_packs_dir()
    files = []
    for p in sorted(PACKS_DIR.glob(f"*-{date}.json")):
        if p.stem == "manifest":
            continue
        if canonical_index(p.stem) in INDEX_IDS:
            files.append(p)
    return files


def merge_pack_files(date: str) -> dict | None:
    files = pack_files_for_date(date)
    if not files:
        return None
    results = []
    count = 0
    saved_at = None
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        saved_at = data.get("savedAt") or saved_at
        idx = canonical_index(data.get("index") or fp.stem)
        if idx not in INDEX_IDS:
            continue
        for row in data.get("results") or []:
            idxs = universe_indexes(row.get("indexes") or ([idx] if idx else []))
            if idx not in idxs:
                idxs.append(idx)
            if not idxs:
                continue
            row["index"] = idxs[0]
            row["indexes"] = idxs
            results.append(row)
            if row.get("bars"):
                count += 1
    return {
        "date": date,
        "index": "all",
        "indexLabel": "台股",
        "savedAt": saved_at,
        "count": count,
        "results": results,
    }


def list_pack_dates() -> list[str]:
    ensure_packs_dir()
    dates = set()
    for p in PACKS_DIR.glob("*.json"):
        if p.stem == "manifest":
            continue
        if canonical_index(p.stem) not in INDEX_IDS:
            continue
        if DATE_RE.match(p.stem):
            dates.add(p.stem)
        elif len(p.stem) >= 11 and DATE_RE.match(p.stem[-10:]):
            dates.add(p.stem[-10:])
    return sorted(dates, reverse=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {args[0]}")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, *")

    def _json(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/ndx100-scanner.html"):
            fp = ROOT / "ndx100-scanner.html"
            self._bytes(200, fp.read_bytes(), "text/html; charset=utf-8")
            return

        if path in ("/api/listed", "/listed.json"):
            try:
                self._json(200, listed_payload())
            except Exception as e:
                self._json(502, {"error": str(e)})
            return

        if path == "/api/chart":
            qs = parse_qs(parsed.query)
            symbol = (qs.get("symbol") or [""])[0].strip().upper()
            range_ = (qs.get("range") or ["10y"])[0]
            # Yahoo range=max often downsamples to week/month/quarter — prefer 10y/5y for 1d
            if range_ == "max":
                range_ = "10y"
            if not symbol:
                self._json(400, {"error": "symbol required"})
                return
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?interval=1d&range={range_}&includePrePost=false"
            )
            try:
                raw = http_get(url)
                self._bytes(200, raw, "application/json")
            except urllib.error.HTTPError as e:
                self._json(e.code, {"error": f"yahoo HTTP {e.code}"})
            except Exception as e:
                self._json(502, {"error": str(e)})
            return

        if path == "/api/packs":
            self._json(200, {"dates": list_pack_dates()})
            return

        if path.startswith("/api/packs/"):
            date = path[len("/api/packs/") :].strip("/")
            if not DATE_RE.match(date):
                self._json(400, {"error": "invalid date"})
                return
            merged = merge_pack_files(date)
            if not merged:
                self._json(404, {"error": "pack not found", "date": date})
                return
            try:
                self._json(200, merged)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/packs/"):
            date = path[len("/api/packs/") :].strip("/")
            if not DATE_RE.match(date):
                self._json(400, {"error": "invalid date"})
                return
            try:
                body = self._read_json()
            except Exception as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "body must be object"})
                return
            body["date"] = date
            ensure_packs_dir()
            fp = pack_path(date)
            try:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                fp.write_bytes(raw)
                self._json(200, {"ok": True, "date": date, "bytes": len(raw)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    ensure_packs_dir()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"台股掃描器（台灣50 / 中型100 / 富櫃50）→ http://127.0.0.1:{PORT}/")
    print(f"Chartpacks → {PACKS_DIR}")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
