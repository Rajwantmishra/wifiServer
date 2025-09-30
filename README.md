# Flask Wi-Fi Drop (Resumable, iPhone-Friendly, Windows-Safe)

Turn your Windows PC into a local Wi-Fi drop box for phones and laptops.  
Supports **large files**, **desktop folder uploads**, **resumable chunked** transfer, and a **Windows-safe finalize** step that survives AV/indexer locks.

> **Scope:** LAN by default (no auth, no HTTPS). Ideal for quick, private offloads. Extend as you like—see “What’s Next”.

---

## Features
- 📱 **Phone-friendly**: simple UI, sequential uploads (reliable on iOS).
- 🗂️ **Folder uploads (desktop)** via `webkitdirectory` and drag-and-drop.
- 🔁 **Resumable**: `/upload/status` + `/upload/chunk` + `/upload/finish` with offset correction (409).
- 🪟 **Windows-safe finalize**: retries `os.replace()` then falls back to `shutil.move`.
- 📊 **Stats**: shows total file count at the destination (excludes temp parts).
- ⚙️ Minimal, readable code—easy to customize.

---

## Quick Start (Windows)

```bash
pip install flask waitress
# run with waitress for concurrency
python -m waitress --listen=0.0.0.0:5000 Server:app
