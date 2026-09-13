import os
import re
import json
import base64
import mimetypes
import sqlite3
import secrets
import difflib
import shutil
from datetime import datetime, date
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, send_file
try:
    from groq import Groq
except ImportError:
    Groq = None
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
try:
    import pymupdf as fitz  # Modern PyMuPDF import
except ImportError:
    try:
        import fitz  # Backward-compatible PyMuPDF import
    except ImportError:
        fitz = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "medhistory.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('PATIENT','DOCTOR')),
    full_name TEXT NOT NULL,
    phone TEXT,
    date_of_birth TEXT,
    blood_group TEXT DEFAULT 'Unknown',
    gender TEXT,
    emergency_contact TEXT,
    specialization TEXT,
    hospital_affiliation TEXT,
    medical_registration_number TEXT,
    medical_id_number TEXT,
    doctor_verification_status TEXT NOT NULL DEFAULT 'APPROVED',
    doctor_verification_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctor_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    registration_number TEXT NOT NULL,
    medical_id_number TEXT,
    card_file_name TEXT NOT NULL,
    card_stored_name TEXT NOT NULL,
    ai_result TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('APPROVED','REJECTED')) DEFAULT 'REJECTED',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS medical_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    doctor_id INTEGER,
    record_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'PATIENT_REPORTED',
    is_verified INTEGER NOT NULL DEFAULT 0,
    diagnosis_condition TEXT,
    diagnosis_severity TEXT,
    medication_name TEXT,
    medication_dosage TEXT,
    medication_instructions TEXT,
    date_recorded TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(patient_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    doctor_id INTEGER NOT NULL,
    care_path TEXT NOT NULL DEFAULT 'ALLOPATHIC',
    appointment_date TEXT NOT NULL,
    time_slot TEXT NOT NULL,
    reason_for_visit TEXT NOT NULL,
    share_summary INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'PENDING',
    doctor_notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(patient_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    category TEXT NOT NULL,
    extracted_data TEXT NOT NULL DEFAULT '{}',
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(patient_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    care_path TEXT NOT NULL DEFAULT 'ALLOPATHIC',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(patient_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    sender TEXT NOT NULL,
    message_text TEXT NOT NULL,
    extra_metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ai_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    conversation_id INTEGER,
    structured_data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(patient_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    # Migrate databases created by older Medikiosk versions.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, ddl in [
        ("medical_registration_number", "TEXT"),
        ("medical_id_number", "TEXT"),
        ("doctor_verification_status", "TEXT NOT NULL DEFAULT 'APPROVED'"),
        ("doctor_verification_reason", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    # Existing seeded/demo doctors remain approved so current demo accounts keep working.
    conn.execute("UPDATE users SET doctor_verification_status='APPROVED' WHERE role='DOCTOR' AND doctor_verification_status IS NULL")
    conversation_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "care_path" not in conversation_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN care_path TEXT NOT NULL DEFAULT 'ALLOPATHIC'")
    appointment_cols = {row[1] for row in conn.execute("PRAGMA table_info(appointments)").fetchall()}
    if "care_path" not in appointment_cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN care_path TEXT NOT NULL DEFAULT 'ALLOPATHIC'")
    # Seed the project's configured doctor accounts.
    # Passwords are hashed before storage. These are demo/project credentials,
    # not real hospital credentials.
    doctors = [
        ('t.sanjay@medikiosk.local', '7LmlPnQYK6mlsA', 'Dr. T. Sanjay', 'Neurosurgeon', 'Medicover Hospitals'),
        ('r.shravan.kumar@medikiosk.local', 'OxUMyZNEV-k8Jw', 'Dr. R. Shravan Kumar', 'Cardiologist', 'Medicover Hospitals'),
        ('barupathi.laxman@medikiosk.local', 'Dusg25VQlD_sgQ', 'Dr. Barupathi Laxman', 'Urologist', 'Medicover Hospitals'),
        ('banda.naveen.kumar@medikiosk.local', '3RZ8LWlmxnT1xg', 'Dr. Banda Naveen Kumar', 'Orthopedic Surgeon / General Physician', 'Medicover Hospitals'),
        ('gautham.chowhan.m@medikiosk.local', 'cf84p7GpSbWXyA', 'Dr. Gautham Chowhan M', 'Orthopedic Surgeon', 'Medicover Hospitals'),
        ('parimala.surender.rao@medikiosk.local', 'qC-_pJJ_XJSyRw', 'Dr. Parimala Surender Rao', 'Gynecologist', 'Medicover Hospitals'),
        ('priyanka.d@medikiosk.local', 'dnm5BngGT6jj-w', 'Dr. Priyanka D', 'General Medicine', 'Medicover Hospitals'),
        ('sravan.kumar.jogu@medikiosk.local', 'aG9_Akjo7oaBiQ', 'Dr. Sravan Kumar Jogu', 'General Medicine', 'Medicover Hospitals'),
        ('srinivas@medikiosk.local', 'j-MSUpDpdpXfcg', 'Dr. Srinivas', 'General Medicine', 'Medicover Hospitals'),
        ('rama.krishna@medikiosk.local', 'ft2Pbe6jfYBhnw', 'Dr. Rama Krishna', 'General Medicine', 'Times Hospital'),
        ('vidhya.reddy@medikiosk.local', 'Q8e6bqrRwEvgsA', 'Dr. Vidhya Reddy', 'Dermatology / Skin', 'Times Hospital'),
        ('vamshi.krishna@medikiosk.local', 'rxoA9zkTudIwlA', 'Dr. Vamshi Krishna', 'General Surgeon', 'Times Hospital'),
        ('ram.babu@medikiosk.local', '9WCo6cSXAWocHQ', 'Dr. Ram Babu', 'General Medicine', 'Times Hospital'),
        ('uttam.vaidya.gatta@medikiosk.local', 'nAoSHdZ4xqCPng', 'Dr. Uttam Vaidya Gatta', 'Orthopedic Surgery', 'Times Hospital'),
        ('bhageerath.atthe@medikiosk.local', '_MlcOq_JB2aIsQ', 'Dr. Bhageerath Atthe', 'Cardiology', 'Times Hospital'),
        ('thrinath@medikiosk.local', 'NQgrwmMQPbwGcA', 'Dr. Thrinath', 'ENT', 'Times Hospital'),
        ('raghu@medikiosk.local', 'X_8frmHiEuCuig', 'Dr. Raghu', 'Pulmonology', 'Times Hospital'),
        ('sravanth@medikiosk.local', 'N-_f_bFsnqRdHQ', 'Dr. Sravanth', 'General Medicine / Physiotherapy', 'Times Hospital'),
        ('rachana@medikiosk.local', 'EpcZGwcgI22hqw', 'Dr. Rachana', 'General Medicine', 'Times Hospital'),
        ('m.vishwa.bharath.reddy@medikiosk.local', 'jMPsdffBrQ11hQ', 'Dr. M. Vishwa Bharath Reddy', 'General Physician & Diabetologist', 'Srinivasa Hospital'),
        ('n.kruthika@medikiosk.local', 'GUygQ_BNx1niVg', 'Dr. N. Kruthika', 'Ophthalmologist', 'Srinivasa Hospital'),
        ('n.manasa.reddy@medikiosk.local', 'advmA8707SUbsA', 'Dr. N. Manasa Reddy', 'Pediatrician', 'Srinivasa Hospital'),
        ('prabhakar@medikiosk.local', 'UrNBLBRFYfG9Jw', 'Dr. Prabhakar', 'Neuro & Trauma', 'Amrutha Hospital'),
    ]
    for email, password, full_name, specialization, hospital in doctors:
        if not conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            conn.execute(
                """INSERT INTO users
                (email,password_hash,role,full_name,specialization,hospital_affiliation,doctor_verification_status)
                VALUES (?,?,?,?,?,?,?)""",
                (email, generate_password_hash(password), "DOCTOR", full_name, specialization, hospital, "APPROVED"),
            )
    conn.commit()
    conn.close()


def hash_token(token: str) -> str:
    # SHA-256 is sufficient for a random, high-entropy bearer token.
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    conn = db()
    row = conn.execute(
        """SELECT u.* FROM auth_tokens t JOIN users u ON u.id=t.user_id
           WHERE t.token_hash=?""", (hash_token(token),)
    ).fetchone()
    conn.close()
    return row


def require_auth(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify(detail="Authentication required."), 401
            if role and user["role"] != role:
                return jsonify(detail="You do not have permission for this action."), 403
            return fn(user, *args, **kwargs)
        return wrapped
    return decorator


def json_row(row):
    return dict(row) if row else None


GROQ_MODEL = "qwen/qwen3.6-27b"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

LANGUAGE_STYLE_PROMPT = {
    "en": "Use natural, friendly Indian English. Avoid awkward literal translations.",
    "hi": "Use natural conversational Indian Hindi (हिंदी), not a word-for-word translation from English. Keep medical terms understandable.",
    "te": "Use natural conversational Telugu (తెలుగు) as a Telugu-speaking person would normally speak. Do NOT answer in English, Telugu written in English letters, or awkward word-for-word translated English. Prefer simple everyday Telugu while keeping medical terms clear.",
    "ta": "Use natural conversational Tamil (தமிழ்), not literal translated English or Tamil written in English letters.",
    "kn": "Use natural conversational Kannada (ಕನ್ನಡ), not literal translated English or Kannada written in English letters.",
    "ml": "Use natural conversational Malayalam (മലയാളം), not literal translated English or Malayalam written in English letters.",
    "mr": "Use natural conversational Marathi (मराठी), not literal translated English or Marathi written in English letters.",
    "bn": "Use natural conversational Bengali (বাংলা), not literal translated English or Bengali written in English letters.",
    "gu": "Use natural conversational Gujarati (ગુજરાતી), not literal translated English or Gujarati written in English letters.",
    "pa": "Use natural conversational Punjabi (ਪੰਜਾਬੀ), not literal translated English or Punjabi written in English letters.",
    "ur": "Use natural conversational Urdu (اردو), not literal translated English or Urdu written in English letters."
}

MY_BOT_SYSTEM_PROMPT = r"""
You are a friendly multilingual patient pre-consultation assistant.

Your job is to collect information about the patient's MAIN health complaint and prepare the information for a doctor.

You are NOT a doctor.

You must NOT:
- diagnose diseases
- prescribe medicines
- recommend treatments
- give treatment instructions

LANGUAGE
The patient can communicate in ANY language. Understand and respond in the SAME LANGUAGE used by the patient.
Support English, Telugu, Hindi, Tamil, Kannada, Malayalam, Marathi, Bengali, Gujarati, Punjabi, Urdu, and other languages. If the patient mixes languages, use the language mainly used in their latest message. Do not translate into English unless specifically asked.

START
Start naturally with: Hi! I'm here to understand what's bothering you. What is your problem?
Do not ask for the patient's name.

MAIN COMPLAINT
First understand the MAIN health problem. Stay focused on that problem. Ask ONE question at a time. Do not suddenly change to another unrelated problem. Do not repeat questions already answered.

CONVERSATION LENGTH
Normally ask around 5 to 7 useful questions. Do not make it feel like a long medical questionnaire. Use previous answers when deciding what to ask next.

HEADACHE
When relevant, ask about start time, whether present now, location, severity, continuous/intermittent pattern, nausea/vomiting/dizziness/vision changes/fever, and what makes it better or worse. Only ask useful questions.

FEVER
When relevant, ask about start time, whether present now, temperature if known, pattern, chills, cough, sore throat, body pain, headache, vomiting or diarrhea. Only ask useful questions.

STOMACH / ABDOMINAL PAIN
When relevant, ask about start time, whether present now, exact location, severity, continuous/intermittent pattern, nausea/vomiting, diarrhea/constipation, fever, and whether eating affects it. Only ask useful questions.

OTHER PROBLEMS
Naturally ask about when it started, whether still present, severity, location if relevant, important related symptoms, and what makes it better or worse.

AYURVEDIC / TRADITIONAL MEDICINE CONTEXT
After the main complaint is understood, naturally collect up to 2-3 brief questions about Ayurvedic or traditional-medicine use when relevant. Ask ONE question at a time. Do not recommend Ayurveda, herbs, home remedies, doshas, or treatments. Use questions such as:
- Are you currently using any Ayurvedic medicines, herbal products, oils, kadha, or supplements for this problem or any other health issue? If yes, ask what they are and how often they are used.
- Have you consulted an Ayurvedic practitioner for this problem? If yes, ask what was prescribed or advised, without judging whether it is medically correct.
- Have you recently changed your diet, sleep, daily routine, exercise, or stress-management practices because of this problem?
Only ask the questions that add useful context. If the patient does not use Ayurveda/traditional medicine or does not want to discuss it, respect that and continue the normal intake. Never suggest stopping prescribed medicines or replacing medical care with Ayurveda.

PREVIOUS MEDICAL HISTORY
Do NOT routinely ask about previous medical history. Only ask when directly relevant to understanding the current complaint.

DISTRACTION
You are ONLY a patient pre-consultation assistant. If the patient plays games, tells jokes, asks unrelated/coding/entertainment questions, repeatedly sends random or meaningless messages, insults/abuses the bot, or intentionally changes subject, politely end the conversation. Do not argue or insult. Reply in the SAME LANGUAGE.

NO PROBLEM
If the patient says they have no problem anymore, are fine, or everything is fine/okay, end politely. Understand equivalent meanings in any language.

SUMMARY
When enough useful information has been collected, stop asking unnecessary questions. Say in the patient's language that you have enough information, will prepare a short summary for the doctor, and ask whether they would like to submit it. If YES, the conversation is finished. If NO, do not continue asking medical questions; ask whether they want to add anything else.

STYLE
Sound natural, human, friendly and empathetic. Keep responses short. Ask only ONE question at a time. Do not sound robotic. Never diagnose, prescribe medicines, or recommend treatment.

CONVERSATION CONTROL
For EVERY response, return JSON in exactly this format:
{
  "reply": "your response to the patient",
  "action": "ask_question"
}

The action MUST be exactly ONE of:
- ask_question: more useful information is needed
- ask_submit: enough information has been collected and you are asking whether to submit the summary
- end: conversation should finish, including patient saying yes/submit, no problem/fine, or they do not want to add anything else
- distraction: patient is deliberately taking the bot away from the medical consultation; reply must politely end the conversation

Return ONLY valid JSON.
"""

def get_groq_client():
    if Groq is None:
        raise RuntimeError("Groq package is not installed. Run: pip install groq")
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured on the server.")
    return Groq(api_key=GROQ_API_KEY)

def groq_chat(messages):
    client = get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.7,
        max_completion_tokens=200,
        reasoning_effort="none",
        reasoning_format="hidden",
        response_format={"type": "json_object"},
        stream=False
    )
    content = response.choices[0].message.content
    result = json.loads(content)
    reply = str(result.get("reply", "")).strip()
    action = str(result.get("action", "ask_question")).strip()
    if action not in {"ask_question", "ask_submit", "end", "distraction"}:
        action = "ask_question"
    if not reply:
        raise RuntimeError("The AI returned an empty reply.")
    return reply, action


DOCUMENT_SUMMARY_SYSTEM = """You are the document-reading component of MedHistory's My Pre-Consultation Bot.
Read medical reports, prescriptions, lab results, scan reports, discharge summaries, and similar patient documents.
Extract ONLY information actually visible or present in the document. Do not diagnose, invent, or interpret beyond what the report states.
Focus on clinically important facts that a doctor would need: report type, date, doctor/hospital, tests, measured values, units, reference ranges, abnormal/high/low flags, impressions, diagnoses explicitly written in the report, medications, and important recommendations explicitly written in the document.
Ignore logos, page numbers, decorative text, and unrelated administrative filler.
Return valid JSON with exactly these keys:
{
  "document_type":"string",
  "test_or_report_name":"string",
  "doctor_or_hospital_name":"string",
  "report_date":"string",
  "patient_name":"string",
  "test_values_or_findings":["string"],
  "abnormal_findings":["string"],
  "diagnoses_or_impressions":["string"],
  "medications":["string"],
  "important_points":["string"],
  "summary":"1-4 factual sentences",
  "limitations":["string"]
}
Use "Not reported" for missing single-value fields and [] for missing lists. Preserve exact numbers and units. If the document is unclear or unreadable, say so in limitations instead of guessing."""


def _clean_json_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _document_prompt(source_text="", image_count=0):
    extra = "\nDocument text extracted locally:\n" + source_text[:60000] if source_text else ""
    if image_count:
        extra += f"\nThe document also contains {image_count} page/image view(s). Use the images as the primary source for values and layout."
    return DOCUMENT_SUMMARY_SYSTEM + extra + "\nReturn JSON only."


def _normalize_document_result(data, fallback_name):
    base = {
        "document_type": "Medical document",
        "test_or_report_name": fallback_name,
        "doctor_or_hospital_name": "Not reported",
        "report_date": "Not reported",
        "patient_name": "Not reported",
        "test_values_or_findings": [],
        "abnormal_findings": [],
        "diagnoses_or_impressions": [],
        "medications": [],
        "important_points": [],
        "summary": "No clinical summary could be extracted.",
        "limitations": []
    }
    if isinstance(data, dict):
        for key in base:
            if key in data:
                value = data[key]
                if isinstance(base[key], list):
                    base[key] = [str(x).strip() for x in (value if isinstance(value, list) else [value]) if str(x).strip()]
                else:
                    base[key] = str(value).strip() if value is not None else base[key]
    if not base["test_or_report_name"]:
        base["test_or_report_name"] = fallback_name
    return base


def _encode_image_data(path):
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    raw = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _render_pdf_pages(path, max_pages=20):
    if fitz is None:
        return []
    images = []
    pdf = fitz.open(str(path))
    try:
        for idx in range(min(len(pdf), max_pages)):
            page = pdf.load_page(idx)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            images.append((idx + 1, "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii")))
    finally:
        pdf.close()
    return images


def _extract_pdf_text(path):
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
        return "\n\n".join(chunks).strip()
    except Exception:
        return ""


def _groq_document_json(prompt, images=None):
    client = get_groq_client()
    content = [{"type": "text", "text": prompt}]
    for image_url in images or []:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": DOCUMENT_SUMMARY_SYSTEM}, {"role": "user", "content": content}],
        temperature=0.1,
        max_completion_tokens=1200,
        reasoning_effort="none",
        reasoning_format="hidden",
        response_format={"type": "json_object"},
        stream=False
    )
    return _clean_json_object(response.choices[0].message.content)


def analyze_uploaded_document(path, original_name):
    """Read a patient document with the user's Qwen/Groq bot and return structured clinical facts."""
    ext = Path(original_name).suffix.lower()
    if not GROQ_API_KEY or Groq is None:
        raise RuntimeError("GROQ_API_KEY is not configured; the AI document reader cannot analyze this file yet.")

    if ext == ".txt":
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        data = _groq_document_json(_document_prompt(text))
        return _normalize_document_result(data, original_name)

    if ext in {".png", ".jpg", ".jpeg"}:
        data = _groq_document_json(
            _document_prompt("", image_count=1),
            images=[_encode_image_data(path)]
        )
        return _normalize_document_result(data, original_name)

    if ext == ".pdf":
        text = _extract_pdf_text(path)
        # Text PDFs are handled directly first. Scanned/image-heavy PDFs fall back to visual OCR.
        if len(re.sub(r"\s+", "", text)) >= 150:
            data = _groq_document_json(_document_prompt(text))
            result = _normalize_document_result(data, original_name)
            result["limitations"].append("Summary generated from PDF text extraction; page images were not required.")
            return result

        page_images = _render_pdf_pages(path, max_pages=20)
        if not page_images:
            raise RuntimeError("This PDF could not be read. Install pypdf and PyMuPDF, then try again.")

        batch_results = []
        for start in range(0, len(page_images), 5):
            batch = page_images[start:start + 5]
            prompt = _document_prompt(text, image_count=len(batch)) + f"\nThese are PDF pages {batch[0][0]} through {batch[-1][0]}. Extract only what is visible in these pages."
            batch_results.append(_groq_document_json(prompt, images=[img for _, img in batch]))

        if len(batch_results) == 1:
            result = _normalize_document_result(batch_results[0], original_name)
        else:
            merged = {
                "document_type": "Medical document",
                "test_or_report_name": original_name,
                "doctor_or_hospital_name": "Not reported",
                "report_date": "Not reported",
                "patient_name": "Not reported",
                "test_values_or_findings": [], "abnormal_findings": [], "diagnoses_or_impressions": [],
                "medications": [], "important_points": [], "summary": "", "limitations": []
            }
            for item in batch_results:
                item = _normalize_document_result(item, original_name)
                for key in ["test_values_or_findings", "abnormal_findings", "diagnoses_or_impressions", "medications", "important_points"]:
                    merged[key].extend(item[key])
                for key in ["doctor_or_hospital_name", "report_date", "patient_name", "document_type"]:
                    if merged[key] == "Not reported" and item[key] != "Not reported":
                        merged[key] = item[key]
                if item["summary"] and item["summary"] not in merged["summary"]:
                    merged["summary"] += (" " if merged["summary"] else "") + item["summary"]
                merged["limitations"].extend(item["limitations"])
            result = _normalize_document_result(merged, original_name)
            result["limitations"].append("Visual extraction was performed in page batches; the PDF contained more than one analyzed page batch.")

        # De-duplicate extracted lists while preserving order.
        for key in ["test_values_or_findings", "abnormal_findings", "diagnoses_or_impressions", "medications", "important_points", "limitations"]:
            result[key] = list(dict.fromkeys(result[key]))
        return result

    raise RuntimeError("Unsupported document type.")


def summarize_documents_for_doctor(docs):
    """Create one concise clinician-facing summary across all patient-uploaded documents."""
    if not docs:
        return "No patient-uploaded documents are available."
    if not GROQ_API_KEY or Groq is None:
        parts = []
        for d in docs:
            e = d.get("extracted_data") or {}
            if e.get("summary"):
                parts.append(f'{d.get("file_name", "Document")}: {e["summary"]}')
        return " ".join(parts) or "Documents are available for review, but no AI summary was generated."
    payload = []
    for d in docs:
        e = d.get("extracted_data") or {}
        payload.append({
            "file_name": d.get("file_name"),
            "category": d.get("category"),
            "document_type": e.get("document_type"),
            "report_date": e.get("report_date"),
            "findings": e.get("test_values_or_findings", []),
            "abnormal_findings": e.get("abnormal_findings", []),
            "impressions": e.get("diagnoses_or_impressions", []),
            "medications": e.get("medications", []),
            "important_points": e.get("important_points", []),
            "summary": e.get("summary", "")
        })
    prompt = """Create a concise doctor-facing summary of the patient's uploaded medical documents.
Include only facts explicitly extracted from the documents. Prioritize abnormal results, important measurements with units, explicit impressions/diagnoses, medications, and clinically important points. Mention normal findings only when they materially help interpretation. Do not diagnose or add advice not present in the documents. Use 3-6 short bullet-style sentences. Start with 'Document summary:' and keep it easy to scan.
DOCUMENTS:\n""" + json.dumps(payload, ensure_ascii=False)
    try:
        response = get_groq_client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": "You summarize extracted medical-document facts for a doctor. Never invent or diagnose."}, {"role": "user", "content": prompt}],
            temperature=0.1, max_completion_tokens=700, reasoning_effort="none", reasoning_format="hidden", stream=False
        )
        return str(response.choices[0].message.content).strip()
    except Exception:
        parts = []
        for d in docs:
            e = d.get("extracted_data") or {}
            if e.get("summary"):
                parts.append(f'{d.get("file_name", "Document")}: {e["summary"]}')
        return " ".join(parts) or "Documents are available for review."


def emergency_flag(text):
    terms = ["chest pain", "difficulty breathing", "can't breathe", "cannot breathe", "severe bleeding", "unconscious", "stroke", "suicide"]
    low = text.lower()
    return any(t in low for t in terms)


def fallback_medical_summary(messages):
    """Extract only clinically relevant facts from the intake, ignoring submit/ack chatter."""
    import re as _re
    patient = [str(m["message_text"]).strip() for m in messages if m["sender"] == "PATIENT"]
    text = " ".join(x for x in patient if x)
    low = text.lower()
    medical_patterns = [
        (r"\bfever\b", "Fever"), (r"\bcough\b", "Cough"),
        (r"\bheadache\b", "Headache"), (r"\bsore throat\b", "Sore throat"),
        (r"\babdominal pain\b", "Abdominal pain"), (r"\bstomach pain\b", "Stomach pain"),
        (r"\bchest pain\b", "Chest pain"), (r"\bvomit(?:ing)?\b", "Vomiting"),
        (r"\bdiarrh(?:ea|hoea)\b", "Diarrhea"), (r"\bdizz(?:y|iness)\b", "Dizziness"),
        (r"\bnausea\b", "Nausea"), (r"\bbreath(?:ing)? difficulty\b", "Breathing difficulty"),
        (r"\bbody pain\b", "Body pain"),
    ]
    chief = None
    for msg in patient:
        ml = msg.lower()
        for pat, label in medical_patterns:
            if _re.search(pat, ml):
                chief = msg if len(msg) <= 100 and not any(x in ml for x in ["submit", "anything else"]) else label
                break
        if chief:
            break
    chief = chief or "No specific medical complaint recorded"
    symptoms = [label for pat, label in medical_patterns if _re.search(pat, low)]
    duration = None
    m = _re.search(r"(\d+)\s*(day|days|week|weeks|month|months|hour|hours|year|years)\s*(ago|for)?", low)
    if m:
        duration = f"{m.group(1)} {m.group(2)}" + (" ago" if m.group(3) == "ago" else "")
    temperature = None
    tm = _re.search(r"(\d+(?:\.\d+)?)\s*°?\s*([fc])\b", low)
    if tm:
        temperature = f"{tm.group(1)}°{tm.group(2).upper()}"
    current_status = None
    negatives = []
    pairs = []
    pending_question = ""
    for m in messages:
        role = m["sender"]
        msg = str(m["message_text"]).strip()
        if role == "AI":
            pending_question = msg.lower()
        elif role == "PATIENT" and pending_question:
            pairs.append((pending_question, msg.lower()))
            pending_question = ""
    for question, answer in pairs:
        if "still have the fever" in question or ("fever" in question and "right now" in question):
            if answer in {"yes", "yeah", "yep", "yes."}:
                current_status = "Fever currently present"
            elif answer.startswith("no"):
                current_status = "No current fever reported"
        for term, label in [
            ("chills", "Chills: No"), ("cough", "Cough: No"),
            ("sore throat", "Sore throat: No"), ("body pain", "Body pain: No"),
            ("headache", "Headache: No"), ("vomiting", "Vomiting: No"),
            ("diarrhea", "Diarrhea: No")]:
            if term in question and answer.startswith("no"):
                negatives.append(label)
    for pat, label in [
        (r"no\s+(?:chills?|chill)", "Chills: No"), (r"no\s+cough", "Cough: No"),
        (r"no\s+sore throat", "Sore throat: No"), (r"no\s+body pain", "Body pain: No"),
        (r"no\s+headache", "Headache: No"), (r"no\s+(?:vomiting|vomit)", "Vomiting: No"),
        (r"no\s+diarrh(?:ea|hoea)", "Diarrhea: No")]:
        if _re.search(pat, low):
            negatives.append(label)
    negatives = list(dict.fromkeys(negatives))
    if current_status is None and _re.search(r"\b(still have|currently|right now)\b.*\bfever\b|\bfever\b.*\b(still|currently|right now)\b", low):
        current_status = "Fever currently present"
    return {
        "care_preference": "Not reported",
        "chief_complaint": chief, "duration": duration or "Not clearly reported",
        "severity": "Needs clinical assessment", "symptoms": symptoms,
        "current_status": current_status or "Not clearly reported", "temperature": temperature or "Not reported",
        "associated_findings": negatives, "existing_conditions": [], "medications": [], "allergies": [],
        "ayurvedic_medicines": [], "ayurvedic_practitioner": "Not reported",
        "diet_lifestyle_context": [],
        "warnings": ["Urgent symptoms were mentioned. Seek immediate professional care."] if emergency_flag(text) else [],
        "ai_summary": "Medical information extracted from the patient intake. Non-medical chat, acknowledgements, and submission text are excluded. This is not a diagnosis."
    }


def groq_medical_summary(messages):
    """Ask the user's bot provider to extract only clinically relevant intake facts."""
    if not GROQ_API_KEY or Groq is None:
        return fallback_medical_summary(messages)
    transcript = []
    for m in messages:
        role = "Patient" if m["sender"] == "PATIENT" else "Assistant"
        content = str(m["message_text"]).strip()
        if content: transcript.append(f"{role}: {content}")
    prompt = """Create a concise clinician-facing medical intake summary from the conversation below.
ONLY include medically relevant information explicitly stated by the patient or clearly established by the assistant's questions and patient answers.
IGNORE greetings, acknowledgements, submit/confirmation messages, filler, and unrelated conversation.
Do not diagnose, infer causes, or invent missing facts.
Return JSON only with exactly these keys:
{
  "care_preference": "Allopathic or Ayurvedic or Not reported",
  "chief_complaint": "string",
  "duration": "string",
  "severity": "string",
  "symptoms": ["string"],
  "current_status": "string",
  "temperature": "string",
  "associated_findings": ["string"],
  "existing_conditions": ["string"],
  "medications": ["string"],
  "allergies": ["string"],
  "ayurvedic_medicines": ["string"],
  "ayurvedic_practitioner": "string",
  "diet_lifestyle_context": ["string"],
  "warnings": ["string"],
  "ai_summary": "string"
}
Use Not reported or an empty array when a fact is not provided. Preserve important negative answers such as No vomiting when clinically relevant. Keep ai_summary to 1-3 factual sentences and do not diagnose.

Conversation:
""" + "\n".join(transcript)
    try:
        client = get_groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role":"system","content":"You are a medical-information extraction assistant. Extract facts only; do not diagnose."}, {"role":"user","content":prompt}],
            temperature=0.1, max_completion_tokens=500, reasoning_effort="none", reasoning_format="hidden",
            response_format={"type":"json_object"}, stream=False)
        data = json.loads(response.choices[0].message.content)
        base = fallback_medical_summary(messages)
        for key in base:
            if key in data: base[key] = data[key]
        return base
    except Exception:
        return fallback_medical_summary(messages)


def build_simple_summary(messages):
    return groq_medical_summary(messages)


@app.route("/")
def index():
    return send_file(BASE_DIR / "combined.html")


@app.route("/health")
def health():
    return jsonify(status="ok", database=str(DB_PATH.name))


def _normalize_credential_text(value):
    value = str(value or "").upper().strip()
    return re.sub(r"[^A-Z0-9]", "", value)


def _name_similarity(a, b):
    aa = re.sub(r"[^A-Z0-9 ]", "", str(a or "").upper()).strip()
    bb = re.sub(r"[^A-Z0-9 ]", "", str(b or "").upper()).strip()
    if not aa or not bb:
        return 0.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def _verify_doctor_identity_card(card_path, card_name, full_name, registration_number):
    """Strict AI-only screening of a newly uploaded professional medical ID card.

    This verifies that the uploaded document visibly appears to be a medical
    professional credential and that the name/registration number match the
    signup fields. It does NOT connect to a government/licensing registry, so
    it must never be described as legal credential verification.
    """
    if not GROQ_API_KEY or Groq is None:
        raise RuntimeError("AI verification is unavailable because GROQ_API_KEY is not configured on the server.")
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for medical ID verification.")

    ext = Path(card_name).suffix.lower()
    image_bytes = None
    mime = None
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        image_bytes = Path(card_path).read_bytes()
        mime = {".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".webp":"image/webp"}[ext]
    elif ext == ".pdf":
        doc = fitz.open(str(card_path))
        if not doc.page_count:
            doc.close()
            raise RuntimeError("The uploaded medical ID PDF has no readable pages.")
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
        image_bytes = pix.tobytes("png")
        mime = "image/png"
        doc.close()
    else:
        raise RuntimeError("Medical ID card must be PNG, JPG, JPEG, WEBP, or PDF.")

    if len(image_bytes) > 18 * 1024 * 1024:
        raise RuntimeError("The medical ID image is too large for AI verification. Use a clear image under 18 MB.")

    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii")
    prompt = f"""
Perform STRICT document screening for a new doctor signup.
Applicant-provided full name: {full_name}
Applicant-provided medical registration number: {registration_number}
Uploaded filename: {card_name}

The image must visibly be a professional medical/doctor identity or registration credential.
Do NOT assume authenticity merely because it looks professional. Do not invent unreadable text.
Extract only text and visual facts that are actually visible.

Return JSON only with exactly:
{{
  "is_medical_professional_card": true/false,
  "document_type": "string",
  "name_on_card": "string",
  "registration_number_on_card": "string",
  "medical_id_number_on_card": "string",
  "issuing_body": "string",
  "valid_until": "string",
  "text_readable": true/false,
  "obvious_tampering_or_editing": true/false,
  "confidence": 0,
  "decision": "APPROVE" or "REJECT",
  "reason": "short factual reason"
}}

STRICT APPROVAL RULES:
- APPROVE only when the document is clearly a medical professional credential, text is readable,
  the applicant name matches the card name, the applicant registration number matches the card
  registration number, there is no obvious tampering/editing, and confidence is at least 90.
- If any required item is missing, unclear, mismatched, suspicious, or unreadable, decision MUST be REJECT.
- Never approve based on appearance alone.
- An expired credential MUST be REJECTED when an expiry date is clearly visible and has passed.
""".strip()

    client = get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role":"system", "content":"You are a strict credential-document screening AI. Never guess or invent document text."},
            {"role":"user", "content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":data_url}}]}
        ],
        temperature=0.0,
        max_completion_tokens=700,
        reasoning_effort="none",
        reasoning_format="hidden",
        response_format={"type":"json_object"},
        stream=False
    )
    result = _clean_json_object(response.choices[0].message.content)

    supplied_reg = _normalize_credential_text(registration_number)
    card_reg = _normalize_credential_text(result.get("registration_number_on_card"))
    name_score = _name_similarity(full_name, result.get("name_on_card"))
    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    strict_ok = all([
        bool(result.get("is_medical_professional_card")),
        bool(result.get("text_readable")),
        not bool(result.get("obvious_tampering_or_editing")),
        supplied_reg and card_reg and supplied_reg == card_reg,
        name_score >= 0.90,
        confidence >= 90,
        str(result.get("decision", "")).upper() == "APPROVE",
    ])

    if not strict_ok:
        result["decision"] = "REJECT"
        failures = []
        if not result.get("is_medical_professional_card"): failures.append("document is not clearly a medical professional credential")
        if not result.get("text_readable"): failures.append("credential text is not sufficiently readable")
        if bool(result.get("obvious_tampering_or_editing")): failures.append("possible editing/tampering detected")
        if not (supplied_reg and card_reg and supplied_reg == card_reg): failures.append("registration number mismatch or not readable")
        if name_score < 0.90: failures.append("applicant name does not sufficiently match the card")
        if confidence < 90: failures.append("AI confidence is below the strict approval threshold")
        result["reason"] = "; ".join(failures) or "Strict AI verification requirements were not met."

    result["name_match_score"] = round(name_score, 3)
    result["registration_match"] = bool(supplied_reg and card_reg and supplied_reg == card_reg)
    return result


