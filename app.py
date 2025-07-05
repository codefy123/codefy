from flask import Flask, request, send_file, make_response, send_from_directory
from fpdf import FPDF
import os
import fitz
from PIL import Image, ImageDraw
from datetime import datetime
import re
from dotenv import load_dotenv
import google.generativeai as genai
import secrets
import shutil
import tempfile
import logging
from flask_wtf.csrf import CSRFProtect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WriteMyPDF")

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = Flask(__name__, static_folder="templates", static_url_path="")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB
app.config['SECRET_KEY'] = secrets.token_hex(16)

csrf = CSRFProtect(app)

# Create necessary folders
os.makedirs("fonts", exist_ok=True)
os.makedirs("bg", exist_ok=True)

FONT_MAP = {
    "handwriting1": "font1.ttf",
    "handwriting2": "font2.ttf",
    "handwriting3": "font3.ttf",
    "handwriting4": "font4.ttf",
    "handwriting5": "font5.ttf",
}

BG_MAP = {
    "blank": os.path.abspath("bg/blank.png"),
    "lined": os.path.abspath("bg/lined.png"),
}

# ---------- Utilities ----------
def normalize_quotes(text):
    replacements = {
        '‘': '', '’': '', '“': '', '”': '',
        '—': '-', '–': '-', '…': '...',
        '(': '<', ')': '>', '{': '<', '}': '>',
        '[': '<', ']': '>', '=': ':',
        '\\': '', '/': '', '"': '', "'": ''
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

def sanitize_text(text):
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 [](),.:;!?@%&-+=*\n")
    return ''.join(c for c in text if c in allowed or c == '\n')

def clean_response(text):
    text = re.sub(r"[*`]+", "", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()

def extract_text_from_pdf(path):
    text = ""
    with fitz.open(path) as doc:
        for page in doc:
            text += page.get_text()
    return text

def extract_text_from_image(path):
    try:
        import pytesseract
        img = Image.open(path)
        return pytesseract.image_to_string(img)
    except:
        return "Sample OCR Text"

def solve_with_gemini(questions_raw):
    expected_count = len(re.findall(r"\n*\s*\d+\.\s*", questions_raw)) or 5

    if len(questions_raw.strip()) < 40:
        return "1. The uploaded file seems to be empty or unclear. Please upload a valid assignment."

    if len(questions_raw) > 9000:
        questions_raw = questions_raw[:9000]

    prompt = f"""
You're a hardworking student completing a handwritten assignment.
Your task is to answer the following questions as if you're submitting a real assignment.

🧠 How to answer:
- Keep it natural and human-like — no AI tone.
- Use point-wise format for factual questions.
- Write long paragraphs for theory/essay-based answers.
- Never skip any question — try your best even if it's tricky.
- Don’t repeat the question, just write answers.
- Avoid markdown, symbols like *, **, etc.

📝 Output Format:
1. Answer one
2. Answer two
... and so on

Questions:
{questions_raw.strip()}
"""

    model = genai.GenerativeModel("models/gemini-2.0-flash")
    response = model.generate_content(prompt, generation_config={"temperature": 0.7})

    text = clean_response(response.text)
    answers = re.split(r"\n*\s*\d+\.\s*", text)
    answers = [a.strip() for a in answers if a.strip()]

    final_output = ""
    for i, ans in enumerate(answers[:expected_count], 1):
        final_output += f"{i}. {ans}\n\n"
    return final_output.strip()

# ---------- Backgrounds ----------
def create_backgrounds():
    if not os.path.exists(BG_MAP["blank"]):
        Image.new('RGB', (2480, 3508), (255, 255, 255)).save(BG_MAP["blank"])
    if not os.path.exists(BG_MAP["lined"]):
        img = Image.new('RGB', (2480, 3508), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for y in range(200, 3508, 40):
            draw.line([(120, y), (2360, y)], fill=(150, 150, 150), width=3)
        draw.line([(100, 0), (100, 3508)], fill=(220, 220, 220), width=5)
        img.save(BG_MAP["lined"])

create_backgrounds()

# ---------- PDF ----------
class HandwrittenPDF(FPDF):
    def __init__(self, bg_path):
        super().__init__()
        self.bg_path = bg_path
        self.name = ""
        self.roll = ""

    def set_student_info(self, name, roll):
        self.name = name
        self.roll = roll

    def header(self):
        self.image(self.bg_path, x=0, y=0, w=210, h=297)
        self.set_font("CustomFont", size=36)
        self.text(105 - self.get_string_width(self.name)/2, 17, self.name)
        self.set_font("CustomFont", size=32)
        self.text(105 - self.get_string_width(self.roll)/2, 24, self.roll)

    def footer(self):
        pass

# ---------- Routes ----------
@app.route("/")
def serve_index():
    return send_from_directory("templates", "index.html")

@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory("templates", path)

@app.route("/upload", methods=["POST"])
@csrf.exempt
def upload():
    try:
        name = request.form.get("name", "Unknown")
        roll = request.form.get("roll", "Unknown")
        file = request.files.get("file")
        font_key = request.form.get("font", "handwriting1")
        bg_key = request.form.get("background", "blank")
        ink_color = request.form.get("ink", "black")

        if not file:
            return "No file uploaded", 400

        temp_dir = tempfile.mkdtemp()
        upload_path = os.path.join(temp_dir, file.filename)
        file.save(upload_path)

        ext = file.filename.split(".")[-1].lower()
        raw = extract_text_from_image(upload_path) if ext in ["jpg", "jpeg", "png"] else extract_text_from_pdf(upload_path)

        clean_txt = sanitize_text(normalize_quotes(raw))
        solution = solve_with_gemini(clean_txt)

        font_path = os.path.join("fonts", FONT_MAP.get(font_key, "font1.ttf"))
        bg_path = BG_MAP.get(bg_key, BG_MAP["blank"])

        pdf = HandwrittenPDF(bg_path)
        pdf.set_student_info(name, roll)
        pdf.add_font("CustomFont", "", font_path, uni=True)
        pdf.add_page()
        pdf.set_font("CustomFont", size=36)

        if ink_color == "blue":
            pdf.set_text_color(0, 0, 255)
        else:
            pdf.set_text_color(0, 0, 0)

        y, number = 38, 1
        line_height = 11
        margin_x = 30
        number_x = 18.1
        max_width = 180

        answers = re.split(r"\n*\s*\d+\.\s*", solution)
        answers = [a.strip() for a in answers if a.strip()]

        for ans in answers:
            pdf.text(number_x, y, f"{number}.")
            current_line = ""
            for word in ans.split():
                if pdf.get_string_width(current_line + word + " ") < max_width:
                    current_line += word + " "
                else:
                    pdf.text(margin_x, y, current_line.strip())
                    y += line_height
                    current_line = word + " "
                    if y > 285:
                        pdf.add_page()
                        y = 38
            if current_line:
                pdf.text(margin_x, y, current_line.strip())
                y += line_height
            y += 4
            number += 1
            if y > 285:
                pdf.add_page()
                y = 38

        original_filename = os.path.splitext(file.filename)[0]
        safe_name = re.sub(r'[^\w\-_]', '_', original_filename)
        outname = f"{safe_name}_solved.pdf"
        output_path = os.path.join(temp_dir, outname)
        pdf.output(output_path)

        return make_response(send_file(
            output_path,
            as_attachment=True,
            download_name=outname,
            mimetype="application/pdf"
        ))
    except Exception as e:
        logger.exception("Error in /upload")
        return "Error: " + str(e), 500
    finally:
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)
