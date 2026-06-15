"""
Extract U.S. Bancorp holdings-statement PDFs (scanned/image pages) to CSV.

Pipeline: render each PDF page to an image (PyMuPDF), OCR it with the
built-in Windows OCR engine (winocr), cluster the recognized words into
table rows/columns by position, clean up common OCR errors, and write
one CSV row per holding.

The Windows OCR engine tends to drop short isolated tokens (a ticker
like "F" or a quantity like "17"), so rows are anchored on the
market-value column (always a long, reliably-recognized string) and any
missing cell is retried by OCR-ing an enlarged crop of just that cell.

Usage:
    python holdings_pdf_to_csv.py input.pdf output.csv

Requirements (Windows 10/11):
    pip install pymupdf winocr pillow
"""

import csv
import re
import sys

import fitz  # PyMuPDF
import winocr
from PIL import Image

DPI = 200

# Column bins as fractions of page width (left edge, right edge).
COLUMN_BINS = {
    "symbol":       (0.000, 0.118),
    "price":        (0.118, 0.200),
    "price_change": (0.200, 0.318),
    "quantity":     (0.318, 0.432),
    "market_value": (0.432, 0.559),
    "day_change":   (0.559, 0.673),
    "cost_basis":   (0.673, 0.786),
    "gain_loss":    (0.786, 0.900),
    "portfolio":    (0.900, 1.000),
}

SECTION_WORDS = {"STOCKS", "ETFS", "MUTUAL", "FUNDS", "UNITS", "CASH", "&", "EQUIVALENTS"}
STOP_WORDS = {"Disclosures", "Insurance", "FINRA"}

CSV_HEADER = [
    "section", "symbol", "price", "price_change", "price_change_pct",
    "quantity", "market_value", "day_change", "day_change_pct",
    "cost_basis", "gain_loss", "gain_loss_pct", "portfolio_pct",
]


def ocr_words(img):
    """OCR a PIL image, return [(y, x1, x2, text)] sorted by position."""
    result = winocr.recognize_pil_sync(img, "en-US")
    words = []
    for line in result["lines"]:
        for w in line["words"]:
            br = w["bounding_rect"]
            words.append((br["y"], br["x"], br["x"] + br["width"], w["text"]))
    return sorted(words)


def ocr_crop_candidates(img, x1, y1, x2, y2):
    """Yield raw OCR text of one cell at several zoom levels.

    The Windows OCR engine fails on overly enlarged crops, so several
    modest scale factors are tried; the caller keeps the first candidate
    that survives number cleaning.
    """
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(img.width, int(x2)), min(img.height, int(y2))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return
    for scale in (2, 3, 1):
        crop = img.crop((x1, y1, x2, y2)).resize(
            ((x2 - x1) * scale, (y2 - y1) * scale), Image.LANCZOS)
        text = "".join(t for _, _, _, t in ocr_words(crop))
        if text:
            yield text


def clean_number(text):
    """Normalize an OCR'd numeric cell to a plain number string ('' if blank)."""
    t = text.replace(" ", "")
    # '%' is often misread as '0/0', '0//0' or 'OZ'
    t = t.replace("0//0", "%").replace("0/0", "%")
    t = re.sub(r"OZ(?=\)?$)", "%", t)
    # Common OCR letter/digit confusions inside numeric cells
    t = t.replace("O", "0").replace("o", "0")
    t = t.replace("I", "1").replace("l", "1").replace("S", "5")
    t = t.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    t = t.replace("%", "")
    t = t.replace("\u2013", "").replace("\u2014", "")  # en/em dashes = blank cell
    t = re.sub(r"^\*", "+", t)   # '+' misread as '*'
    t = re.sub(r"^-\+", "+", t)  # stray dash picked up before a real '+'
    t = t.strip("+")
    if t in ("", "-", "."):
        return ""
    m = re.match(r"^(-?)(\d*)\.?(\d*)$", t)
    if not m:
        return None  # unparseable
    sign, ip, fp = m.groups()
    if not ip and not fp:
        return ""
    return f"{sign}{ip or '0'}" + (f".{fp}" if fp else "")


def clean_symbol(text):
    t = text.replace("'", "I").replace(" ", "")
    t = re.sub(r"[^A-Za-z0-9]", "", t)
    return t.upper()


def trim_to_ink(crop):
    """Crop away surrounding whitespace; None if the region is blank."""
    gray = crop.convert("L").point(lambda p: 255 if p < 160 else 0)
    bbox = gray.getbbox()
    if bbox is None:
        return None
    return crop.crop(bbox)