@app.post("/api/auth/doctor-signup")
def doctor_signup():
    if "medical_id_card" not in request.files:
        return jsonify(detail="A medical ID/registration card upload is required for AI verification."), 400
    full_name = str(request.form.get("full_name", "")).strip()
    email = str(request.form.get("email", "")).strip().lower()
    password = str(request.form.get("password", ""))
    registration_number = str(request.form.get("registration_number", "")).strip()
    specialization = str(request.form.get("specialization", "General Medicine")).strip() or "General Medicine"
    hospital_affiliation = str(request.form.get("hospital_affiliation", "Independent / Not specified")).strip() or "Independent / Not specified"
    card = request.files.get("medical_id_card")

    if not full_name or not email or not password or not registration_number or not card or not card.filename:
        return jsonify(detail="Name, email, password, medical registration number, and medical ID card are required."), 400
    if len(password) < 8:
        return jsonify(detail="Password must be at least 8 characters."), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify(detail="Enter a valid email address."), 400
    ext = Path(secure_filename(card.filename)).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
        return jsonify(detail="Medical ID card must be PNG, JPG, JPEG, WEBP, or PDF."), 400

    conn = db()
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        return jsonify(detail="An account with this email already exists."), 409
    conn.close()

    temp_name = f"doctor_verify_{secrets.token_hex(12)}{ext}"
    temp_path = UPLOAD_DIR / temp_name
    card.save(temp_path)
    try:
        ai_result = _verify_doctor_identity_card(temp_path, card.filename, full_name, registration_number)
    except Exception as exc:
        try: temp_path.unlink(missing_ok=True)
        except Exception: pass
        return jsonify(detail=f"AI verification could not be completed: {exc}"), 503

    if str(ai_result.get("decision")).upper() != "APPROVE":
        try: temp_path.unlink(missing_ok=True)
        except Exception: pass
        return jsonify(detail="Doctor signup rejected by strict AI verification.", verification=ai_result), 422

    stored_name = f"doctor_id_{secrets.token_hex(12)}{ext}"
    stored_path = UPLOAD_DIR / stored_name
    shutil.move(str(temp_path), str(stored_path))
    conn = db()
    try:
        cur = conn.execute(
            """INSERT INTO users(email,password_hash,role,full_name,specialization,hospital_affiliation,medical_registration_number,medical_id_number,doctor_verification_status,doctor_verification_reason)\n               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (email, generate_password_hash(password), "DOCTOR", full_name, specialization, hospital_affiliation,
             registration_number, ai_result.get("medical_id_number_on_card") or None, "APPROVED", ai_result.get("reason", "Strict AI verification approved."))
        )
        user_id = cur.lastrowid
        conn.execute(
            """INSERT INTO doctor_verifications(user_id,registration_number,medical_id_number,card_file_name,card_stored_name,ai_result,status)\n               VALUES (?,?,?,?,?,?,?)""",
            (user_id, registration_number, ai_result.get("medical_id_number_on_card") or None, secure_filename(card.filename), stored_name, json.dumps(ai_result, ensure_ascii=False), "APPROVED")
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        try: stored_path.unlink(missing_ok=True)
        except Exception: pass
        return jsonify(detail="An account with this email already exists."), 409
    finally:
        conn.close()

    return jsonify(message="Doctor account created after strict AI verification.", verified=True, verification=ai_result), 201


@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    role = str(data.get("role", "PATIENT")).upper()
    full_name = str(data.get("full_name", "")).strip()

    if role != "PATIENT":
        return jsonify(detail="Doctor accounts are created by an administrator and cannot be self-registered."), 403
    if not full_name or not email or not password:
        return jsonify(detail="Full name, email and password are required."), 400
    if len(password) < 8:
        return jsonify(detail="Password must be at least 8 characters."), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify(detail="Enter a valid email address."), 400

    conn = db()
    try:
        cur = conn.execute(
            """INSERT INTO users
            (email,password_hash,role,full_name,phone,date_of_birth,blood_group)
            VALUES (?,?,?,?,?,?,?)""",
            (
                email,
                generate_password_hash(password),
                "PATIENT",
                full_name,
                data.get("phone"),
                data.get("date_of_birth"),
                data.get("blood_group") or "Unknown",
            ),
        )
        user_id = cur.lastrowid
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO auth_tokens(user_id,token_hash) VALUES (?,?)", (user_id, hash_token(token)))
        conn.commit()
        return jsonify(token=token, role="PATIENT", name=full_name, full_name=full_name), 201
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify(detail="An account with this email already exists. Please log in."), 409
    finally:
        conn.close()


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        conn.close()
        return jsonify(detail="Invalid email or password."), 401
    if user["role"] == "DOCTOR" and str(user["doctor_verification_status"] or "").upper() != "APPROVED":
        conn.close()
        return jsonify(detail="Doctor account is not approved for login. Strict AI verification is required."), 403
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO auth_tokens(user_id,token_hash) VALUES (?,?)", (user["id"], hash_token(token)))
    conn.commit()
    conn.close()
    return jsonify(token=token, role=user["role"], full_name=user["full_name"], name=user["full_name"])


@app.post("/api/auth/recover")
def recover():
    # Do not reveal whether an email exists.
    return jsonify(message="If an account exists, recovery instructions can be sent by your configured recovery service.")


@app.get("/api/patient/dashboard")
@require_auth("PATIENT")
def patient_dashboard(user):
    conn = db()
    verified = conn.execute("SELECT COUNT(*) c FROM medical_records WHERE patient_id=? AND is_verified=1", (user["id"],)).fetchone()["c"]
    appt = conn.execute(
        """SELECT a.*, u.full_name doctor_name, u.specialization, u.hospital_affiliation
           FROM appointments a JOIN users u ON u.id=a.doctor_id
           WHERE a.patient_id=? AND a.status IN ('PENDING','ACCEPTED')
           ORDER BY a.appointment_date, a.id LIMIT 1""", (user["id"],)
    ).fetchone()
    conn.close()
    return jsonify(full_name=user["full_name"], verified_count=verified, upcoming_appointment=json_row(appt))


@app.get("/api/patient/history")
@require_auth("PATIENT")
def patient_history(user):
    conn = db()
    rows = conn.execute(
        """SELECT r.*, d.full_name doctor_name, d.specialization
           FROM medical_records r LEFT JOIN users d ON d.id=r.doctor_id
           WHERE r.patient_id=? ORDER BY datetime(r.date_recorded) DESC, r.id DESC""", (user["id"],)
    ).fetchall()
    conn.close()
    return jsonify(records=[dict(r, is_verified=bool(r["is_verified"])) for r in rows])


@app.get("/api/hospitals/list")
def hospitals_list():
    conn = db()
    rows = conn.execute("SELECT DISTINCT hospital_affiliation FROM users WHERE role='DOCTOR' AND hospital_affiliation IS NOT NULL AND TRIM(hospital_affiliation)<>'' ORDER BY hospital_affiliation").fetchall()
    conn.close()
    return jsonify(hospitals=[r[0] for r in rows])


SPECIALTY_KEYWORDS = {
    "Neurosurgeon": ["headache", "migraine", "head pain", "dizziness", "vertigo", "seizure", "faint", "numbness", "weakness", "brain"],
    "Cardiologist": ["chest pain", "heart", "palpitation", "palpitations", "blood pressure", "bp", "shortness of breath"],
    "Urologist": ["urine", "urinary", "kidney stone", "kidney", "burning urination", "bladder", "prostate"],
    "Orthopedic": ["bone", "joint", "knee", "back pain", "neck pain", "shoulder", "fracture", "sprain", "muscle", "leg pain", "arm pain"],
    "Gynecologist": ["period", "menstrual", "pregnancy", "pregnant", "pelvic", "vaginal", "ovary", "uterus"],
    "Dermatology": ["skin", "rash", "itch", "acne", "eczema", "hair loss", "allergy on skin"],
    "ENT": ["ear", "nose", "throat", "sinus", "hearing", "tonsil", "sore throat"],
    "Pulmonology": ["cough", "breathing", "asthma", "lung", "wheezing", "phlegm"],
    "Ophthalmologist": ["eye", "vision", "blurred vision", "red eye", "sight"],
    "Pediatrician": ["child", "baby", "infant", "kid"],
    "Diabetologist": ["diabetes", "sugar", "glucose", "insulin"],
    "General Medicine": ["fever", "cold", "body pain", "vomiting", "diarrhea", "stomach pain", "abdominal pain", "fatigue", "weakness", "pain"]
}


def _recommended_specialties(overview):
    text_parts = []
    if isinstance(overview, dict):
        for key in ("chief_complaint", "current_status", "duration", "ai_summary"):
            text_parts.append(str(overview.get(key) or ""))
        for key in ("symptoms", "associated_findings", "warnings"):
            value = overview.get(key) or []
            text_parts.extend([str(x) for x in value] if isinstance(value, list) else [str(value)])
    else:
        text_parts.append(str(overview or ""))
    text = " ".join(text_parts).lower()
    scores = []
    for specialty, keywords in SPECIALTY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score:
            scores.append((score, specialty))
    scores.sort(reverse=True)
    chosen = [specialty for score, specialty in scores[:4]]
    if "General Medicine" not in chosen:
        chosen.append("General Medicine")
    return chosen


@app.post("/api/doctors/recommend")
@require_auth("PATIENT")
def recommend_doctors(user):
    data = request.get_json(silent=True) or {}
    overview = data.get("overview") or {}
    hospital = str(data.get("hospital") or "").strip()
    specialties = _recommended_specialties(overview)
    conn = db()
    query = "SELECT id,full_name,specialization,hospital_affiliation FROM users WHERE role='DOCTOR'"
    params = []
    if hospital:
        query += " AND hospital_affiliation=?"
        params.append(hospital)
    rows = conn.execute(query + " ORDER BY full_name", params).fetchall()
    conn.close()
    specialty_terms = {
        "Neurosurgeon": ["neuro"],
        "Cardiologist": ["cardio"],
        "Urologist": ["uro"],
        "Orthopedic": ["orthopedic", "orthopaedic"],
        "Gynecologist": ["gynecologist", "gynaecologist"],
        "Dermatology": ["dermatology", "dermatologist", "skin"],
        "ENT": ["ent"],
        "Pulmonology": ["pulmonology", "pulmonologist"],
        "Ophthalmologist": ["ophthalmologist", "ophthalmology", "eye"],
        "Pediatrician": ["pediatric", "paediatric"],
        "Diabetologist": ["diabetologist", "diabetes"],
        "General Medicine": ["general medicine", "general physician", "physician"],
    }
    doctors = []
    for row in rows:
        spec = str(row["specialization"] or "").lower()
        score = 0
        for idx, wanted in enumerate(specialties):
            terms = specialty_terms.get(wanted, [wanted.lower()])
            if any(term in spec for term in terms):
                score += max(1, 10 - idx)
        if score:
            doctors.append({**dict(row), "recommendation_score": score, "recommended": True})
    doctors.sort(key=lambda d: (-d["recommendation_score"], d["full_name"]))
    if not doctors and hospital:
        # If no specialist at the chosen hospital matches, offer General Medicine there.
        for row in rows:
            if "general medicine" in str(row["specialization"] or "").lower() or "general physician" in str(row["specialization"] or "").lower():
                doctors.append({**dict(row), "recommendation_score": 1, "recommended": True})
    return jsonify(specialties=specialties, doctors=doctors)


@app.get("/api/doctors/list")
def doctors_list():
    conn = db()
    rows = conn.execute("SELECT id,full_name,specialization,hospital_affiliation FROM users WHERE role='DOCTOR' ORDER BY full_name").fetchall()
    conn.close()
    return jsonify(doctors=[dict(r) for r in rows])


@app.post("/api/appointments/request")
@require_auth("PATIENT")
def appointment_request(user):
    data = request.get_json(silent=True) or {}
    try:
        doctor_id = int(data.get("doctor_id"))
    except (TypeError, ValueError):
        return jsonify(detail="Valid doctor is required."), 400
    conn = db()
    doctor = conn.execute("SELECT id FROM users WHERE id=? AND role='DOCTOR'", (doctor_id,)).fetchone()
    if not doctor:
        conn.close()
        return jsonify(detail="Doctor not found."), 404
    care_path = str(data.get("care_path", "ALLOPATHIC")).strip().upper()
    if care_path not in {"ALLOPATHIC", "AYURVEDIC"}:
        care_path = "ALLOPATHIC"
    cur = conn.execute(
        """INSERT INTO appointments(patient_id,doctor_id,care_path,appointment_date,time_slot,reason_for_visit,share_summary)
           VALUES (?,?,?,?,?,?,?)""",
        (user["id"], doctor_id, care_path, data.get("appointment_date"), data.get("time_slot"), data.get("reason_for_visit", ""), int(bool(data.get("share_summary", True))))
    )
    conn.commit()
    conn.close()
    return jsonify(id=cur.lastrowid, message="Appointment requested successfully."), 201


@app.get("/api/appointments/my")
@require_auth("PATIENT")
def my_appointments(user):
    conn = db()
    rows = conn.execute(
        """SELECT a.*, u.full_name doctor_name, u.specialization, u.hospital_affiliation
           FROM appointments a JOIN users u ON u.id=a.doctor_id
           WHERE a.patient_id=? ORDER BY a.appointment_date DESC, a.id DESC""", (user["id"],)
    ).fetchall()
    conn.close()
    return jsonify(appointments=[dict(r) for r in rows])


@app.get("/api/doctor/dashboard")
@require_auth("DOCTOR")
def doctor_dashboard(user):
    conn = db()
    pending = conn.execute(
        """SELECT a.*, p.full_name patient_name, p.blood_group
           FROM appointments a JOIN users p ON p.id=a.patient_id
           WHERE a.doctor_id=? AND a.status='PENDING' ORDER BY a.appointment_date,a.id""", (user["id"],)
    ).fetchall()
    today = date.today().isoformat()
    today_rows = conn.execute(
        """SELECT a.*, p.full_name patient_name FROM appointments a JOIN users p ON p.id=a.patient_id
           WHERE a.doctor_id=? AND a.status='ACCEPTED' AND a.appointment_date=? ORDER BY a.time_slot""", (user["id"], today)
    ).fetchall()
    shared = conn.execute(
        """SELECT p.id patient_id,p.full_name patient_name,p.gender,p.blood_group,p.phone,
                  MAX(a.created_at) granted_at
           FROM appointments a JOIN users p ON p.id=a.patient_id
           WHERE a.doctor_id=? AND a.status='ACCEPTED' AND a.share_summary=1
           GROUP BY p.id ORDER BY granted_at DESC""", (user["id"],)
    ).fetchall()
    updates = conn.execute(
        """SELECT r.patient_id,p.full_name patient_name,r.title,r.date_recorded
           FROM medical_records r JOIN users p ON p.id=r.patient_id
           WHERE r.doctor_id=? AND r.is_verified=1 ORDER BY datetime(r.date_recorded) DESC LIMIT 20""", (user["id"],)
    ).fetchall()
    conn.close()
    return jsonify(
        doctor_name=user["full_name"],
        pending_appointments=[dict(r) for r in pending],
        today_appointments=[dict(r) for r in today_rows],
        shared_patients=[dict(r) for r in shared],
        recent_updates=[dict(r) for r in updates],
    )


def doctor_has_access(doctor_id, patient_id):
    conn = db()
    row = conn.execute(
        """SELECT 1 FROM appointments
           WHERE doctor_id=? AND patient_id=? AND status='ACCEPTED' AND share_summary=1 LIMIT 1""",
        (doctor_id, patient_id),
    ).fetchone()
    conn.close()
    return bool(row)


@app.get("/api/doctor/patient/<int:patient_id>")
@require_auth("DOCTOR")
def doctor_patient_chart(user, patient_id):
    if not doctor_has_access(user["id"], patient_id):
        return jsonify(detail="Patient history is available only after the patient shares access through an accepted appointment."), 403
    conn = db()
    patient = conn.execute(
        "SELECT id,full_name,phone,date_of_birth,blood_group,gender,emergency_contact FROM users WHERE id=? AND role='PATIENT'",
        (patient_id,),
    ).fetchone()
    if not patient:
        conn.close()
        return jsonify(detail="Patient not found."), 404
    records = conn.execute(
        """SELECT r.*, d.full_name doctor_name,d.specialization
           FROM medical_records r LEFT JOIN users d ON d.id=r.doctor_id
           WHERE r.patient_id=? ORDER BY datetime(r.date_recorded) DESC,r.id DESC""", (patient_id,)
    ).fetchall()
    docs = conn.execute("SELECT * FROM documents WHERE patient_id=? ORDER BY datetime(uploaded_at) DESC", (patient_id,)).fetchall()
    summary = conn.execute("SELECT structured_data FROM ai_summaries WHERE patient_id=? ORDER BY id DESC LIMIT 1", (patient_id,)).fetchone()
    conn.close()
    doc_dicts = [dict(d, extracted_data=json.loads(d["extracted_data"] or "{}")) for d in docs]
    document_ai_summary = summarize_documents_for_doctor(doc_dicts)
    return jsonify(
        patient=dict(patient),
        medical_records=[dict(r, is_verified=bool(r["is_verified"])) for r in records],
        documents=doc_dicts,
        document_ai_summary=document_ai_summary,
        latest_ai_summary={"structured_data": json.loads(summary["structured_data"])} if summary else None,
    )


@app.post("/api/doctor/patient/<int:patient_id>/verify-record")
@require_auth("DOCTOR")
def verify_record(user, patient_id):
    if not doctor_has_access(user["id"], patient_id):
        return jsonify(detail="Patient has not granted shared access to this doctor."), 403
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not title or not content:
        return jsonify(detail="Record title and clinical notes are required."), 400
    conn = db()
    cur = conn.execute(
        """INSERT INTO medical_records
        (patient_id,doctor_id,record_type,title,content,source,is_verified,diagnosis_condition,diagnosis_severity,medication_name,medication_dosage,medication_instructions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            patient_id,user["id"],data.get("record_type","CONSULTATION_NOTE"),title,content,"DOCTOR_VERIFIED",1,
            data.get("diagnosis_condition"),data.get("diagnosis_severity"),data.get("medication_name"),data.get("medication_dosage"),data.get("medication_instructions")
        )
    )
    conn.commit()
    conn.close()
    return jsonify(id=cur.lastrowid, message="Verified clinical record saved.")


