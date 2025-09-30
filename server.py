import os
import re
import time
import shutil
from pathlib import Path
from flask import Flask, request, render_template_string, send_from_directory, abort, jsonify
from werkzeug.utils import secure_filename

# === Settings ===
UPLOAD_ROOT = Path("D:/iphone8OLD") / "PhoneUploads"  # your target root
TMP_DIR = UPLOAD_ROOT / ".incoming"                    # temp chunks here

app = Flask(__name__)
# No MAX_CONTENT_LENGTH -> allow large, streaming uploads
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

# ---------- helpers ----------
SAFE_SEG = re.compile(r"^[ .A-Za-z0-9_\-()+=@#,&{}!$%^~\[\]]{1,255}$")

def _safe_name(name: str) -> str:
    s = secure_filename(name)
    return s or "upload.bin"

def _safe_relpath(rel: str) -> Path:
    """
    Sanitize a client-provided relative path:
    - split on slashes/backslashes
    - drop empty, '.', '..', and unsafe segments
    - cap depth to avoid abuse
    """
    parts = []
    for raw in (rel or "").replace("\\", "/").split("/"):
        seg = raw.strip()
        if not seg or seg in (".", ".."):
            continue
        if not SAFE_SEG.match(seg):
            seg = "_"
        parts.append(seg[:255])
        if len(parts) >= 50:  # cap depth
            break
    return Path(*parts)

def _tmp_path(name: str, rel: str) -> Path:
    rp = _safe_relpath(rel)
    return (TMP_DIR / rp / (_safe_name(name) + ".part"))

def _final_path(name: str, rel: str) -> Path:
    rp = _safe_relpath(rel)
    return (UPLOAD_ROOT / rp / _safe_name(name))

def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1

def _atomic_move_with_retry(src: Path, dst: Path, attempts: int = 10, delay: float = 0.2):
    """
    Try os.replace repeatedly (handles transient locks on Windows),
    then fall back to shutil.move (cross-volume safe).
    """
    last_err = None
    for i in range(attempts):
        try:
            os.replace(src, dst)  # atomic on same volume
            return
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(delay * (i + 1))  # linear backoff
    try:
        shutil.move(str(src), str(dst))
    except Exception as e:
        raise last_err or e

