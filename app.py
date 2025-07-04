from flask import Flask, render_template, request, send_file, make_response
from fpdf import FPDF
import os
import fitz  # PyMuPDF
from PIL import Image, ImageDraw
from datetime import datetime
import re
from dotenv import load_dotenv
import google.generativeai as genai
from flask_wtf.csrf import CSRFProtect, generate_csrf
import secrets
import shutil
import tempfile
import time
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('WriteMate')

# Load API key from .env
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB limit for uploaded files
app.config['SECRET_KEY'] = secrets.token_hex(16)
csrf = CSRFProtect(app)

# Create required directories
os.makedirs("uploads", exist_ok=True)
os.makedirs("fonts", exist_ok=True)
os.makedirs("bg", exist_ok=True)

# Font and background mappings
FONT_MAP = {
    "handwriting1": "font1.ttf",
    "handwriting2": "font2.ttf",
    "handwriting3": "font3.ttf",
    "handwriting4": "font4.ttf",
    "handwriting5": "font5.ttf",
}

# Create proper background images with higher resolution
def create_backgrounds():
    # Blank background
    blank_path = "bg/blank.png"
    if not os.path.exists(blank_path):
        img = Image.new('RGB', (2480, 3508), (255, 255, 255))  # A4 at 300dpi
        img.save(blank_path)
        logger.info(f"Created blank background at: {os.path.abspath(blank_path)}")
    
    # Lined background - create with high contrast lines
    lined_path = "bg/lined.png"
    if not os.path.exists(lined_path):
        # Create a high-resolution lined background
        img = Image.new('RGB', (2480, 3508), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        
        # Draw margin line
        draw.line([(100, 0), (100, 3508)], fill=(220, 220, 220), width=5)
        
        # Draw lines every 40 pixels (simulating notebook lines)
        for y in range(200, 3508, 40):
            draw.line([(120, y), (2360, y)], fill=(150, 150, 150), width=3)
        
        img.save(lined_path)
        logger.info(f"Created lined background at: {os.path.abspath(lined_path)}")
    
    # Verify both backgrounds exist
    if not os.path.exists(blank_path) or not os.path.exists(lined_path):
        logger.error("Background creation failed!")

create_backgrounds()

# Use absolute paths for backgrounds
BG_MAP = {
    "blank": os.path.abspath("bg/blank.png"),
    "lined": os.path.abspath("bg/lined.png"),
}

# Log background paths for debugging
logger.info(f"Blank background path: {BG_MAP['blank']}")
logger.info(f"Lined background path: {BG_MAP['lined']}")
logger.info(f"Blank exists: {os.path.exists(BG_MAP['blank'])}")
logger.info(f"Lined exists: {os.path.exists(BG_MAP['lined'])}")

# ---------- Text Utilities ----------
def normalize_quotes(text):
    replacements = {
        '‘': '', '’': '',
        '“': '', '”': '',
        '—': '-', '–': '-', '…': '...',
        '(': '<', ')': '>',
        '{': '<', '}': '>',
        '[': '<', ']': '>',
        '=': ':',
        '\\': '', '/': '',
        '"': '', "'": ''
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

def sanitize_text(text):
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 [](),.:;!?@%&-+=*\n")
    return ''.join(c for c in text if c in allowed or c == '\n')

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
    except ImportError:
        logger.warning("Pytesseract not installed, using sample text")
        return "Sample question text for development purposes"

def clean_response(text):
    text = re.sub(r"[*`]", "", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()

def solve_with_gemini(questions):
    if not os.getenv("GEMINI_API_KEY"):
        logger.warning("No Gemini API key, using sample solutions")
        return "1. Sample solution for question one.\n2. Sample solution for question two.\n3. Sample solution for question three."
    
    prompt = (
        "You are a teacher solving a student's assignment.\n"
        "Provide only numbered answers (1., 2., ...) without repeating questions.\n"
        "Avoid using *, **, or double line breaks (\\n\\n).\n\n"
        f"{questions}"
    )
    model = genai.GenerativeModel("models/gemini-2.0-flash")
    response = model.generate_content(prompt)
    return clean_response(response.text)

# ---------- PDF Generator (Fixed Version) ----------
class HandwrittenPDF(FPDF):
    def __init__(self, bg_path):
        super().__init__()
        self.bg_path = bg_path
        self.name = ""
        self.roll = ""
        logger.info(f"PDF initialized with background: {bg_path}")

    def set_student_info(self, name, roll):
        self.name = name
        self.roll = roll

    def header(self):
        # Add background to every page
        if self.bg_path and os.path.exists(self.bg_path):
            try:
                # Add background with original aspect ratio
                self.image(self.bg_path, x=0, y=0, w=210, h=297)  # A4 full
                logger.debug(f"Added background: {self.bg_path}")
            except Exception as e:
                logger.error(f"Error adding background: {str(e)}")
                # Try to use blank background as fallback
                blank_path = BG_MAP["blank"]
                if os.path.exists(blank_path):
                    self.image(blank_path, x=0, y=0, w=210, h=297)
                    logger.warning(f"Using fallback background: {blank_path}")
        else:
            logger.warning(f"Background not found: {self.bg_path}")
        
        # Add name/roll to every page
        self.set_font("CustomFont", size=36)
        self.set_text_color(0)
        self.text(105 - self.get_string_width(self.name)/2, 17, self.name)
        self.set_font("CustomFont", size=32)
        self.text(105 - self.get_string_width(self.roll)/2, 24, self.roll)

    def footer(self):
        # Empty footer to prevent default page numbers
        pass

# ---------- Routes ----------
@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf)

@app.route("/")
def index():
    return render_template("index.html")

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

        logger.info(f"Received upload request - Name: {name}, Roll: {roll}, Font: {font_key}, BG: {bg_key}, Ink: {ink_color}")

        if not file or file.filename == '':
            logger.error("No file selected")
            return "No file selected", 400

        # Create temp directory for output
        temp_dir = tempfile.mkdtemp()
        logger.info(f"Created temp directory: {temp_dir}")
        
        # Save uploaded file
        upload_path = os.path.join(temp_dir, file.filename)
        file.save(upload_path)
        logger.info(f"Saved uploaded file to: {upload_path}")

        # Extract and sanitize text
        ext = file.filename.split(".")[-1].lower()
        logger.info(f"Processing {ext} file")
        
        if ext in ["jpg", "jpeg", "png"]:
            raw = extract_text_from_image(upload_path)
        else:
            raw = extract_text_from_pdf(upload_path)
            
        logger.debug(f"Raw extracted text: {raw[:200]}...")
        
        clean_txt = sanitize_text(normalize_quotes(raw))
        solution_raw = solve_with_gemini(clean_txt)
        solution = sanitize_text(normalize_quotes(solution_raw))
        
        logger.info(f"Generated solution with {len(solution)} characters")

        # Get font and background paths
        font_path = os.path.join("fonts", FONT_MAP.get(font_key, "font1.ttf"))
        
        # Handle background selection
        bg_path = BG_MAP.get(bg_key, BG_MAP["blank"])
        logger.info(f"Selected background: {bg_key} -> {bg_path}")
        
        # Verify background exists
        if not os.path.exists(bg_path):
            logger.warning(f"Background not found: {bg_path}, using blank")
            bg_path = BG_MAP["blank"]
            
        logger.info(f"Using background: {bg_path} (exists: {os.path.exists(bg_path)})")

        # Generate PDF
        pdf = HandwrittenPDF(bg_path)
        pdf.set_student_info(name, roll)
        pdf.add_font("CustomFont", "", font_path, uni=True)
        pdf.add_page()
        pdf.set_font("CustomFont", size=36)

        # Set ink color based on user selection
        if ink_color == "blue":
            pdf.set_text_color(0, 0, 255)
            logger.info("Using blue ink")
        else:
            pdf.set_text_color(0, 0, 0)
            logger.info("Using black ink")

        line_height = 11
        max_width = 180
        margin_x = 30
        number_x = 18.1
        y = 38
        number = 1

        # Split solution into answers
        answers = re.split(r"\n*\s*\d+\.\s*", solution)
        answers = [ans.strip() for ans in answers if ans.strip()]
        logger.info(f"Split solution into {len(answers)} answers")

        for ans in answers:
            # Draw the answer number
            pdf.text(number_x, y, f"{number}.")

            # Wrap answer text
            words = ans.split()
            current_line = ""
            for word in words:
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

            # Space between answers
            y += 4
            number += 1

            if y > 285:
                pdf.add_page()
                y = 38

        # Save PDF to temporary file
        outname = f"solution_{name.replace(' ', '_')}_{int(datetime.now().timestamp())}.pdf"
        output_path = os.path.join(temp_dir, outname)
        pdf.output(output_path)
        logger.info(f"PDF generated at: {output_path} (size: {os.path.getsize(output_path)} bytes)")
        
        # Send file and clean up
        response = make_response(send_file(
            output_path,
            as_attachment=True,
            download_name=f"WriteMate_Solution_{name.replace(' ', '_')}.pdf",
            mimetype='application/pdf'
        ))
        
        return response
    except Exception as e:
        logger.exception(f"Error generating solution: {str(e)}")
        return f"Error generating solution: {str(e)}", 500
    finally:
        # Clean up temporary files
        if 'temp_dir' in locals():
            logger.info(f"Cleaning up temp directory: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)

# Error Handlers
@app.errorhandler(413)
def too_large(e):
    return "File is too large (max 5MB)", 413

@app.errorhandler(400)
def bad_request(e):
    return "Invalid request", 400

@app.errorhandler(500)
def server_error(e):
    return "Internal server error", 500

# ---------- Run ----------
if __name__ == "__main__":
    # Verify background images
    print("Verifying background images:")
    print(f"Blank: {BG_MAP['blank']} - Exists: {os.path.exists(BG_MAP['blank'])}")
    print(f"Lined: {BG_MAP['lined']} - Exists: {os.path.exists(BG_MAP['lined'])}")
    
    if not os.path.exists(BG_MAP['lined']):
        print("Recreating lined background...")
        try:
            # Create a high-resolution lined background
            img = Image.new('RGB', (2480, 3508), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            
            # Draw margin line
            draw.line([(100, 0), (100, 3508)], fill=(220, 220, 220), width=5)
            
            # Draw lines every 40 pixels
            for y in range(200, 3508, 40):
                draw.line([(120, y), (2360, y)], fill=(150, 150, 150), width=3)
            
            img.save(BG_MAP['lined'])
            print(f"Recreated lined background at: {BG_MAP['lined']}")
        except Exception as e:
            print(f"Error recreating background: {str(e)}")
    
    app.run(debug=True)