@app.post("/api/doctor/appointment/<int:appt_id>/status")
@require_auth("DOCTOR")
def appointment_status(user, appt_id):
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).upper()
    if status not in ("ACCEPTED", "REJECTED"):
        return jsonify(detail="Status must be ACCEPTED or REJECTED."), 400
    conn = db()
    cur = conn.execute("UPDATE appointments SET status=?,doctor_notes=? WHERE id=? AND doctor_id=?", (status, data.get("doctor_notes"), appt_id, user["id"]))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify(detail="Appointment not found."), 404
    return jsonify(message=f"Appointment {status.lower()}.")


@app.post("/api/chat/clear")
@require_auth("PATIENT")
def clear_chat(user):
    """Delete the private patient/AI intake conversation after an appointment is booked.
    The generated medical summary and medical record are intentionally preserved.
    """
    conn = db()
    convo_rows = conn.execute("SELECT id FROM conversations WHERE patient_id=?", (user["id"],)).fetchall()
    for row in convo_rows:
        conn.execute("DELETE FROM chat_messages WHERE conversation_id=?", (row["id"],))
    conn.execute("DELETE FROM conversations WHERE patient_id=?", (user["id"],))
    conn.commit()
    conn.close()
    return jsonify(message="Private intake chat cleared. Medical summaries and records were preserved.")