def ocr_symbol_with_context(img, width, y0):
    """Fallback for tickers the engine drops (1-2 letter words).

    Windows OCR silently discards short isolated words, but keeps them when
    they closely follow a confidently-recognized token. So OCR the symbol
    cell with the row's market-value cell pasted right in front of it, then
    keep the letter tokens.
    """
    lo, hi = COLUMN_BINS["market_value"]
    num = trim_to_ink(img.crop((int(lo * width) + 10, int(y0 - 14), int(hi * width), int(y0 + 26))))
    # The ticker sits below the main number line; stay clear of the border at x=0.
    sym = trim_to_ink(img.crop((10, int(y0 + 14), int(0.10 * width), int(y0 + 62))))
    if num is None or sym is None:
        return ""

    gap, margin = 35, 30
    h = max(num.height, sym.height)
    canvas = Image.new("RGB", (num.width + sym.width + gap + 2 * margin, h + 2 * margin), "white")
    canvas.paste(num, (margin, margin + (h - num.height) // 2))
    canvas.paste(sym, (margin + num.width + gap, margin + (h - sym.height) // 2))
    big = canvas.resize((canvas.width * 2, canvas.height * 2), Image.LANCZOS)

    tokens = [t for _, _, _, t in ocr_words(big)]
    letters = [t for t in tokens if re.fullmatch(r"[A-Za-z']+", t)]
    return clean_symbol("".join(letters))


def parse_page(img, pageno, section, records, warnings):
    width = img.width
    words = ocr_words(img)
    if not words:
        return section

    def col_of(x_center):
        f = x_center / width
        for name, (lo, hi) in COLUMN_BINS.items():
            if lo <= f < hi:
                return name
        return None

    def bin_px(col):
        lo, hi = COLUMN_BINS[col]
        return lo * width, hi * width

    # Skip everything above the table header row (use the lowest 'Symbol'
    # word: page 1 also has a 'Symbol or company name' search box above it).
    min_y = 0
    for y, x1, x2, t in words:
        if t == "Symbol":
            min_y = max(min_y, y + 20)

    # Stop at disclosure text if present on this page.
    max_y = float("inf")
    for y, x1, x2, t in words:
        if t in STOP_WORDS and y > min_y:
            max_y = min(max_y, y - 10)

    words = [w for w in words if min_y < w[0] < max_y]

    # Section headers (text in the symbol column made only of section words).
    headers = []  # (y, section_name)
    sym_words = [(y, t) for y, x1, x2, t in words if col_of((x1 + x2) / 2) == "symbol"]
    bands = []
    for y, t in sym_words:
        if bands and abs(y - bands[-1][0]) < 40:
            bands[-1][1].append(t)
        else:
            bands.append((y, [t]))
    for by, tokens in bands:
        if {t.upper().strip() for t in tokens} <= SECTION_WORDS:
            name = " ".join(tokens).upper()
            if "MUTUAL" in name or "FUNDS" in name:
                headers.append((by, "MUTUAL FUNDS"))
            elif "CASH" in name or "EQUIVALENTS" in name:
                headers.append((by, "CASH & EQUIVALENTS"))
            elif "ETFS" in name:
                headers.append((by, "ETFS"))
            elif "UNITS" in name:
                headers.append((by, "UNITS"))
            elif "STOCKS" in name:
                headers.append((by, "STOCKS"))

    # Rows are anchored on the market-value column: always present, and a
    # long string the OCR engine never drops.
    mv_words = [(y, x1, x2, t) for y, x1, x2, t in words
                if col_of((x1 + x2) / 2) == "market_value"]
    rows = []  # list of y0 (main line y)
    for y, x1, x2, t in mv_words:
        if rows and abs(y - rows[-1]) < 35:
            continue
        rows.append(y)

    header_idx = 0
    headers.sort()
    for y0 in rows:
        # Apply any section header that appears above this row.
        while header_idx < len(headers) and headers[header_idx][0] < y0:
            section = headers[header_idx][1]
            header_idx += 1

        cells_main = {}
        cells_pct = {}
        sym_parts = []
        for y, x1, x2, t in words:
            col = col_of((x1 + x2) / 2)
            if col is None:
                continue
            if col == "symbol":
                if y0 - 18 <= y <= y0 + 60:
                    sym_parts.append((x1, t))
            elif y0 - 18 <= y <= y0 + 20:
                cells_main.setdefault(col, []).append((x1, t))
            elif y0 + 20 < y <= y0 + 78:
                cells_pct.setdefault(col, []).append((x1, t))

        row_warnings = []
        MONEY_COLS = {"price", "price_change", "market_value",
                      "day_change", "cost_basis", "gain_loss"}

        def raw_cell(col, pct=False):
            src = cells_pct if pct else cells_main
            return "".join(t for _, t in sorted(src.get(col, [])))

        def cell(col, pct=False):
            raw = raw_cell(col, pct)
            val = clean_number(raw) if raw else ""

            # Decide whether the full-page OCR result looks trustworthy.
            # The engine drops short or fragmented tokens, so retry a
            # zoomed-in crop of just this cell when anything looks off.
            suspicious = (val is None) or (raw == "")
            if not pct and col in MONEY_COLS and raw and "$" not in raw:
                suspicious = True  # money cells always carry a '$'
            if (pct or col == "portfolio") and val and re.search(r"\.\d{3,}$", val):
                suspicious = True  # percents have at most 2 decimals

            if suspicious:
                lo, hi = bin_px(col)
                y1, y2 = (y0 + 34, y0 + 86) if pct else (y0 - 14, y0 + 36)
                fallback = ""
                for cand in ocr_crop_candidates(img, lo, y1, hi, y2):
                    v = clean_number(cand)
                    if not v:
                        continue
                    if pct or col == "portfolio":
                        if re.search(r"\.\d{3,}$", v):
                            continue  # percents never have 3+ decimals
                        return v
                    if col in MONEY_COLS:
                        if "$" in cand:
                            return v  # a complete money cell includes the '$'
                        fallback = fallback or v
                    else:
                        return v
                if fallback:
                    return fallback
                if val is None:
                    row_warnings.append(
                        f"page {pageno} row at y={y0} {col}{' pct' if pct else ''}: cannot parse {raw!r}")
                    return ""
            return val or ""

        qty = cell("quantity")
        mv = cell("market_value")
        if not qty or not mv:
            continue  # not a data row

        symbol = clean_symbol("".join(t for _, t in sorted(sym_parts)))
        if not symbol:
            symbol = ocr_symbol_with_context(img, width, y0)
        if not symbol:
            symbol = "???"
            row_warnings.append(f"page {pageno} row at y={y0}: symbol not readable")

        price = cell("price")
        # Sanity guard: a real row satisfies price x quantity = market value.
        if price:
            try:
                if abs(float(price) * float(qty) - float(mv)) > max(1.0, 0.05 * float(mv)):
                    row_warnings.append(
                        f"page {pageno} {symbol}: price*qty != market value, check this row")
            except ValueError:
                pass

        rec = {
            "section": section,
            "symbol": symbol,
            "price": price,
            "price_change": cell("price_change"),
            "price_change_pct": cell("price_change", pct=True),
            "quantity": qty,
            "market_value": mv,
            "day_change": cell("day_change"),
            "day_change_pct": cell("day_change", pct=True),
            "cost_basis": cell("cost_basis"),
            "gain_loss": cell("gain_loss"),
            "gain_loss_pct": cell("gain_loss", pct=True),
            "portfolio_pct": cell("portfolio"),
        }

        # Market value - cost basis = gain/loss must hold; when it doesn't,
        # one of the two was misread (usually a clipped leading digit).
        # Repair whichever candidate best agrees with the gain/loss percent.
        try:
            mvf = float(rec["market_value"])
            gl = float(rec["gain_loss"]) if rec["gain_loss"] else None
            cb = float(rec["cost_basis"]) if rec["cost_basis"] else None
            glp = float(rec["gain_loss_pct"]) if rec["gain_loss_pct"] else None
            if gl is not None and cb is not None and abs(mvf - cb - gl) > 1.0:
                gl2, cb2 = mvf - cb, mvf - gl
                if glp is not None and cb != 0 and cb2 != 0:
                    err_fix_gl = abs(100 * gl2 / cb - glp)
                    err_fix_cb = abs(100 * gl / cb2 - glp)
                    if err_fix_gl <= err_fix_cb:
                        rec["gain_loss"] = f"{gl2:.2f}"
                    else:
                        rec["cost_basis"] = f"{cb2:.2f}"
                    row_warnings.append(
                        f"page {pageno} {symbol}: repaired gain_loss/cost_basis from arithmetic")
                else:
                    row_warnings.append(
                        f"page {pageno} {symbol}: gain_loss/cost_basis inconsistent, check row")
            elif gl is None and cb is not None:
                rec["gain_loss"] = f"{mvf - cb:.2f}"
            elif cb is None and gl is not None and rec["section"] != "CASH & EQUIVALENTS":
                rec["cost_basis"] = f"{mvf - gl:.2f}"
        except ValueError:
            pass

        # A value and its own percentage always share the same sign; the
        # OCR sometimes loses the minus on the parenthesized percent.
        for vc, pc in (("price_change", "price_change_pct"),
                       ("day_change", "day_change_pct"),
                       ("gain_loss", "gain_loss_pct")):
            v, p = rec[vc], rec[pc]
            if v and p and v.startswith("-") != p.startswith("-"):
                rec[pc] = ("-" + p.lstrip("-")) if v.startswith("-") else p.lstrip("-")

        # Derive a missing percent from its dollar pair, or replace one that
        # is wildly inconsistent with it (a sign of OCR junk). The statement
        # truncates percentages toward zero, so do the same.
        def derive(pct_key, num, den):
            if not num or not den:
                return
            try:
                implied = 100 * float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                return
            current = rec[pct_key]
            if current:
                try:
                    if abs(float(current) - implied) <= 0.5:
                        return  # consistent, keep the OCR value
                except ValueError:
                    pass
            rec[pct_key] = f"{int(implied * 100) / 100:.2f}"
            row_warnings.append(
                f"page {pageno} {symbol} {pct_key}: derived arithmetically"
                + (f" (OCR said {current!r})" if current else ""))

        if rec["price_change"] and rec["price"]:
            derive("price_change_pct", rec["price_change"],
                   str(float(rec["price"]) - float(rec["price_change"])))
        if rec["day_change"] and rec["market_value"]:
            derive("day_change_pct", rec["day_change"],
                   str(float(rec["market_value"]) - float(rec["day_change"])))
        derive("gain_loss_pct", rec["gain_loss"], rec["cost_basis"])

        # The statement prints percents with exactly 2 decimals; extra
        # digits are OCR junk, so truncate them away.
        for k in ("price_change_pct", "day_change_pct", "gain_loss_pct"):
            v = rec[k]
            if v and "." in v and len(v.split(".")[1]) > 2:
                f = float(v)
                rec[k] = f"{int(f * 100) / 100:.2f}"

        records.append(rec)
        warnings.extend(row_warnings)

    # Apply trailing headers (e.g. a section header at the bottom of a page).
    while header_idx < len(headers):
        section = headers[header_idx][1]
        header_idx += 1
    return section


def parse_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    records = []
    warnings = []
    section = ""
    for pageno, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=DPI)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        section = parse_page(img, pageno, section, records, warnings)

    # Portfolio % must match each row's share of the summed market value.
    total = sum(float(r["market_value"]) for r in records if r["market_value"])
    if total > 0:
        for r in records:
            if not r["market_value"]:
                continue
            implied = 100 * float(r["market_value"]) / total
            cur = r["portfolio_pct"]
            bad = not cur
            if cur:
                try:
                    bad = abs(float(cur) - implied) > 0.25
                except ValueError:
                    bad = True
            if bad:
                r["portfolio_pct"] = f"{int(implied * 100) / 100:.2f}"
                warnings.append(f"{r['symbol']}: portfolio_pct corrected (OCR said {cur!r})")
            elif "." in cur and len(cur.split(".")[1]) > 2:
                r["portfolio_pct"] = f"{int(float(cur) * 100) / 100:.2f}"
    return records, warnings


def validate(records):
    issues = []
    for r in records:
        try:
            calc = float(r["price"]) * float(r["quantity"])
            mv = float(r["market_value"])
        except (ValueError, TypeError):
            continue
        if abs(calc - mv) > max(1.0, 0.005 * mv):
            issues.append(f"{r['symbol']}: price*qty={calc:.2f} but market_value={mv:.2f}")
    return issues


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    pdf_path, csv_path = sys.argv[1], sys.argv[2]

    records, warnings = parse_pdf(pdf_path)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(records)

    print(f"Wrote {len(records)} holdings to {csv_path}")
    for w in warnings:
        print("WARNING:", w)
    issues = validate(records)
    if issues:
        print(f"\n{len(issues)} rows failed the price x quantity check (verify by eye):")
        for i in issues:
            print(" ", i)
    else:
        print("All rows passed the price x quantity = market value check.")


if __name__ == "__main__":
    main()
