# Medikiosk — SQLite Backend

This version connects the existing single-file `combined.html` frontend to a SQLite database through a local Python HTTP backend. It is packaged to run without downloading third-party packages.

## What is now implemented

- Patient sign-up before login. The Patient portal opens on **Sign Up** first.
- Patient credentials are stored in SQLite with **hashed passwords**, not plaintext passwords.
- Doctor credentials are stored the same way.
- **Doctor Sign Up is now enabled with strict AI credential screening.** A doctor must provide name, email, password, medical registration number, specialization/hospital details, and upload a medical ID/registration card. The account is created only when the AI screening passes strict name/registration/document/readability/tampering checks.
- Existing seeded demo doctors remain approved so the existing doctor portal continues to work.
- Patient records, appointments, uploaded document metadata, chat messages and AI summaries are stored in SQLite.
- A doctor can open a patient's clinical history only after an accepted appointment where the patient has shared access.
- Doctors can add verified clinical records to an accessible patient's history.
- Uploaded documents are saved under `uploads/` and their metadata is saved in SQLite.

## Run on Windows

1. Install Python 3.10+.
2. Open Command Prompt / PowerShell in this folder.
3. Create a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

4. Start the server:

```powershell
python app.py
```

5. Open `http://127.0.0.1:5000` in your browser.

The database file `medhistory.db` is created automatically on first run.

## Test the patient → doctor flow

1. Choose **PATIENT**.
2. The page opens on **Sign Up** first.
3. Create a new patient account.
4. Log in with that account if you logged out.
5. Open **Appointments** and select the demo doctor.
6. Submit an appointment with **Share my latest structured AI medical overview and records** enabled.
7. Log out.
8. Choose **DOCTOR** and use the demo doctor credentials above.
9. Accept the patient's appointment.
10. Open **Shared Patients** and then **Open Clinical Chart**.
11. The doctor can now see the patient's stored medical history and add a doctor-verified record.

## Important security note

This is a local development/demo backend, not a production medical-record system. Before deploying real medical data, add HTTPS, secure cookie/session handling, CSRF protection, rate limiting, audit logs, encryption at rest, proper doctor identity/credential verification against an authoritative licensing registry, consent management, backups, access revocation, and compliance controls appropriate to the jurisdiction. The built-in AI screening is document screening only; it does not query a government registry.


## Your AI pre-consultation bot
The chat endpoint now uses your supplied Groq/Qwen pre-consultation prompt and conversation-control JSON actions (`ask_question`, `ask_submit`, `end`, `distraction`). The Groq API key is **not stored in the project files**. Configure it as an environment variable before starting the server.

Windows Command Prompt:
```bat
set GROQ_API_KEY=YOUR_GROQ_API_KEY
python app.py
```

PowerShell:
```powershell
$env:GROQ_API_KEY="YOUR_GROQ_API_KEY"
python app.py
```

The bot uses model `qwen/qwen3.6-27b`. Because the API key was pasted into chat, rotate/revoke that key in Groq and use a new key in `GROQ_API_KEY`.


## IMPORTANT: use the new bot version correctly

Do **not** double-click `combined.html`. The screenshot showing an older AI-branded frontend is from an older frontend file. This package's chat UI uses **MY PRE-CONSULTATION BOT** and sends chat messages to `/api/chat/message`, which calls Groq with `qwen/qwen3.6-27b`.

After extracting this ZIP:
1. Open Command Prompt in the extracted folder.
2. Run `pip install -r requirements.txt`.
3. Set your new Groq key with `set GROQ_API_KEY=YOUR_NEW_GROQ_KEY`.
4. Run `python app.py`.
5. Open **http://127.0.0.1:5000** on the host computer.
6. To allow devices on the same Wi-Fi/LAN, run `start.bat` (or `python app.py`). The server now listens on `0.0.0.0:5000`. Find the host computer IPv4 address using `ipconfig` and open `http://HOST-IP:5000` from another device on the same network. Example: `http://192.168.1.10:5000`.
7. On Windows, if Firewall asks, allow Python for **Private networks**. Do not expose port 5000 directly to the public internet.
8. Keep the host computer and server running while other devices use the site.
9. If you previously ran an older version and old AI messages still appear, stop the server, delete `medhistory.db`, restart `python app.py`, and create a fresh patient account. This clears old test conversation history.