def _count_files_excluding_tmp(root: Path, tmp_dir: Path) -> int:
    """Count all regular files under root, excluding the temp folder and any .part files."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # Skip the temp directory
        try:
            dirnames.remove(tmp_dir.name)
        except ValueError:
            pass
        total += sum(1 for f in filenames if not f.endswith(".part"))
    return total

# ---------- UI ----------
PAGE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Upload to PC (folders & resumable)</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px}
      .card{max-width:860px;margin:auto;padding:20px;border:1px solid #ddd;border-radius:12px}
      h1{font-size:1.25rem;margin:0 0 8px}
      .muted{color:#666;font-size:0.9rem}
      .row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}
      button{padding:10px 14px;border-radius:10px;border:1px solid #222;background:#222;color:#fff;cursor:pointer}
      button.ghost{background:#fff;color:#222}
      #drop{border:2px dashed #999;padding:18px;border-radius:12px;text-align:center;margin:12px 0}
      #drop.drag{border-color:#000}
      #queue{margin-top:8px}
      .item{display:flex;gap:8px;align-items:center;justify-content:space-between;border:1px solid #eee;border-radius:8px;padding:8px 10px;margin:6px 0}
      .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .path{color:#555;font-size:0.85rem}
      .size{white-space:nowrap;color:#555;font-variant-numeric:tabular-nums}
      .rm{background:#fff;color:#a00;border-color:#a00}
      progress{width:260px;height:16px}
      .ok{color:#0a0}
      .err{color:#a00}
      code{background:#fafafa;border:1px solid #eee;padding:2px 6px;border-radius:6px}
      .stats{margin-top:16px;padding:10px;border:1px dashed #ccc;border-radius:8px;background:#fafafa}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Upload files to this PC</h1>
      <p class="muted">Saved under <code>{{ upload_dir }}</code></p>

      <div class="row">
        <button type="button" id="btnFiles">Add from Files</button>
        <button type="button" id="btnPhotos">Add Photos/Videos</button>
        <button type="button" id="btnFolder">Add Folder (desktop)</button>
        <button type="button" id="btnClear" class="ghost">Clear list</button>
      </div>

      <!-- hidden pickers -->
      <input id="pickFiles" type="file" multiple style="display:none">
      <input id="pickPhotos" type="file" accept="image/*,video/*" multiple style="display:none">
      <input id="pickFolder" type="file" webkitdirectory directory multiple style="display:none">

      <div id="drop">Drag & drop files or folders here (desktop) — or use the buttons above</div>

      <div class="row">
        <div class="muted" id="summary">No files selected</div>
        <div style="flex:1"></div>
        <button id="start" type="button">Start upload</button>
      </div>

      <div id="queue"></div>
      <div id="jobs"></div>

      <div class="stats">
        <b>Destination file count:</b> <span id="destCount">{{ file_count }}</span>
        <button type="button" id="refreshStats" class="ghost" style="margin-left:8px">Refresh</button>
      </div>

      <p class="muted" style="margin-top:16px">
        iPhone: to upload a <b>folder</b>, open Files, long-press the folder → <b>Compress</b>, then upload the ZIP here.
        For very large batches, keep the phone plugged in and set Auto-Lock to <b>Never</b>.
      </p>
    </div>

<script>
const drop = document.getElementById('drop');
const btnFiles = document.getElementById('btnFiles');
const btnPhotos = document.getElementById('btnPhotos');
const btnFolder = document.getElementById('btnFolder');
const pickFiles = document.getElementById('pickFiles');
const pickPhotos = document.getElementById('pickPhotos');
const pickFolder = document.getElementById('pickFolder');
const startBtn = document.getElementById('start');
const queueEl = document.getElementById('queue');
const jobs = document.getElementById('jobs');
const summary = document.getElementById('summary');
const refreshBtn = document.getElementById('refreshStats');
const destCountEl = document.getElementById('destCount');

// queue items: {file: File, relpath: "Sub/Dir" }
const queue = [];

function fmtSize(n){
  if (n < 1024) return n + ' B';
  const u = ['KB','MB','GB','TB'];
  let i= -1; do { n = n/1024; i++; } while(n >= 1024 && i < u.length-1);
  return n.toFixed(n < 10 ? 2 : n < 100 ? 1 : 0) + ' ' + u[i];
}

function renderQueue(){
  queueEl.innerHTML = '';
  let total = 0;
  queue.forEach((it, idx) => {
    const f = it.file;
    total += f.size || 0;
    const div = document.createElement('div');
    div.className = 'item';
    const displayPath = it.relpath ? it.relpath + '/' : '';
    div.innerHTML = `
      <div class="name">
        <div>${displayPath}<b>${f.name}</b></div>
        <div class="path">${it.relpath || ''}</div>
      </div>
      <div class="size">${fmtSize(f.size || 0)}</div>
      <button class="rm" data-i="${idx}">Remove</button>`;
    queueEl.appendChild(div);
  });
  summary.textContent = queue.length
    ? `${queue.length} file(s) — ${fmtSize(total)} total`
    : 'No files selected';
}
queueEl.addEventListener('click', e=>{
  if (e.target.classList.contains('rm')){
    const i = +e.target.getAttribute('data-i');
    if (!Number.isNaN(i)) { queue.splice(i,1); renderQueue(); }
  }
});
btnFiles.addEventListener('click', ()=> pickFiles.click());
btnPhotos.addEventListener('click', ()=> pickPhotos.click());
btnFolder.addEventListener('click', ()=> pickFolder.click());

// pickers
pickFiles.addEventListener('change', ()=>{
  for (const f of pickFiles.files) queue.push({file:f, relpath:''});
  pickFiles.value=''; renderQueue();
});
pickPhotos.addEventListener('change', ()=>{
  for (const f of pickPhotos.files) queue.push({file:f, relpath:''});
  pickPhotos.value=''; renderQueue();
});
pickFolder.addEventListener('change', ()=>{
  for (const f of pickFolder.files) {
    let rel = f.webkitRelativePath || '';
    rel = rel.replace(/\\\\/g,'/'); // normalize
    const slash = rel.lastIndexOf('/');
    const relpath = slash > -1 ? rel.slice(0, slash) : '';
    queue.push({file:f, relpath});
  }
  pickFolder.value=''; renderQueue();
});

// drag & drop (desktop)
['dragenter','dragover'].forEach(evt => drop.addEventListener(evt, e => {
  e.preventDefault(); e.stopPropagation(); drop.classList.add('drag');
}));
['dragleave','drop'].forEach(evt => drop.addEventListener(evt, e => {
  e.preventDefault(); e.stopPropagation(); drop.classList.remove('drag');
}));
drop.addEventListener('drop', async (e) => {
  const items = e.dataTransfer?.items;
  if (!items) return;
  const promises = [];
  for (const it of items){
    if (it.kind === 'file' && it.webkitGetAsEntry){
      const entry = it.webkitGetAsEntry();
      if (entry && entry.isDirectory){
        promises.push(readDirectory(entry, entry.name));
      } else {
        const f = it.getAsFile();
        if (f) queue.push({file:f, relpath:''});
      }
    } else if (it.kind === 'file') {
      const f = it.getAsFile();
      if (f) queue.push({file:f, relpath:''});
    }
  }
  await Promise.all(promises);
  renderQueue();
});

// recursively read a DirectoryEntry
function readDirectory(dirEntry, base){
  return new Promise((resolve) => {
    const reader = dirEntry.createReader();
    function readBatch(){
      reader.readEntries(async (entries)=>{
        if (!entries.length) { resolve(); return; }
        for (const ent of entries){
          if (ent.isFile){
            await new Promise(res => ent.file(f => {
              queue.push({file:f, relpath: base});
              res();
            }));
          } else if (ent.isDirectory){
            await readDirectory(ent, base + '/' + ent.name);
          }
        }
        readBatch();
      });
    }
    readBatch();
  });
}

// upload plumbing (resumable)
function addRow(name, relpath) {
  const row = document.createElement('div');
  row.className = 'row';
  const full = (relpath ? relpath + '/' : '') + name;
  row.innerHTML =
    '<div class="name"></div><progress max="100" value="0"></progress><span class="pct muted">0%</span><span class="status muted"></span>';
  row.querySelector('.name').textContent = full;
  jobs.appendChild(row);
  return {
    setProgress: (p) => {
      row.querySelector('progress').value = p;
      row.querySelector('.pct').textContent = Math.floor(p) + '%';
    },
    ok: (msg) => { row.querySelector('.status').textContent = ' ' + msg; row.querySelector('.status').className='status ok'; },
    err: (msg) => { row.querySelector('.status').textContent = ' ' + msg; row.querySelector('.status').className='status err'; },
  };
}

async function getReceived(name, size, relpath) {
  const r = await fetch(`/upload/status?name=${encodeURIComponent(name)}&size=${size}&relpath=${encodeURIComponent(relpath||'')}`);
  if (!r.ok) throw new Error('status failed');
  const j = await r.json();
  return j.received || 0;
}

async function uploadItem(item) {
  const file = item.file;
  const relpath = item.relpath || '';
  const ui = addRow(file.name, relpath);
  const size = file.size || 0;
  let offset = await getReceived(file.name, size, relpath);
  const chunkSize = 16 * 1024 * 1024; // 16MB

  try {
    while (offset < size) {
      const chunk = file.slice(offset, Math.min(offset + chunkSize, size));
      const res = await fetch(`/upload/chunk?name=${encodeURIComponent(file.name)}&size=${size}&offset=${offset}&relpath=${encodeURIComponent(relpath)}`, {
        method: 'POST',
        body: chunk,
      });
      if (res.status === 409) {
        const j = await res.json();
        offset = j.received || 0;
        continue;
      }
      if (!res.ok) throw new Error(await res.text());
      const j = await res.json();
      offset = j.received || (offset + chunk.size);
      ui.setProgress((offset / size) * 100);
    }
    const fin = await fetch(`/upload/finish?name=${encodeURIComponent(file.name)}&size=${size}&relpath=${encodeURIComponent(relpath)}`, { method: 'POST' });
    if (!fin.ok) throw new Error(await fin.text());
    ui.ok('done');
  } catch (e) {
    console.error(e);
    ui.err('failed: ' + (e.message || e));
  }
}

startBtn.addEventListener('click', async ()=>{
  if (!queue.length) return;
  for (const it of queue) { await uploadItem(it); } // sequential = more reliable esp. on iOS
  queue.length = 0;
  renderQueue();
  // Reload so the file count updates
  window.location.reload();
});

// Manual refresh for file count
refreshBtn.addEventListener('click', async ()=>{
  try{
    const r = await fetch('/stats');
    if (!r.ok) throw new Error('stats failed');
    const j = await r.json();
    destCountEl.textContent = j.files;
  }catch(e){
    alert('Could not refresh stats');
  }
});
</script>
  </body>
</html>
"""

