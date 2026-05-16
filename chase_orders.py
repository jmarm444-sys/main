import argparse
import csv
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image
import pytesseract

# run as python chase_orders.py firstscreenshot.png second.png 
# put in as many screenshots as needed to get all the pdf symbols, the output file default is ABC.csv
# for other output.csv file name use -o othername.csv


COLUMNS = ["date", "symbol", "BS", "qty", "limit", "time-in-force", "status"]

DEFAULT_SCREENSHOTS = (
    Path("A-orders.png"),
    Path("B-orders.png"),
    Path("C-orders.png"),
)
DEFAULT_OUTPUT_CSV = Path("ABC.csv")

DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")
# Time: allow "PMET"/"AMET" (missing space before ET). Prefix: optional ">", stray "p", or junk before digits.
# Limit rows: symbol may include OCR noise (!, :) before Buy/Sell.
# Status: Chase uses "Queued" in screenshots; OCR may split "Cancellation" / "pending" across the detail line.
L1_RE = re.compile(
    r"(?:>\s*)?[^\d\(]*?"
    r"\(?\d{1,2}:\d{2}\s*[AP]M\s*E?T\s+"
    r"\(?(?P<symbol>[A-Za-z0-9][A-Za-z0-9!.-]{0,7})\)?:?\s+"
    r"(?P<side>Buy|Sell)\s+"
    r"(?P<qty>\d+)\s+"
    r"Limit\s*\$?(?P<limit>\d+(?:\.\d{1,2})?)\s+"
    r"(?P<tif>Good.*?)"
    r"(?P<status>In\s+queue|Queued|Executed|Open|Partially(?:\s+executed)?|Cancellation(?:\s+pending)?)",
    flags=re.IGNORECASE,
)
# Market rows: no limit price; e.g. "DRAM Buy 10 Market Day Executed".
MARKET_RE = re.compile(
    r"(?:>\s*)?[^\d\(]*?"
    r"\(?\d{1,2}:\d{2}\s*[AP]M\s*E?T\s+"
    r"\(?(?P<symbol>[A-Za-z0-9][A-Za-z0-9!.-]{0,7})\)?:?\s+"
    r"(?P<side>Buy|Sell)\s+"
    r"(?P<qty>\d+)\s+"
    r"Market\s+"
    r"(?:(?P<market_tif>Day|GTC|IOC)\s+)?"
    r"(?P<status>Executed|Open|Queued|In\s+queue|Partially(?:\s+executed)?)",
    flags=re.IGNORECASE,
)
DATE_TIF_RE = re.compile(r"\b([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})\b")
# Chase puts executed/fill price on the security line, e.g. "... ETF $51.21"
FILL_PRICE_RE = re.compile(r"\$(\d+(?:\.\d{1,4})?)")

SYMBOL_FIXUPS = {
    "QCCOM": "QCOM",
    "QCCOMM": "QCOM",
    "LROX": "LRCX",
    "C": "C",
}


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_tif(tif_text: str) -> str:
    joined = normalize_spaces(tif_text.replace(",", ""))
    low = joined.lower().replace("till", "til")
    if "cancelled" in low:
        return "good til canceled"
    if "date" in low:
        return "good til date"
    return low


def normalize_market_tif(session: Optional[str]) -> str:
    s = normalize_spaces(session or "").lower()
    if s == "day":
        return "market day"
    if s == "gtc":
        return "market gtc"
    if s == "ioc":
        return "market ioc"
    return "market"


def normalize_status(status: str) -> str:
    s = normalize_spaces(status).lower()
    if s.startswith("partially"):
        return "partially executed"
    if s == "queued":
        return "in queue"
    if s == "cancellation pending":
        return "cancellation pending"
    if s == "cancellation":
        return "cancellation"
    return s


def normalize_symbol(symbol: str) -> str:
    s = symbol.upper().strip("()")
    if s.endswith("!"):
        s = s[:-1] + "I"
    s = s.strip(":.,!")
    return SYMBOL_FIXUPS.get(s, s)


def fix_ocr_order_line(line: str) -> str:
    """Repair common OCR glitches on Chase order rows (time without colon, PM+ET run together)."""
    line = re.sub(r"(\d{1,2}:\d{2})\s*([AP]M)ET\b", r"\1 \2 ET", line, flags=re.IGNORECASE)
    def _colon(m: re.Match[str]) -> str:
        h, mm, rest = m.group(1), m.group(2), m.group(3)
        if int(mm) > 59:
            return m.group(0)
        return f"{h}:{mm}{rest}"

    line = re.sub(
        r"(?<![:/\d])(\d{1,2})(\d{2})(\s+[AP]M\s*E?T\b)",
        _colon,
        line,
        flags=re.IGNORECASE,
    )
    return line