Never put the Groq API key into `combined.html`, JavaScript, or Git. Keep it only in the server environment.


## Latest security and booking changes
- The “Send Overview to Doctor & Book Appointment” button now opens the appointment request with the medical overview prepared in the reason field.
- After a successful appointment request, the private patient/AI chat conversation is deleted. The generated medical summary and medical record remain available to the doctor.
- Patient and doctor password fields are masked, autocomplete is disabled, and password fields are cleared after successful authentication. Passwords are stored server-side only as hashes, never plaintext.
- The browser session token is kept in `sessionStorage` instead of persistent `localStorage`, so it is removed when the browser tab/session ends.
- For real deployment, use HTTPS and a production session/cookie system.

## AI document reading

Patient uploads are now analyzed by the same **My Pre-Consultation Bot** model (`qwen/qwen3.6-27b`) used for the patient intake chat.

Supported uploads:
- PDF reports and discharge summaries
- PNG/JPG/JPEG scanned reports
- TXT medical notes

The bot extracts clinically relevant information such as report type, doctor/hospital, date, test values with units, abnormal findings, explicit impressions/diagnoses, medications, and important points. The doctor patient chart then shows both each document's AI summary and a consolidated **AI Document Summary** across the patient's uploaded reports.

For scanned PDFs, the server renders pages and sends them to the vision-capable Qwen model in batches. The implementation analyzes up to 20 PDF pages per upload.

The AI only summarizes information present in the document. It must not be treated as a diagnosis or a replacement for clinician review.


## Same-network access

This version is configured for a trusted local Wi-Fi/LAN demonstration. The Flask server binds to `0.0.0.0`, which means it accepts connections from other devices that can reach the computer on port 5000.

**Host computer:** run `start.bat`, note its IPv4 address (for example `192.168.1.10`), then open `http://127.0.0.1:5000` locally.

**Other phone/laptop:** connect to the same Wi-Fi/router and open `http://192.168.1.10:5000` using the host computer's actual IPv4 address.

For a real global deployment, do not use this LAN mode as the final architecture. Use a production WSGI server, HTTPS, secure cookies, a real database, reverse proxy, access controls, audit logging, and cloud hosting.


## Seeded doctor accounts

This demo build includes 23 doctor accounts supplied for the Medikiosk project. The credentials are synthetic project login credentials; they are not real hospital credentials. Passwords are hashed in the SQLite database.


## New booking recommendation flow
- After the patient generates the AI medical overview, the appointment page shows a dedicated **Hospital** dropdown.
- The **Doctor** dropdown is filtered to doctors whose specialties are related to the patient's AI overview (for example headache-related complaints can prioritize neurology/neurosurgery and general medicine).
- Changing the hospital immediately re-filters the recommended doctors to that hospital.
- If no related specialist exists at the selected hospital, the UI asks the patient to try another hospital; general medicine is used as a fallback where appropriate.

## Responsive design
The frontend now includes responsive layouts for desktop/laptop, tablet, and mobile widths. Appointment forms, doctor signup, navigation, chat controls, cards, and grids collapse into touch-friendly single-column layouts on smaller screens.

## Doctor AI verification
The doctor signup endpoint is `/api/auth/doctor-signup`. The uploaded credential is analyzed by the same multimodal `qwen/qwen3.6-27b` model. Groq documents that this model supports image inputs and JSON mode. The implementation rejects the signup when the document is unclear, is not recognized as a medical professional credential, the name or registration number does not match, tampering is flagged, or AI confidence is below 90. It stores an audit record in `doctor_verifications`.

### Ayurvedic / Traditional Medicine Intake
The pre-consultation bot now collects optional Ayurvedic/traditional-medicine context after the main complaint is understood. Depending on relevance, it can ask about current Ayurvedic medicines/herbal products/oils/kadha/supplements, Ayurvedic practitioner advice, and recent diet/sleep/daily-routine changes. These are recorded as patient-reported context for the clinician; the bot does not diagnose, recommend Ayurveda, or advise replacing prescribed medical care.