# ---------- routes ----------
@app.route("/", methods=["GET"])
def index():
    file_count = _count_files_excluding_tmp(UPLOAD_ROOT, TMP_DIR)
    return render_template_string(PAGE, file_count=file_count, upload_dir=str(UPLOAD_ROOT))

# Simple stats endpoint (used by the "Refresh" button)
@app.get("/stats")
def stats():
    return jsonify({"files": _count_files_excluding_tmp(UPLOAD_ROOT, TMP_DIR)})

# Legacy endpoint kept (small uploads via <form>, still streams in chunks)
@app.post("/upload")
def upload_legacy():
    if "files" not in request.files:
        abort(400, "No files part")
    files = request.files.getlist("files")
    saved = 0
    for f in files:
        if not f or not f.filename.strip():
            continue
        name = _safe_name(f.filename)
        dst = _unique_path(_final_path(name, rel=""))
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as out:
            while True:
                chunk = f.stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        saved += 1
    return (f"Uploaded {saved} file(s). <a href='/'>Back</a>", 200)

# Resumable: query how many bytes already received for (name, relpath)
@app.get("/upload/status")
def upload_status():
    name = request.args.get("name", "")
    size = int(request.args.get("size", "0") or 0)
    relpath = request.args.get("relpath", "")
    if not name or size < 0:
        abort(400, "name/size required")

    tmp = _tmp_path(name, relpath)
    if tmp.exists():
        return jsonify({"received": tmp.stat().st_size})

    final = _final_path(name, relpath)
    if final.exists():
        if not size or final.stat().st_size == size:
            return jsonify({"received": size or final.stat().st_size, "complete": True})
        return jsonify({"received": final.stat().st_size})

    return jsonify({"received": 0})

