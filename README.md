# USPS Label Generator (Template-Based) — FastAPI

This project generates USPS-style 4x6 shipping labels by **cloning a PDF template** and replacing specific fields using **pixel-perfect anchors**.  
It returns the generated PDF from a FastAPI endpoint.

✅ Features:
- Uses a **real template PDF** (`template_usps.pdf`)
- Replaces text fields via **search anchors + redaction + insertion**
- Generates:
  - Barcode
  - Two DataMatrix codes with the same payload (auto-detected slots, centered & squeezed)
- Randomizes:
  - Carrier code (`C003`, `C008`, `C013`, `R144`, `C001`)
  - `0028W000XXXXXXX` (7 random digits)
  - `2000XXXXXX` (6 random digits)
- Auto-fetches USPS **Zone** via:
  - `https://postcalc.usps.com/DomesticZoneChart/GetZone`

---

## Project Structure

```
release_v3/
├─ server.py
├─ template_usps.pdf
├─ NimbusSanL-Bold.otf        
├─ requirements.txt
└─ README.md
```

---

## Requirements (Important)

### 1) Python
- Python **3.10+** recommended

### 2) Ghostscript (required by `treepoem`)
`treepoem` uses BWIPP through Ghostscript. Install Ghostscript:

#### Windows
- Install Ghostscript from the official installer (64-bit).
- After install, confirm `gswin64c.exe` exists (usually in Program Files).
- Ensure Ghostscript is available in PATH (installer often handles it).

#### macOS (Homebrew)
```bash
brew install ghostscript
```

#### Linux (Debian/Ubuntu)
```bash
sudo apt update
sudo apt install -y ghostscript
```

#### Linux (CentOS/RHEL/Fedora)
```bash
sudo dnf install -y ghostscript
```

---

## Install (Windows / macOS / Linux)

### 1) Create and activate a virtual environment

#### Windows (PowerShell)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

#### Windows (CMD)
```cmd
python -m venv venv
venv\Scripts\activate.bat
```

#### macOS / Linux
```bash
python3 -m venv venv
source venv/bin/activate
```

### 2) Install dependencies
```bash
pip install -r requirements.txt
```

---

## Run the Server

From the project folder:

```bash
python server.py
```

Server runs at:
- `http://127.0.0.1:8000`

---

## API Usage

### Endpoint
`POST /generate-label`

### Example Request Body
```json
{
  "from": {
    "name": "WALMART STORE",
    "address": "1123 CROSSWIND DRIVE",
    "city": "MURPHY",
    "state": "TX",
    "zip": "75094"
  },
  "to": {
    "name": "SHARIZA KNECHT",
    "address": "4765 WEATHERVANE DR",
    "city": "JOHNS CREEK",
    "state": "GA",
    "zip": "30022"
  },
  "weight": "5 lbs 0 oz",
  "zone": "5",
  "tracking": "9401 9283 4177 9509 3221 71",
  "date": "01/13/2026",
  "delivery_date": "01/30/2026"
}
```

### cURL (macOS/Linux)
```bash
curl -X POST "http://127.0.0.1:8000/generate-label" \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"name":"WALMART STORE","address":"1123 CROSSWIND DRIVE","city":"MURPHY","state":"TX","zip":"75094"},
    "to": {"name":"SHARIZA KNECHT","address":"4765 WEATHERVANE DR","city":"JOHNS CREEK","state":"GA","zip":"30022"},
    "weight":"5 lbs 0 oz",
    "zone":"5",
    "tracking":"9401 9283 4177 9509 3221 71",
    "date":"01/13/2026",
    "delivery_date":"01/30/2026"
  }' --output label.pdf
```

### cURL (Windows PowerShell)
```powershell
$body = @{
  from = @{ name="WALMART STORE"; address="1123 CROSSWIND DRIVE"; city="MURPHY"; state="TX"; zip="75094" }
  to   = @{ name="SHARIZA KNECHT"; address="4765 WEATHERVANE DR"; city="JOHNS CREEK"; state="GA"; zip="30022" }
  weight="5 lbs 0 oz"
  zone="5"
  tracking="9401 9283 4177 9509 3221 71"
  date="01/13/2026"
  delivery_date="01/30/2026"
} | ConvertTo-Json -Depth 5

Invoke-WebRequest -Uri "http://127.0.0.1:8000/generate-label" `
  -Method POST `
  -ContentType "application/json" `
  -Body $body `
  -OutFile "label.pdf"
```

---

## Notes

### Template Files
Make sure these files exist in the same folder as `server.py`:
- `template_usps.pdf` (required)
- `NimbusSanL-Bold.otf`

### USPS Zone Auto-Lookup
The server calls USPS:
- Origin ZIP = `from.zip`
- Destination ZIP = `to.zip`
- Shipping Date = `date`

If USPS returns a zone, it overwrites `zone` in your request.

---

## Troubleshooting

### 1) `treepoem` / barcode errors
Usually means Ghostscript is missing.

Verify:
```bash
gs --version
```

On Windows, try:
```cmd
where gswin64c
```

### 2) Template not found
You must have:
- `template_usps.pdf` next to `server.py`

### 3) Port already in use
Run on a different port:
```bash
uvicorn server:app --host 0.0.0.0 --port 8080
```

---

## License
Private/internal use.