@app.get("/api/chat/history")
@require_auth("PATIENT")
def chat_history(user):
    conn = db()
    convo = conn.execute("SELECT * FROM conversations WHERE patient_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not convo:
        cur = conn.execute("INSERT INTO conversations(patient_id,care_path) VALUES (?,?)", (user["id"], "ALLOPATHIC"))
        convo_id = cur.lastrowid
        conn.commit()
        messages = []
    else:
        convo_id = convo["id"]
        messages = [dict(r, extra_metadata=json.loads(r["extra_metadata"] or "{}")) for r in conn.execute("SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY id", (convo_id,)).fetchall()]
    conn.close()
    return jsonify(conversation_id=convo_id, care_path=(convo["care_path"] if convo else "ALLOPATHIC"), messages=messages)


@app.post("/api/chat/preference")
@require_auth("PATIENT")
def chat_preference(user):
    data = request.get_json(silent=True) or {}
    care_path = str(data.get("care_path", "")).strip().upper()
    if care_path not in {"ALLOPATHIC", "AYURVEDIC"}:
        return jsonify(detail="Choose Allopathic or Ayurvedic care."), 400
    conn = db()
    convo = conn.execute("SELECT * FROM conversations WHERE patient_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if convo:
        count = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (convo["id"],)).fetchone()[0]
        if count > 0:
            conn.close()
            return jsonify(detail="This chat has already started. Start a new intake to change the care preference."), 409
        conn.execute("UPDATE conversations SET care_path=? WHERE id=?", (care_path, convo["id"]))
        convo_id = convo["id"]
    else:
        cur = conn.execute("INSERT INTO conversations(patient_id,care_path) VALUES (?,?)", (user["id"], care_path))
        convo_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify(conversation_id=convo_id, care_path=care_path)


@app.post("/api/chat/message")
@require_auth("PATIENT")
def chat_message(user):
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text:
        return jsonify(detail="Message cannot be empty."), 400

    conn = db()
    convo_id = data.get("conversation_id")
    if not convo_id:
        requested_care = str(data.get("care_path", "ALLOPATHIC")).strip().upper()
        if requested_care not in {"ALLOPATHIC", "AYURVEDIC"}: requested_care = "ALLOPATHIC"
        cur = conn.execute("INSERT INTO conversations(patient_id,care_path) VALUES (?,?)", (user["id"], requested_care))
        convo_id = cur.lastrowid
    owned = conn.execute("SELECT id FROM conversations WHERE id=? AND patient_id=?", (convo_id, user["id"])).fetchone()
    if not owned:
        conn.close()
        return jsonify(detail="Conversation not found."), 404

    conn.execute(
        "INSERT INTO chat_messages(conversation_id,sender,message_text,extra_metadata) VALUES (?,?,?,?)",
        (convo_id, "PATIENT", text, json.dumps({"is_emergency": False}))
    )
    history_rows = conn.execute(
        "SELECT sender,message_text FROM chat_messages WHERE conversation_id=? ORDER BY id", (convo_id,)
    ).fetchall()
    conn.commit()
    conn.close()

    conn2 = db()
    convo_row = conn2.execute("SELECT care_path FROM conversations WHERE id=?", (convo_id,)).fetchone()
    conn2.close()
    care_path = (convo_row["care_path"] if convo_row else "ALLOPATHIC") or "ALLOPATHIC"
    care_label = "Ayurvedic" if care_path == "AYURVEDIC" else "Allopathic"
    messages = [{"role": "system", "content": MY_BOT_SYSTEM_PROMPT},
                {"role": "system", "content": f"The patient selected {care_label} care for this appointment. Respect this preference when collecting context, but do not diagnose, prescribe, or recommend treatment. If Ayurvedic care was selected, you may collect relevant Ayurvedic/traditional-medicine history; if Allopathic care was selected, keep the intake focused on standard clinical history unless Ayurvedic use is directly relevant."},
                {"role": "system", "content": (lambda lang: f"The patient's chosen language code is {lang}. You MUST reply entirely in that language. {LANGUAGE_STYLE_PROMPT.get(lang, 'Use natural conversational language in the selected language.')} Do not switch to English unless the patient explicitly asks for English.")(str(data.get('language', 'en')).strip() or 'en')}]
    for row in history_rows:
        messages.append({
            "role": "user" if row["sender"] == "PATIENT" else "assistant",
            "content": row["message_text"]
        })

    try:
        reply, action = groq_chat(messages)
    except Exception as exc:
        return jsonify(detail=f"AI assistant error: {exc}"), 502

    is_em = emergency_flag(text)
    conn = db()
    conn.execute(
        "UPDATE chat_messages SET extra_metadata=? WHERE id=(SELECT id FROM chat_messages WHERE conversation_id=? AND sender='PATIENT' ORDER BY id DESC LIMIT 1)",
        (json.dumps({"is_emergency": False}), convo_id)
    )
    conn.execute(
        "INSERT INTO chat_messages(conversation_id,sender,message_text,extra_metadata) VALUES (?,?,?,?)",
        (convo_id, "AI", reply, json.dumps({"is_emergency": is_em, "action": action}))
    )
    conn.commit()
    conn.close()
    return jsonify(conversation_id=convo_id, reply=reply, is_emergency=is_em, action=action)


@app.post("/api/chat/edit")
@require_auth("PATIENT")
def edit_chat_message(user):
    data = request.get_json(silent=True) or {}
    try:
        message_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        return jsonify(detail="Invalid message id."), 400
    text = str(data.get("message", "")).strip()
    language = str(data.get("language", "en")).strip() or "en"
    if not text:
        return jsonify(detail="Message cannot be empty."), 400

    conn = db()
    row = conn.execute(
        "SELECT cm.id, cm.conversation_id FROM chat_messages cm JOIN conversations c ON c.id=cm.conversation_id WHERE cm.id=? AND c.patient_id=? AND cm.sender='PATIENT'",
        (message_id, user["id"])
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(detail="Patient message not found."), 404

    convo_id = row["conversation_id"]
    # Editing an earlier answer invalidates the bot's later answers. Remove this
    # message and everything after it, then rebuild the conversation from the edited answer.
    conn.execute("DELETE FROM chat_messages WHERE conversation_id=? AND id>=?", (convo_id, message_id))
    conn.execute(
        "INSERT INTO chat_messages(conversation_id,sender,message_text,extra_metadata) VALUES (?,?,?,?)",
        (convo_id, "PATIENT", text, json.dumps({"is_emergency": emergency_flag(text), "edited": True, "language": language}))
    )
    history_rows = conn.execute(
        "SELECT sender,message_text FROM chat_messages WHERE conversation_id=? ORDER BY id", (convo_id,)
    ).fetchall()
    conn.commit()
    conn.close()

    convo_meta = db()
    convo_care = convo_meta.execute("SELECT care_path FROM conversations WHERE id=?", (convo_id,)).fetchone()
    convo_meta.close()
    care_label = "Ayurvedic" if ((convo_care["care_path"] if convo_care else "ALLOPATHIC") == "AYURVEDIC") else "Allopathic"
    messages = [{"role": "system", "content": MY_BOT_SYSTEM_PROMPT},
                {"role": "system", "content": f"The patient selected {care_label} care for this appointment. Respect this preference when collecting context; do not diagnose, prescribe, or recommend treatment."},
                {"role": "system", "content": f"The patient's chosen language code is {language}. You MUST reply entirely in that language. {LANGUAGE_STYLE_PROMPT.get(language, 'Use natural conversational language in the selected language.')} Do not switch to English unless the patient explicitly asks for English."}]
    for r in history_rows:
        messages.append({"role": "user" if r["sender"] == "PATIENT" else "assistant", "content": r["message_text"]})
    try:
        reply, action = groq_chat(messages)
    except Exception as exc:
        return jsonify(detail=f"AI assistant error: {exc}"), 502

    conn = db()
    conn.execute(
        "INSERT INTO chat_messages(conversation_id,sender,message_text,extra_metadata) VALUES (?,?,?,?)",
        (convo_id, "AI", reply, json.dumps({"is_emergency": emergency_flag(text), "action": action, "language": language}))
    )
    conn.commit()
    conn.close()
    return jsonify(conversation_id=convo_id, reply=reply, is_emergency=emergency_flag(text), action=action)


@app.post("/api/chat/delete-message")
@require_auth("PATIENT")
def delete_chat_message(user):
    data = request.get_json(silent=True) or {}
    try:
        message_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        return jsonify(detail="Invalid message id."), 400

    conn = db()
    row = conn.execute(
        "SELECT cm.id, cm.conversation_id FROM chat_messages cm JOIN conversations c ON c.id=cm.conversation_id WHERE cm.id=? AND c.patient_id=? AND cm.sender='PATIENT'",
        (message_id, user["id"])
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(detail="Patient message not found."), 404
    # Keep the remaining dialogue logically consistent by removing the selected
    # answer and all bot responses that depended on it.
    conn.execute("DELETE FROM chat_messages WHERE conversation_id=? AND id>=?", (row["conversation_id"], message_id))
    conn.commit()
    conn.close()
    return jsonify(message="Message and dependent later chat messages deleted.")


@app.post("/api/chat/generate-overview")
@require_auth("PATIENT")
def generate_overview(user):
    data = request.get_json(silent=True) or {}
    convo_id = data.get("conversation_id")
    conn = db()
    if not convo_id:
        row = conn.execute("SELECT id FROM conversations WHERE patient_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        convo_id = row["id"] if row else None
    messages = conn.execute("SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY id", (convo_id,)).fetchall() if convo_id else []
    summary = build_simple_summary(messages)
    convo_row = conn.execute("SELECT care_path FROM conversations WHERE id=?", (convo_id,)).fetchone() if convo_id else None
    summary["care_preference"] = "Ayurvedic" if convo_row and convo_row["care_path"] == "AYURVEDIC" else ("Allopathic" if convo_row and convo_row["care_path"] == "ALLOPATHIC" else "Not reported")
    conn.execute("INSERT INTO ai_summaries(patient_id,conversation_id,structured_data) VALUES (?,?,?)", (user["id"],convo_id,json.dumps(summary)))
    # Keep the intake visible in the medical timeline as AI-generated information.
    conn.execute(
        "INSERT INTO medical_records(patient_id,record_type,title,content,source,is_verified) VALUES (?,?,?,?,?,0)",
        (user["id"],"AI_SUMMARY","AI Structured Intake",summary["ai_summary"],"AI_GENERATED")
    )
    conn.commit()
    conn.close()
    return jsonify(conversation_id=convo_id, structured_overview=summary)


@app.get("/api/documents/list")
@require_auth("PATIENT")
def documents_list(user):
    conn = db()
    rows = conn.execute("SELECT * FROM documents WHERE patient_id=? ORDER BY datetime(uploaded_at) DESC", (user["id"],)).fetchall()
    conn.close()
    return jsonify(documents=[dict(r, extracted_data=json.loads(r["extracted_data"] or "{}")) for r in rows])


@app.post("/api/documents/upload")
@require_auth("PATIENT")
def documents_upload(user):
    file = request.files.get("file")
    category = request.form.get("category", "OTHER")
    if not file or not file.filename:
        return jsonify(detail="No file uploaded."), 400
    original = secure_filename(file.filename)
    ext = Path(original).suffix.lower()
    if ext not in {".pdf", ".png", ".jpg", ".jpeg", ".txt"}:
        return jsonify(detail="Unsupported file type. Use PDF, PNG, JPG, JPEG, or TXT."), 400
    stored = f"{secrets.token_hex(12)}{ext}"
    stored_path = UPLOAD_DIR / stored
    file.save(stored_path)

    try:
        extracted = analyze_uploaded_document(stored_path, original)
    except Exception as exc:
        # Do not pretend an unreadable document was analyzed. Keep the uploaded file so the patient/doctor can still review it.
        extracted = {
            "document_type": "Medical document",
            "test_or_report_name": original,
            "doctor_or_hospital_name": "Not extracted",
            "report_date": "Not extracted",
            "patient_name": "Not extracted",
            "test_values_or_findings": [],
            "abnormal_findings": [],
            "diagnoses_or_impressions": [],
            "medications": [],
            "important_points": [],
            "summary": "The document was uploaded, but the AI document reader could not analyze it.",
            "limitations": [str(exc)]
        }
        conn = db()
        conn.execute("INSERT INTO documents(patient_id,file_name,stored_name,category,extracted_data) VALUES (?,?,?,?,?)", (user["id"], original, stored, category, json.dumps(extracted)))
        conn.commit()
        conn.close()
        return jsonify(message="Document uploaded, but AI analysis failed.", extracted_data=extracted), 502

    conn = db()
    conn.execute("INSERT INTO documents(patient_id,file_name,stored_name,category,extracted_data) VALUES (?,?,?,?,?)", (user["id"], original, stored, category, json.dumps(extracted, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return jsonify(message="Document uploaded and analyzed by My Pre-Consultation Bot.", extracted_data=extracted)


if __name__ == "__main__":
    init_db()
    import socket
    lan_ip = "127.0.0.1"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass
    print(f"Medikiosk local: http://127.0.0.1:5000")
    print(f"Medikiosk on this network: http://{lan_ip}:5000")
    print("Devices on the same Wi-Fi/LAN can open the network address above.")
    print(f"Database: {DB_PATH}")
    app.run(host="0.0.0.0", port=5000, debug=False)
else:
    init_db()