def fill_price_from_detail_line(detail_line: str) -> str:
    """Last $amount on the date/security line (fill price for executed market orders on Chase)."""
    found = FILL_PRICE_RE.findall(detail_line)
    if not found:
        return ""
    try:
        return f"{float(found[-1]):.2f}"
    except ValueError:
        return ""


def parse_rows(lines: List[str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for i, line in enumerate(lines):
        line = fix_ocr_order_line(line)
        m = L1_RE.search(line)
        is_market = False
        if not m:
            m = MARKET_RE.search(line)
            is_market = m is not None
        if not m:
            continue
        if i + 1 >= len(lines):
            continue
        date_line = lines[i + 1]
        date_match = DATE_RE.search(date_line)
        if not date_match:
            continue

        status_raw = m.group("status")
        if re.fullmatch(r"cancellation", status_raw.strip(), re.I) and (
            "pending" in normalize_spaces(date_line).lower()
        ):
            status_raw = "Cancellation pending"

        if is_market:
            mtif = m.groupdict().get("market_tif")
            tif_val = normalize_market_tif(mtif if mtif else None)
            limit_val = ""
            if normalize_status(status_raw) == "executed":
                limit_val = fill_price_from_detail_line(date_line)
        else:
            tif_val = normalize_tif(m.group("tif"))
            if "date" in tif_val:
                dtm = DATE_TIF_RE.search(date_line)
                if dtm:
                    mon, day, year = dtm.groups()
                    tif_val = f"good til date {mon.lower()} {int(day)} {year}"
            limit_val = f"{float(m.group('limit')):.2f}"

        rows.append(
            {
                "date": date_match.group(0),
                "symbol": normalize_symbol(m.group("symbol")),
                "BS": m.group("side").lower(),
                "qty": m.group("qty"),
                "limit": limit_val,
                "time-in-force": tif_val,
                "status": normalize_status(status_raw),
            }
        )
    return rows


def ocr_lines(image_path: Path) -> List[str]:
    image = Image.open(image_path)
    # psm 6: assume a block of text; tends to work better on table screenshots.
    text = pytesseract.image_to_string(image, config="--psm 6")
    lines = [normalize_spaces(x) for x in text.splitlines()]
    return [line for line in lines if line]


def extract_rows_from_images(image_paths: List[Path]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for img in image_paths:
        lines = ocr_lines(img)
        rows.extend(parse_rows(lines))
    return rows


def resolve_image_paths(paths: List[Path]) -> List[Path]:
    """Expand user, join to cwd when relative, resolve — so argv filenames are found reliably."""
    resolved: List[Path] = []
    for p in paths:
        q = p.expanduser()
        if not q.is_absolute():
            q = Path.cwd() / q
        resolved.append(q.resolve())
    return resolved


def resolve_tesseract_cmd(cli_path: str) -> str:
    """Return path to tesseract.exe: CLI flag, TESSERACT_CMD env, PATH, then common Windows installs."""
    if cli_path.strip():
        return cli_path.strip()
    env = os.environ.get("TESSERACT_CMD", "").strip()
    if env:
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        ):
            if candidate.is_file():
                return str(candidate)
    return ""


def write_csv(rows: List[Dict[str, str]], output_path: Path) -> None:
    deduped: List[Dict[str, str]] = []
    seen = set()
    for r in rows:
        key = (
            r["date"],
            r["symbol"],
            r["BS"],
            r["qty"],
            r["limit"],
            r["time-in-force"],
            r["status"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(deduped)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract order rows from screenshots into CSV."
    )
    parser.add_argument(
        "images",
        nargs="*",
        type=Path,
        default=[],
        help="Screenshot paths (default: A-orders.png B-orders.png C-orders.png in cwd).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT_CSV}).",
    )
    parser.add_argument(
        "--tesseract-cmd",
        type=str,
        default="",
        help="Optional full path to tesseract executable.",
    )
    args = parser.parse_args()

    tesseract_exe = resolve_tesseract_cmd(args.tesseract_cmd or "")
    if not tesseract_exe:
        raise SystemExit(
            "Tesseract not found. Install it from https://github.com/UB-Mannheim/tesseract/wiki "
            "and either add the install folder to your PATH, set TESSERACT_CMD to the full path "
            "to tesseract.exe, or pass --tesseract-cmd \"C:\\Program Files\\Tesseract-OCR\\tesseract.exe\""
        )
    pytesseract.pytesseract.tesseract_cmd = tesseract_exe

    image_paths = list(args.images) if args.images else list(DEFAULT_SCREENSHOTS)
    image_paths = resolve_image_paths(image_paths)

    missing = [str(p) for p in image_paths if not p.exists()]
    if missing:
        raise SystemExit(f"Image file(s) not found: {', '.join(missing)}")

    rows = extract_rows_from_images(image_paths)
    write_csv(rows, args.output)
    print(f"Wrote {len(rows)} row(s) to {args.output}")


if __name__ == "__main__":
    main()
