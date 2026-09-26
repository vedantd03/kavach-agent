# Demo runbook — Kavach agent

Everything below is verified against the deployed server. Times assume the
laptop is already set up (§1 done once, well before the demo).

Server: `https://kavach-server-production-d026.up.railway.app`

---

## 1. Set up the laptop (do this once, the night before)

**a. Build the agent** (only if you don't already have `dist-onedir\kavach-agent\`):

```powershell
cd "C:\Users\Rugved Palodkar\BPF\kavach-agent"
pip install -r requirements-dev.txt
$env:KAVACH_ONEDIR="1"; pyinstaller kavach-agent.spec --noconfirm --distpath dist-onedir
```

**b. Copy it somewhere stable.** Not `dist-onedir` (the next build wipes it) and
not `Downloads` (files there can pick up a Mark-of-the-Web warning):

```powershell
Copy-Item -Recurse .\dist-onedir\kavach-agent C:\kavach -Force
```

**c. Create `C:\kavach\.env`** — in Notepad's Save As dialog set *Save as type*
to *All Files*, or it becomes `.env.txt` and is silently ignored:

```
SERVER_URL=https://kavach-server-production-d026.up.railway.app
DEVICE_ID=RUGVED-LAPTOP
AGENT_DB=agent.db
POLL_INTERVAL_SEC=2
```

`POLL_INTERVAL_SEC=2` is the demo setting: the agent picks up a queued scan
within ~2 seconds instead of 10. The folder must end up looking like this —
`_internal\` is required, the exe alone will not start:

```
C:\kavach\
  kavach-agent.exe
  _internal\
  .env
  start-agent.bat
  scan-folder.bat
```

**d. Generate the demo corpus:**

```powershell
python "C:\Users\Rugved Palodkar\BPF\kavach-agent\tools\make_demo_folder.py" C:\kavach\demo_folder
```

34 synthetic files. The Aadhaar and GSTIN numbers carry valid check digits, so
the server's validators fire; `MANIFEST.md` inside it lists what each file
should produce.

**e. Prove it works, before the audience arrives:**

```powershell
cd C:\kavach
.\kavach-agent.exe health                       # prints the server's health JSON
.\kavach-agent.exe scan C:\kavach\demo_folder   # ~100 s, expect ~600 findings
```

Then delete `C:\kavach\agent.db` so the real demo starts clean — otherwise
unchanged-skip will skip every file and you'll find nothing on stage.

---

## 2. The demo (about 4 minutes)

### Terminal 1 — start the agent

```powershell
cd C:\kavach
.\kavach-agent.exe run
```

It prints one line and goes quiet. **Silence is normal** — it only logs when
something happens. Within ~2 s it registers with the server.

> "This is the agent on an employee laptop. It has just checked in with the
> server and is waiting for work."

### Terminal 2 — set up the shell, show the device checked in

```powershell
$S = "https://kavach-server-production-d026.up.railway.app"
Invoke-RestMethod "$S/admin/devices" | Format-Table device_id, last_seen, online
```

### Queue a scan (the portal will do exactly this call)

```powershell
$body = @{ device_id="RUGVED-LAPTOP"; roots=@("C:/kavach/demo_folder"); force=$true } | ConvertTo-Json
$scan = Invoke-RestMethod -Method Post "$S/admin/scans" -ContentType application/json -Body $body
$scan.scan_id
```

Terminal 1 now shows the command arriving, the crawl, and the detect calls.

> "The server queued a scan. The agent polls, accepts it, walks only the folder
> it was given, parses each file and sends the text for detection. No detection
> runs on the laptop."

### Watch it progress

```powershell
while ($true) {
  $v = Invoke-RestMethod "$S/admin/scans/$($scan.scan_id)"
  "{0}  discovered={1} done={2} findings={3}" -f $v.status, $v.files_discovered, $v.files_done, $v.findings
  if ($v.status -eq "completed") { break }
  Start-Sleep 3
}
```

Takes about 100 seconds for the full corpus.

### Show the result

```powershell
Invoke-RestMethod "$S/admin/scans/$($scan.scan_id)" | Select-Object status, files_done, files_unscannable, findings
(Invoke-RestMethod "$S/admin/scans/$($scan.scan_id)").findings_by_tier
Invoke-RestMethod "$S/admin/summary"
```

Expect roughly: 29 discovered, 26 scanned, **3 unscannable**, ~620 findings,
~210 restricted.

---

## 3. What to point at

| Point | Where to show it |
|---|---|
| The agent only scans what it is told | Queue a scan for `C:/kavach/demo_folder/Desktop` — 1 file, not 34 |
| Bulk data raises the tier | `customer_export_aug.csv` → **restricted**, ~360 findings, risk 100 |
| Exposure matters | `OneDrive - Acme/client_list.csv` → `folder_class=synced`, highest weight |
| Nothing sensitive is stored | `.\kavach-agent.exe findings` → masked values only, never a raw number |
| Unscannable files are still reported | `locked.docx` → `encrypted`, `corrupt.pdf` → `corrupt` |
| False positives are rejected | `logistics_tracking.txt` → **0 findings** (12-digit order ids that fail the checksum) |
| Images and scans are handled | 4 OCR pages — png, jpg, tiff and a scanned PDF page, all OCR'd **on the server** |
| It costs nothing to run | 0.01% CPU idle, ~85 MB; a full scan burns 0.38 CPU-seconds |

Show the privacy claim rather than asserting it — this reads the agent's own
database and prints everything it holds about each finding:

```powershell
.\kavach-agent.exe findings --limit 15
```

```
type             masked value     tier           risk  by      file
AADHAAR          XXXXXXXX7752     restricted      100  rules   customer_export_aug.csv
AADHAAR          XXXXXXXX8862     restricted      100  rules   customer_export_aug.csv
...
showing 15 of 623. Stored per finding: masked value + HMAC hash only -
no raw identifier and no file text is ever written to the agent database.
```

> "That is the whole record. Last four digits and a keyed hash. The number
> itself was never written to this laptop, and neither was the file's text."

---

## 4. If something goes wrong

| Symptom | Cause and fix |
|---|---|
| Window flashes and closes on double-click | Old build. Rebuild — the current one starts the daemon on double-click and holds the window open |
| `404 DEVICE_UNKNOWN` when queueing | The agent hasn't polled yet. Start it first, wait ~3 s |
| Agent logs nothing after startup | Normal. Check `/admin/devices` — `last_seen` should stay within ~10 s |
| Scan completes with 0 findings | The DB remembers the files. Delete `agent.db`, or queue with `force=true` |
| `status=rejected`, `PATH_NOT_FOUND` | The path doesn't exist on the laptop. Check the drive letter and use forward slashes |
| Scan sits at `queued` | Another scan is still running — they run one at a time, FIFO |
| Server slow or erroring | The agent retries 429/502/503/504 with 1/2/4/8/16 s backoff and requeues the files. It recovers on its own; just wait |
| Everything is broken, 60 seconds left | Fall back to the local mock: `$env:PYTHONPATH="tools"; python -m uvicorn mock_server:app --port 8000`, point `SERVER_URL` at `http://localhost:8000`, and run the same scan. It returns synthetic findings and needs no network |

**Ctrl+C** stops the agent cleanly. A half-finished scan resumes on restart:
in-flight files go back to the queue and an interrupted crawl restarts.

---

## 5. One-liner reset between runs

```powershell
Stop-Process -Name kavach-agent -Force -ErrorAction SilentlyContinue
Remove-Item C:\kavach\agent.db* -Force -ErrorAction SilentlyContinue
```

Then start from §2. The server keeps its findings across resets; use a fresh
`DEVICE_ID` in `.env` if you want a clean dashboard too.
