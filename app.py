from flask import Flask, request, send_file, make_response
from fpdf import FPDF
from flask_cors import CORS
import os
import fitz  # PyMuPDF
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

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WriteMate")

# Load API key
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB
app.config['SECRET_KEY'] = secrets.token_hex(16)
CORS(app, resources={r"/upload": {"origins": "https://handwrittenpdf1.web.app"}})

csrf = CSRFProtect(app)

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

# Ensure backgrounds exist
def create_backgrounds():
    if not os.path.exists(BG_MAP["blank"]):
        img = Image.new('RGB', (2480, 3508), (255, 255, 255))
        img.save(BG_MAP["blank"])
    if not os.path.exists(BG_MAP["lined"]):
        img = Image.new('RGB', (2480, 3508), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for y in range(200, 3508, 40):
            draw.line([(120, y), (2360, y)], fill=(150, 150, 150), width=3)
        draw.line([(100, 0), (100, 3508)], fill=(220, 220, 220), width=5)
        img.save(BG_MAP["lined"])

create_backgrounds()

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

def solve_with_gemini(questions):
    prompt = (
        "You are a teacher solving a student's assignment.\n"
        "Provide only numbered answers (1., 2., ...) without repeating questions.\n"
        "Avoid using *, **, or double line breaks (\\n\\n).\n\n"
        f"{questions}"
    )
    model = genai.GenerativeModel("models/gemini-2.0-flash")
    response = model.generate_content(prompt)
    return clean_response(response.text)

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
        if ext in ["jpg", "jpeg", "png"]:
            raw = extract_text_from_image(upload_path)
        else:
            raw = extract_text_from_pdf(upload_path)

        font_path = os.path.join("fonts", FONT_MAP.get(font_key, "font1.ttf"))
        bg_path = BG_MAP.get(bg_key, BG_MAP["blank"])

        solution = solve_with_gemini(raw)

        pdf = HandwrittenPDF(bg_path)
        pdf.set_student_info(name, roll)
        pdf.add_font("CustomFont", "", font_path, uni=True)
        pdf.add_page()
        pdf.set_font("CustomFont", size=36)

        if ink_color == "blue":
            pdf.set_text_color(0, 0, 255)
        else:
            pdf.set_text_color(0, 0, 0)

        y = 38
        line_height = 11
        number = 1
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

        outname = f"solution_{int(datetime.now().timestamp())}.pdf"
        output_path = os.path.join(temp_dir, outname)
        pdf.output(output_path)

        return make_response(send_file(
            output_path,
            as_attachment=True,
            download_name="Handwritten_Solution.pdf",
            mimetype="application/pdf"
        ))
    except Exception as e:
        logger.exception("Error in /upload")
        return "Error: " + str(e), 500
    finally:
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)

# Health check
@app.route("/")
def hello():
    return "WriteMate backend running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
