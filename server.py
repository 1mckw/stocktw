#!/usr/bin/env python3
"""Local server for TWSE listed scanner — serves HTML, proxies Yahoo, saves chartpacks."""
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
ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
CODE_RE = re.compile(r"^(\d{4})\s+")


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _cell_text(raw: str) -> str:
    return unescape(TAG_RE.sub("", raw)).replace("\xa0", " ").replace("&nbsp;", " ").strip()


def parse_isin_listed(html: bytes) -> list[dict]:
    text = html.decode("cp950", errors="replace")
    out = []
    seen = set()
    for row in ROW_RE.findall(text):
        cells = [_cell_text(c) for c in CELL_RE.findall(row)]
        if len(cells) < 6:
            continue
        if (cells[5] or "").upper() != "ESVUFR":
            continue
        m = CODE_RE.match(cells[0].replace("\u3000", " "))
        if not m:
            continue
        code = m.group(1)
        if code.startswith("00"):
            continue
        name = cells[0].replace("\u3000", " ")[len(code) :].strip()
        if not name or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name, "symbol": f"{code}.TW"})
    out.sort(key=lambda x: x["code"])
    return out


def load_listed_file() -> list[dict]:
    if not LISTED_PATH.exists():
        return []
    try:
        data = json.loads(LISTED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data.get("stocks") or []


def save_listed(stocks: list[dict]) -> None:
    LISTED_PATH.write_text(
        json.dumps({"count": len(stocks), "stocks": stocks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_listed_stocks(refresh: bool = False) -> list[dict]:
    cached = load_listed_file()
    if cached and not refresh:
        return cached
    try:
        stocks = parse_isin_listed(http_get(ISIN_URL, timeout=60))
        if len(stocks) >= 500:
            save_listed(stocks)
            return stocks
    except Exception as e:
        print(f"listed fetch failed: {e}", flush=True)
    if cached:
        return cached
    raise RuntimeError("no listed stocks")


def listed_payload() -> dict:
    stocks = fetch_listed_stocks()
    return {
        "count": len(stocks),
        "stocks": stocks,
        "symbols": [s["symbol"] for s in stocks],
    }


def ensure_packs_dir() -> None:
    PACKS_DIR.mkdir(parents=True, exist_ok=True)


def pack_path(date: str) -> Path:
    return PACKS_DIR / f"{date}.json"


def pack_files_for_date(date: str) -> list[Path]:
    ensure_packs_dir()
    files = sorted(p for p in PACKS_DIR.glob(f"*-{date}.json") if p.stem != "manifest")
    exact = pack_path(date)
    if exact.exists() and exact not in files:
        files.append(exact)
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
        for row in data.get("results") or []:
            results.append(row)
            if row.get("bars"):
                count += 1
    return {
        "date": date,
        "index": "listed",
        "indexLabel": "上市",
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
    print(f"台股上市掃描器 → http://127.0.0.1:{PORT}/")
    print(f"Chartpacks → {PACKS_DIR}")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
