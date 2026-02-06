import io
import os
import re
import time
import fitz  # PyMuPDF
import requests
from treepoem import generate_barcode
from random import choice, randint
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional


# =========================
# ✅ PATHS
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PDF = os.path.join(BASE_DIR, "template_usps.pdf")
NIMBUS_BOLD_PATH = os.path.join(BASE_DIR, "NimbusSanL-Bold.otf")


# =========================
# Pydantic models (DATA)
# =========================
class Address(BaseModel):
    name: str
    address: str
    city: str
    state: str
    zip: str


class LabelRequest(BaseModel):
    from_addr: Address = Field(..., alias="from")
    to_addr: Address = Field(..., alias="to")
    weight: str
    zone: Optional[str] = None  # will be overwritten if USPS returns a zone
    tracking: str
    date: str  # "01/13/2026" (used for USPS lookup + printed date)
    delivery_date: Optional[str] = None


# =========================
# Generator Class
# =========================
class USPSLabelGenerator:
    def __init__(self, template_pdf_path: str, nimbus_bold_path: str):
        self.template_pdf_path = template_pdf_path
        self.nimbus_bold_path = nimbus_bold_path
        self.session = requests.Session()

        if not os.path.exists(self.template_pdf_path):
            raise FileNotFoundError(f"Template PDF not found: {self.template_pdf_path}")

    @staticmethod
    def _png_bytes_from_pil(pil_img) -> bytes:
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def find_matrix_slots(page, want=2):
        """
        Find the existing 2 DataMatrix-like image rectangles in the template.
        Heuristic: pick the smallest square-ish images on the page.
        This makes placement pixel-perfect because we reuse the template's image rects.
        """
        candidates = []

        # Each image xref can appear multiple times with different rects
        for img in page.get_images(full=True):
            xref = img[0]
            rects = page.get_image_rects(xref)
            for r in rects:
                w, h = r.width, r.height
                if w <= 0 or h <= 0:
                    continue

                # square-ish
                ar = w / h if h else 999
                if ar < 0.85 or ar > 1.18:
                    continue

                # typical datamatrix size range on a 4x6 label template
                if w < 20 or h < 20:
                    continue
                if w > 160 or h > 160:
                    continue

                area = w * h
                candidates.append((area, r))

        if not candidates:
            return []

        # Prefer smallest square-ish images (usually the matrices)
        candidates.sort(key=lambda t: (t[0], -t[1].y0))

        slots = []
        for _, r in candidates:
            # dedupe near-identical rects
            if any(
                abs(r.x0 - s.x0) < 1 and abs(r.y0 - s.y0) < 1 and
                abs(r.x1 - s.x1) < 1 and abs(r.y1 - s.y1) < 1
                for s in slots
            ):
                continue
            slots.append(r)
            if len(slots) >= want:
                break

        return slots

    def fetch_usps_zone(self, origin_zip: str, destination_zip: str, shipping_date: str) -> str:
        """
        Calls USPS PostCalc Domestic Zone Chart endpoint and extracts the Zone number.

        shipping_date format expected by USPS endpoint: M/D/YYYY or MM/DD/YYYY (your input "01/13/2026" works)
        Returns zone as string (e.g., "5") or "" if not found.
        """
        origin_zip = re.sub(r"[^0-9]", "", origin_zip or "")
        destination_zip = re.sub(r"[^0-9]", "", destination_zip or "")
        shipping_date = (shipping_date or "").strip()

        if len(origin_zip) != 5 or len(destination_zip) != 5 or not shipping_date:
            return ""

        url = "https://postcalc.usps.com/DomesticZoneChart/GetZone"
        params = {
            "origin": origin_zip,
            "destination": destination_zip,
            "shippingDate": shipping_date,
            "_": str(int(time.time() * 1000)),
        }

        # use only headers (no cookies)
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-LV,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": "https://postcalc.usps.com/domesticzonechart",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }

        try:
            r = self.session.get(url, params=params, headers=headers, timeout=12)
            r.raise_for_status()
            js = r.json()

            zi = (js or {}).get("ZoneInformation", "") or ""
            m = re.search(r"\bThe Zone is\s+(\d+)\b", zi)
            if m:
                return m.group(1)

            for k in ("Zone", "zone", "ZoneNumber", "zoneNumber"):
                if k in (js or {}) and str(js[k]).strip().isdigit():
                    return str(js[k]).strip()

        except Exception as e:
            print(f"Zone Lookup Error: {e}")

        return ""

    @staticmethod
    def _gs1_element_string(to_zip: str, tracking_digits: str) -> str:
        """
        Build a GS1 element string for UCC/EAN-128 and GS1 DataMatrix.

        We use:
          (420) = Ship-to Postal Code
          (91)  = Company internal (used to carry the tracking digits)

        This gives a valid GS1 string while preserving your '420 + zip + tracking' concept.
        """
        clean_zip = re.sub(r"[^0-9]", "", to_zip or "")
        clean_track = re.sub(r"[^0-9]", "", tracking_digits or "")
        return f"(420){clean_zip}(91){clean_track}"

    def generate_pdf_bytes(self, data: dict) -> bytes:
        """
        Generates the final PDF as bytes.
        Expects `data` in the same structure as your DATA payload:
        {
          "from": {...},
          "to": {...},
          "weight": "...",
          "zone": "...",
          "tracking": "...",
          "date": "MM/DD/YYYY",
          "delivery_date": "MM/DD/YYYY"
        }
        """
        doc = fitz.open(self.template_pdf_path)
        page = doc[0]

        # Exact anchors from your template (includes 2 new anchors)
        ANCHORS = {
            "postage_date": "01/27/2026",
            "postage_from": "From 35020",
            "postage_weight": "1 lbs 0 ozs",
            "postage_zone": "Zone 8",
            "s_name": "Nicolas Robert",
            "r_name": "ADAM RANEL",
            "routing": "0003",
            "code": "C003",
            "expected": "Expected Delivery Date: 01/30/2026",
            "tracking_text": "9405 5401 0962 8019 5574 80",
            "barcode_header": "USPS TRACKING #",

            # ✅ RANDOMIZED FIELDS (anchors in template)
            "id_028w": "028W0002310105",
            "id_2000": "2000494248",
        }

        insert_queue = []
        found_coords = {}

        # Auto-detect the 2 matrix slots from template images (pixel-perfect placement)
        found_coords["matrix_slots"] = self.find_matrix_slots(page, want=2)

        # Pre-generate random replacements (once per label)
        rand_028w = f"0028W000{randint(0, 9999999):07d}"      # 0028W000 + 7 digits
        rand_2000 = f"2000{randint(0, 999999):06d}"           # 2000 + 6 digits

        # ✅ AUTO ZONE LOOKUP (Origin = from.zip, Destination = to.zip, ShippingDate = data['date'])
        origin_zip = (data.get("from", {}) or {}).get("zip", "")
        destination_zip = (data.get("to", {}) or {}).get("zip", "")
        shipping_date = (data.get("date", "") or "").strip()
        looked_up_zone = self.fetch_usps_zone(origin_zip, destination_zip, shipping_date)
        if looked_up_zone:
            data["zone"] = looked_up_zone  # overwrite / ensure consistent

        # PHASE 1: PRECISION REDACTION
        for key, anchor_text in ANCHORS.items():
            rects = page.search_for(anchor_text)
            if not rects:
                continue

            primary_rect = rects[0]
            found_coords[key] = primary_rect

            if key in ["s_name", "r_name"]:
                clear_zone = fitz.Rect(primary_rect.x0, primary_rect.y0, primary_rect.x0 + 190, primary_rect.y1 + 45)
                page.add_redact_annot(clear_zone, fill=(1, 1, 1))

            elif key == "code":
                page.add_redact_annot(
                    fitz.Rect(primary_rect.x0 + 1.5, primary_rect.y0 + 1.5, primary_rect.x1 - 1.5, primary_rect.y1 - 1.5),
                    fill=(1, 1, 1)
                )

            elif key == "barcode_header":
                # Define the barcode box area but REDACT INSIDE IT (keep border lines intact)
                outer = fitz.Rect(
                    primary_rect.x0 - 90,
                    primary_rect.y1 + 1,
                    primary_rect.x1 + 90,
                    primary_rect.y1 + 70
                )

                inner = fitz.Rect(outer.x0 + 3.0, outer.y0 + 3.0, outer.x1 - 3.0, outer.y1 - 3.0)
                page.add_redact_annot(inner, fill=(1, 1, 1))
                found_coords["barcode_slot"] = outer  # keep using OUTER for positioning

            else:
                # default: redact the found text rects safely
                for r in rects:
                    page.add_redact_annot(
                        fitz.Rect(r.x0 + 0.5, r.y0 + 0.5, r.x1 - 0.5, r.y1 - 0.5),
                        fill=(1, 1, 1)
                    )

            insert_queue.append({"rect": primary_rect, "key": key})

        # Redact INSIDE the two matrix slots (keep any border lines intact)
        for mr in found_coords.get("matrix_slots", []):
            inset = 2.2
            inner = fitz.Rect(mr.x0 + inset, mr.y0 + inset, mr.x1 - inset, mr.y1 - inset)
            page.add_redact_annot(inner, fill=(1, 1, 1))

        page.apply_redactions()

        matrices_done = False  # insert both DataMatrix once

        # PHASE 2: INSERTION
        for item in insert_queue:
            rect, key = item["rect"], item["key"]

            # --- INSERT BOTH GS1 DATAMATRIX CODES ONCE ---
            if not matrices_done and found_coords.get("matrix_slots"):
                gs1_data = self._gs1_element_string(
                    to_zip=(data.get("to", {}) or {}).get("zip", ""),
                    tracking_digits=data.get("tracking", "")
                )

                try:
                    # ✅ GS1 DataMatrix
                    dm_img = generate_barcode(
                        barcode_type="gs1datamatrix",
                        data=gs1_data,
                        options={"includetext": False, "parsefull": True}
                    )
                    dm_png = self._png_bytes_from_pil(dm_img)

                    for mr in found_coords["matrix_slots"]:
                        # SQUEEZE AND CENTER inside the existing slot
                        SQUEEZE = 8.0

                        cx = (mr.x0 + mr.x1) / 2.0
                        cy = (mr.y0 + mr.y1) / 2.0
                        w = mr.width - (SQUEEZE * 2.0)
                        h = mr.height - (SQUEEZE * 2.0)

                        if w < 10:
                            w = 10
                        if h < 10:
                            h = 10

                        target = fitz.Rect(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
                        page.insert_image(target, stream=dm_png, keep_proportion=False)

                except Exception as e:
                    print(f"DataMatrix Error: {e}")

                matrices_done = True

            # --- CARRIER CODE (C003) ---
            if key == "code":
                carrier_code = ['C003', 'C008', 'C013', 'R144', 'C001']
                text = choice(carrier_code)
                size = 12.8

                if os.path.exists(self.nimbus_bold_path):
                    nim_font = fitz.Font(fontfile=self.nimbus_bold_path)
                    text_w = nim_font.text_length(text, fontsize=size)
                    page.insert_font(fontname="nimbus-bold", fontfile=self.nimbus_bold_path)
                    target_font = "nimbus-bold"
                else:
                    text_w = fitz.get_text_length(text, fontname="hebo", fontsize=size)
                    target_font = "hebo"

                x_center = rect.x0 + (rect.width - text_w) / 2
                page.insert_text(point=(x_center, rect.y1 - 3), text=text, fontname=target_font, fontsize=size)
                page.draw_rect(rect, color=(0, 0, 0), width=0.8)

            # --- BOTTOM LARGE BARCODE + DIGITS (UCC/EAN-128 / GS1-128) ---
            elif key == "barcode_header":
                gs1_data = self._gs1_element_string(
                    to_zip=(data.get("to", {}) or {}).get("zip", ""),
                    tracking_digits=data.get("tracking", "")
                )

                try:
                    clean_track = re.sub(r"[^0-9]", "", data.get("tracking", ""))
                    clean_zip = re.sub(r"[^0-9]", "", (data.get("to", {}) or {}).get("zip", ""))

                    barcode_content = f"420{clean_zip}\x1D{clean_track}"  # /f == GS
                    img = generate_barcode(
                        barcode_type="code128",
                        data=barcode_content,
                        options={"includetext": False}
                    )
                    # ✅ UCC/EAN-128 == GS1-128
                    #img = generate_barcode(
                    #    barcode_type="gs1-128",
                    #    data=gs1_data,
                    #    options={"includetext": False, "parsefull": True}
                    #)
                    barcode_png = self._png_bytes_from_pil(img)

                    slot = found_coords["barcode_slot"]

                    # --- MATCH TEMPLATE (SQUEEZE WIDTH + STRETCH HEIGHT) ---
                    SIDE_PAD = 6.0
                    SQUEEZE_W = 10.0
                    TOP_PAD = 4.0
                    DIGITS_AREA = 20.0
                    STRETCH_UP = 2.0
                    STRETCH_DOWN = 10.0

                    DIGITS_FONT = "hebo"
                    DIGITS_SIZE = 9.5
                    DIGITS_BASELINE_PAD = -7.0

                    bars_area = fitz.Rect(
                        slot.x0 + SIDE_PAD + SQUEEZE_W,
                        slot.y0 + TOP_PAD - STRETCH_UP,
                        slot.x1 - SIDE_PAD - SQUEEZE_W,
                        slot.y1 - DIGITS_AREA + STRETCH_DOWN
                    )

                    page.insert_image(bars_area, stream=barcode_png, keep_proportion=False)

                    # Keep your printed tracking number exactly like before
                    clean_track = re.sub(r"[^0-9]", "", data.get("tracking", ""))
                    display_text = " ".join(re.findall(r".{1,4}", clean_track)).strip()
                    text_w = fitz.get_text_length(display_text, fontname=DIGITS_FONT, fontsize=DIGITS_SIZE)
                    tx = slot.x0 + (slot.width - text_w) / 2.0
                    ty = slot.y1 - DIGITS_BASELINE_PAD
                    page.insert_text(point=(tx, ty), text=display_text, fontname=DIGITS_FONT, fontsize=DIGITS_SIZE)

                except Exception as e:
                    print(f"Barcode Error: {e}")

            # ✅ RANDOM TEXT REPLACEMENTS
            elif key == "id_028w":
                SHIFT_LEFT = 5.5
                page.insert_text(
                    point=(rect.x0 - SHIFT_LEFT, rect.y1 - 1.5),
                    text=rand_028w,
                    fontname="helv",
                    fontsize=8.0
                )

            elif key == "id_2000":
                page.insert_text(
                    point=(rect.x0, rect.y1 - 1.5),
                    text=rand_2000,
                    fontname="helv",
                    fontsize=8.0
                )

            elif key == "expected":
                dd = data.get("delivery_date", "")
                text = f"Expected Delivery Date: {dd}" if dd else "Expected Delivery Date:"
                page.insert_text(
                    point=(rect.x0, rect.y1 - 4),
                    text=text,
                    fontname="helv",
                    fontsize=5.0
                )

            # --- ADDRESSES / POSTAGE ---
            elif key == "s_name":
                lines = [
                    data["from"]["name"],
                    data["from"]["address"],
                    f"{data['from']['city']} {data['from']['state']} {data['from']['zip']}"
                ]
                curr_y = rect.y1 - 1.5
                for t in lines:
                    page.insert_text(point=(rect.x0, curr_y), text=t, fontname="helv", fontsize=8.5)
                    curr_y += 10.0

            elif key == "r_name":
                lines = [
                    data["to"]["name"],
                    data["to"]["address"],
                    f"{data['to']['city']} {data['to']['state']} {data['to']['zip']}"
                ]
                curr_y = rect.y1 - 1.5
                for t in lines:
                    page.insert_text(point=(rect.x0, curr_y), text=t, fontname="helv", fontsize=9.5)
                    curr_y += 11.0

            elif key == "routing":
                tw = fitz.get_text_length("0003", fontname="hebo", fontsize=12.0)
                page.insert_text(point=(rect.x1 - tw, rect.y1), text="0003", fontname="hebo", fontsize=12.0)

            elif key in ["postage_date", "postage_from", "postage_weight", "postage_zone"]:
                val = str(data.get(key.split("_")[1], ""))
                if key == "postage_from":
                    val = f"From {data['from']['zip']}"
                elif key == "postage_zone":
                    val = f"Zone {data.get('zone', '')}"
                page.insert_text(point=(rect.x0, rect.y1 - 1.5), text=val, fontname="helv", fontsize=8.0)

        pdf_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
        doc.close()
        return pdf_bytes


# =========================
# FastAPI Server
# =========================
app = FastAPI(title="USPS Label Generator", version="1.0")

GENERATOR = USPSLabelGenerator(
    template_pdf_path=TEMPLATE_PDF,
    nimbus_bold_path=NIMBUS_BOLD_PATH,
)


@app.post("/generate-label", response_class=StreamingResponse)
def generate_label(req: LabelRequest):
    try:
        data = req.model_dump(by_alias=True)
        pdf_bytes = GENERATOR.generate_pdf_bytes(data)
        if not pdf_bytes:
            raise HTTPException(status_code=500, detail="Failed to generate PDF")
        tracking_number = data.get("tracking", "")
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": "inline; filename=label.pdf",
                "X-Tracking-Number": tracking_number
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {e}") from e

@app.get("/gs-version")
def gs_version():
    result = subprocess.run(["gs", "--version"], capture_output=True, text=True)
    return {"ghostscript_version": result.stdout.strip()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
