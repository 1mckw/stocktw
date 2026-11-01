#!/usr/bin/env python3
"""Local server for TWSE listed scanner — serves HTML, proxies Yahoo, saves chartpacks."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PACKS_DIR = ROOT / "chartpacks"
PORT = 8765
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 臺灣50（上市；Yahoo .TW · 對照 0050 2026-09-30 持股）
TW50_SYMBOLS = [
    "2330.TW", "2454.TW", "2308.TW", "2317.TW", "3711.TW", "2303.TW", "3037.TW", "2383.TW", "1303.TW", "2881.TW",
    "2891.TW", "3017.TW", "2882.TW", "2887.TW", "2345.TW", "2382.TW", "2327.TW", "2360.TW", "2885.TW", "2884.TW",
    "2059.TW", "2408.TW", "2357.TW", "3443.TW", "6669.TW", "2883.TW", "3653.TW", "2886.TW", "2890.TW", "2344.TW",
    "3008.TW", "3231.TW", "2301.TW", "2412.TW", "3665.TW", "2892.TW", "2880.TW", "4958.TW", "2368.TW", "6446.TW",
    "1216.TW", "2395.TW", "2449.TW", "8046.TW", "7769.TW", "5880.TW", "2603.TW", "4904.TW", "3045.TW", "6505.TW",
]

# 臺灣中型100（上市；Yahoo .TW · 對照 0051 2026-09-30 持股）
MID100_SYMBOLS = [
    "3481.TW", "2379.TW", "3189.TW", "3034.TW", "3661.TW", "1301.TW", "6770.TW", "2313.TW", "1326.TW", "2409.TW",
    "2337.TW", "2404.TW", "3044.TW", "2801.TW", "2002.TW", "6239.TW", "2376.TW", "5876.TW", "4938.TW", "1101.TW",
    "1590.TW", "3702.TW", "2615.TW", "3036.TW", "2324.TW", "6515.TW", "2207.TW", "3533.TW", "5871.TW", "2356.TW",
    "6213.TW", "6415.TW", "2834.TW", "6805.TW", "8996.TW", "2347.TW", "6139.TW", "2618.TW", "6257.TW", "1605.TW",
    "2912.TW", "1519.TW", "1560.TW", "2609.TW", "2377.TW", "3026.TW", "2455.TW", "2049.TW", "6531.TW", "5434.TW",
    "2353.TW", "2474.TW", "6442.TW", "1402.TW", "2027.TW", "6285.TW", "1102.TW", "2492.TW", "1504.TW", "6919.TW",
    "2812.TW", "2315.TW", "7750.TW", "3023.TW", "2385.TW", "3006.TW", "2354.TW", "1513.TW", "2610.TW", "6409.TW",
    "7610.TW", "8464.TW", "1717.TW", "1802.TW", "6196.TW", "9904.TW", "2451.TW", "3167.TW", "5269.TW", "8210.TW",
    "1503.TW", "1476.TW", "2105.TW", "2542.TW", "6691.TW", "6789.TW", "2633.TW", "2467.TW", "9945.TW", "3005.TW",
    "1229.TW", "3030.TW", "6526.TW", "6949.TW", "6781.TW", "3532.TW", "9910.TW", "2646.TW", "2838.TW", "2645.TW",
]

# 臺灣高股息（上市；Yahoo .TW · 對照 0056 2026-09-30 持股）
HDIV_SYMBOLS = [
    "1303.TW", "2408.TW", "2357.TW", "2890.TW", "2880.TW", "2344.TW", "2891.TW", "3231.TW", "2382.TW", "2886.TW",
    "2603.TW", "2883.TW", "2887.TW", "2885.TW", "2884.TW", "2301.TW", "1216.TW", "2303.TW", "2379.TW", "2317.TW",
    "3045.TW", "4904.TW", "3034.TW", "3044.TW", "3036.TW", "3711.TW", "2454.TW", "2376.TW", "4938.TW", "5876.TW",
    "2618.TW", "6239.TW", "2449.TW", "2356.TW", "3702.TW", "2347.TW", "2324.TW", "2377.TW", "6285.TW", "1102.TW",
    "2474.TW", "2027.TW", "2105.TW", "2353.TW", "1513.TW", "3023.TW", "2385.TW", "3005.TW", "9904.TW", "2645.TW",
]


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ensure_packs_dir() -> None:
    PACKS_DIR.mkdir(parents=True, exist_ok=True)


def pack_path(date: str) -> Path:
    return PACKS_DIR / f"{date}.json"


def list_pack_dates() -> list[str]:
    ensure_packs_dir()
    dates = []
    for p in PACKS_DIR.glob("*.json"):
        if DATE_RE.match(p.stem):
            dates.append(p.stem)
    dates.sort(reverse=True)
    return dates


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

        if path == "/api/tw50":
            self._json(200, {"symbols": TW50_SYMBOLS, "count": len(TW50_SYMBOLS)})
            return

        if path == "/api/mid100":
            self._json(200, {"symbols": MID100_SYMBOLS, "count": len(MID100_SYMBOLS)})
            return

        if path == "/api/hdiv":
            self._json(200, {"symbols": HDIV_SYMBOLS, "count": len(HDIV_SYMBOLS)})
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
            fp = pack_path(date)
            if not fp.exists():
                self._json(404, {"error": "pack not found", "date": date})
                return
            try:
                self._bytes(200, fp.read_bytes(), "application/json; charset=utf-8")
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