# Resumable: append chunk at given offset
@app.post("/upload/chunk")
def upload_chunk():
    name = request.args.get("name", "")
    size = int(request.args.get("size", "0") or 0)
    offset = int(request.args.get("offset", "0") or 0)
    relpath = request.args.get("relpath", "")
    if not name or size < 0 or offset < 0:
        abort(400, "name/size/offset required")

    tmp = _tmp_path(name, relpath)
    current = tmp.stat().st_size if tmp.exists() else 0
    if offset != current:
        return jsonify({"received": current}), 409

    tmp.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(tmp, "ab") as out:
        while True:
            chunk = request.stream.read(8 * 1024 * 1024)  # 8MB server-side read
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)

    received = current + written
    if received > size > 0:
        abort(400, "received more bytes than declared size")
    return jsonify({"received": received})

# Resumable: finalize upload (rename .part -> final) — idempotent & robust
@app.post("/upload/finish")
def upload_finish():
    name = request.args.get("name", "")
    size = int(request.args.get("size", "0") or 0)
    relpath = request.args.get("relpath", "")
    if not name:
        abort(400, "name required")

    tmp = _tmp_path(name, relpath)
    final_pref = _final_path(name, relpath)

    if final_pref.exists():
        if not tmp.exists():
            if size == 0 or final_pref.stat().st_size == size:
                return jsonify({"ok": True, "path": str(final_pref), "note": "already finalized"})
        else:
            final_pref = _unique_path(final_pref)

    if not tmp.exists():
        if final_pref.exists():
            return jsonify({"ok": True, "path": str(final_pref), "note": "already finalized"})
        abort(404, "no partial upload found")

    final_pref.parent.mkdir(parents=True, exist_ok=True)

    try:
        _atomic_move_with_retry(tmp, final_pref)
    except FileNotFoundError:
        if final_pref.exists():
            return jsonify({"ok": True, "path": str(final_pref), "note": "finalized concurrently"})
        abort(404, "partial vanished during finalize")
    except Exception as e:
        if final_pref.exists():
            try:
                if not size or final_pref.stat().st_size == size:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    return jsonify({"ok": True, "path": str(final_pref), "note": "final existed after error"})
            except Exception:
                pass
        abort(500, f"finalize failed: {e}")

    return jsonify({"ok": True, "path": str(final_pref)})

@app.route("/downloads/<path:filename>")
def downloads(filename):
    return send_from_directory(UPLOAD_ROOT, filename, as_attachment=False)

if __name__ == "__main__":
    # For marathon sessions you can use waitress:
    #   pip install waitress
    #   python -m waitress --listen=0.0.0.0:5000 Server:app
    app.run(host="0.0.0.0", port=5000, debug=False)
