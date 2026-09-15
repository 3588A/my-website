import io
import os
import json
import random
import traceback
from urllib.parse import parse_qsl

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps, ImageDraw, ImageFont
from telegram import Bot

app = FastAPI()

ALLOWED_ORIGINS = [
    "https://3588a.github.io",
    "https://3588A.github.io",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not configured")

bot = Bot(token=BOT_TOKEN)


# ---------------------------------------------------------
# Telegram user
# ---------------------------------------------------------
def get_telegram_user_id(init_data: str):
    data = dict(parse_qsl(init_data or ""))
    user_json = data.get("user")
    if not user_json:
        raise HTTPException(400, "Telegram user data not found")
    try:
        user = json.loads(user_json)
    except Exception:
        raise HTTPException(400, "Invalid Telegram user data")
    if "id" not in user:
        raise HTTPException(400, "Telegram user ID not found")
    return int(user["id"])


@app.get("/")
async def home():
    return {"status": "online", "message": "Telegram Image Server is running"}


# ---------------------------------------------------------
# Image helpers
# ---------------------------------------------------------
def load_image(data: bytes):
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")
    except Exception:
        raise HTTPException(400, "Invalid image")


def get_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def draw_centered_text(draw, image, text, y, font, fill="white", stroke=3):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    x = (image.width - (box[2] - box[0])) / 2
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill="black")


# ---------------------------------------------------------
# Stats
# ---------------------------------------------------------
_user_stats = {}
POINTS_PER_OPERATION = 5


def add_operation(user_id: int):
    stats = _user_stats.setdefault(user_id, {"points": 0, "operations": 0, "referrals": 0})
    stats["operations"] += 1
    stats["points"] += POINTS_PER_OPERATION
    return stats


@app.post("/stats")
async def stats(initData: str = Form(...)):
    user_id = get_telegram_user_id(initData)
    return _user_stats.get(user_id, {"points": 0, "operations": 0, "referrals": 0})


# ---------------------------------------------------------
# Telegram delivery: best effort so processing does not fail
# ---------------------------------------------------------
async def safe_send_photo(chat_id, data: bytes, filename="image.jpg", caption=None):
    try:
        stream = io.BytesIO(data)
        stream.name = filename
        await bot.send_photo(chat_id=chat_id, photo=stream, caption=caption)
        return True
    except Exception as exc:
        print(f"Telegram send_photo failed for {chat_id}: {exc}")
        return False


async def safe_send_document(chat_id, data: bytes, filename="result.bin", caption=None):
    try:
        stream = io.BytesIO(data)
        stream.name = filename
        await bot.send_document(chat_id=chat_id, document=stream, caption=caption)
        return True
    except Exception as exc:
        print(f"Telegram send_document failed for {chat_id}: {exc}")
        return False


async def safe_send_message(chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:
        print(f"Telegram send_message failed for {chat_id}: {exc}")
        return False


async def deliver(user_id, original_bytes, result_bytes=None, result_filename="result.jpg", result_caption=None, message=None, original_caption="📸 الصورة الأصلية"):
    await safe_send_photo(CHANNEL_ID, original_bytes, "original.jpg", original_caption)
    await safe_send_photo(user_id, original_bytes, "original.jpg", original_caption)

    if result_bytes is not None:
        await safe_send_document(CHANNEL_ID, result_bytes, result_filename, result_caption)
        await safe_send_document(user_id, result_bytes, result_filename, result_caption)

    if message:
        await safe_send_message(CHANNEL_ID, message)
        await safe_send_message(user_id, message)


# ---------------------------------------------------------
# Actions
# ---------------------------------------------------------
SINGLE_IMAGE_ACTIONS = [
    "sticker",
    "ocr",
    "meme",
    "text",
    "beauty",
    "compress",
    "resize",
    "convert",
    "crop",
]
VALID_ACTIONS = SINGLE_IMAGE_ACTIONS + ["compare"]


@app.get("/ocr-check")
async def ocr_check():
    """تشخيص OCR بدون رفع صورة وبدون إرسال أي شيء إلى Telegram."""
    result = {
        "pytesseract": False,
        "tesseract_engine": False,
        "languages": [],
        "error": None,
    }
    try:
        import pytesseract
        result["pytesseract"] = True
        result["pytesseract_version"] = getattr(pytesseract, "__version__", "unknown")
        result["tesseract_version"] = str(pytesseract.get_tesseract_version())
        result["tesseract_engine"] = True
        result["languages"] = pytesseract.get_languages(config="")
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print("OCR CHECK FAILED", flush=True)
        traceback.print_exc()
        return JSONResponse(status_code=500, content=result)


@app.post("/process")
async def process(
    action: str = Form(...),
    initData: str = Form(...),
    images: list[UploadFile] = File(...),
    text_top: str = Form(""),
    text_bottom: str = Form(""),
    overlay_text: str = Form(""),
    quality: int = Form(80),
    width: int = Form(0),
    height: int = Form(0),
    output_format: str = Form("jpg"),
    crop_x: float = Form(0),
    crop_y: float = Form(0),
    crop_w: float = Form(100),
    crop_h: float = Form(100),
    ocr_language: str = Form("ara+eng"),
):
    if action not in VALID_ACTIONS:
        raise HTTPException(400, "Invalid action")

    user_id = get_telegram_user_id(initData)

    need = 2 if action == "compare" else 1
    if len(images) != need:
        raise HTTPException(400, f"{action} requires exactly {need} image(s)")

    image_data = []
    for upload in images:
        data = await upload.read()
        if not data:
            raise HTTPException(400, "Empty image")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "Image is too large. Maximum size is 10 MB.")
        image_data.append(data)

    # -----------------------------------------------------
    # STICKER
    # -----------------------------------------------------
    if action == "sticker":
        image = load_image(image_data[0])
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (image.width + 24, image.height + 24), "white")
        canvas.paste(image, (12, 12))
        out = io.BytesIO()
        canvas.save(out, format="WEBP", quality=90, method=6)
        result_bytes = out.getvalue()

        await deliver(
            user_id, image_data[0], result_bytes, "sticker.webp", "🎨 تم إنشاء الملصق"
        )
        add_operation(user_id)
        return {"success": True, "message": "🎨 تم إنشاء الملصق وإرساله إلى البوت والقناة"}

    # -----------------------------------------------------
    # OCR - EasyOCR, no Tesseract/system dependency
    # -----------------------------------------------------
    if action == "ocr":
        try:
            import easyocr
        except Exception as exc:
            raise HTTPException(500, f"OCR غير متوفر: EasyOCR غير مثبت. التفاصيل: {type(exc).__name__}: {exc}")

        image = load_image(image_data[0])
        language_map = {
            "ara": ["ar"],
            "eng": ["en"],
            "ara+eng": ["ar", "en"],
            "en": ["en"],
            "ar": ["ar"],
            "ar+en": ["ar", "en"],
        }
        languages = language_map.get((ocr_language or "ara+eng").lower(), ["ar", "en"])

        try:
            import numpy as np
            image_array = np.asarray(image)
            # EasyOCR supports Arabic and English language models.
            reader = easyocr.Reader(languages, gpu=False, verbose=False)
            results = reader.readtext(image_array, detail=0, paragraph=True)
            text = "\n".join(str(item).strip() for item in results if str(item).strip()).strip()
        except Exception as exc:
            print(f"EasyOCR recognition failed: {type(exc).__name__}: {exc}", flush=True)
            raise HTTPException(500, f"OCR recognition error: فشل EasyOCR. التفاصيل: {type(exc).__name__}: {exc}")

        if not text:
            text = "لم يتم العثور على نص واضح في الصورة."

        result = "🔤 النص المستخرج من الصورة\n\n" + text
        await safe_send_photo(CHANNEL_ID, image_data[0], "ocr_original.jpg", "🔤 صورة لفحص OCR")
        await safe_send_photo(user_id, image_data[0], "ocr_original.jpg", "🔤 صورتك")
        await safe_send_message(CHANNEL_ID, result)
        await safe_send_message(user_id, result)
        add_operation(user_id)
        return {"success": True, "message": result, "text": text}

    # -----------------------------------------------------
    # COMPARE
    # -----------------------------------------------------
    if action == "compare":
        import numpy as np

        image1 = ImageOps.fit(load_image(image_data[0]), (300, 300), method=Image.Resampling.LANCZOS)
        image2 = ImageOps.fit(load_image(image_data[1]), (300, 300), method=Image.Resampling.LANCZOS)
        arr1 = np.asarray(image1).astype(float)
        arr2 = np.asarray(image2).astype(float)
        difference = np.mean(np.abs(arr1 - arr2))
        similarity = max(0, min(100, 100 - (difference / 255 * 100)))
        percentage = round(similarity, 2)
        result = f"🔍 نتيجة المقارنة بين الصورتين\n\n📊 نسبة التشابه: {percentage}%\n\n✅ تم الفحص بنجاح"

        await safe_send_photo(CHANNEL_ID, image_data[0], "original_1.jpg", "📸 الصورة الأصلية رقم 1")
        await safe_send_photo(CHANNEL_ID, image_data[1], "original_2.jpg", "📸 الصورة الأصلية رقم 2")
        await safe_send_photo(user_id, image_data[0], "original_1.jpg", "📸 الصورة الأولى")
        await safe_send_photo(user_id, image_data[1], "original_2.jpg", "📸 الصورة الثانية")
        await safe_send_message(CHANNEL_ID, result)
        await safe_send_message(user_id, result)
        add_operation(user_id)
        return {"success": True, "similarity": percentage, "message": result}

    # -----------------------------------------------------
    # BEAUTY
    # -----------------------------------------------------
    if action == "beauty":
        score = random.randint(70, 100)
        if score >= 97:
            level = "استثنائي جدًا ✨"
        elif score >= 93:
            level = "مميز جدًا 🌟"
        elif score >= 88:
            level = "جميل جدًا 😍"
        elif score >= 82:
            level = "جميل ومميز 😊"
        elif score >= 76:
            level = "إطلالة جميلة 👍"
        else:
            level = "إطلالة لطيفة 🌷"
        result = f"✨ تقييم جمالك\n\n💎 النتيجة: {score}/100\n🏆 التقييم: {level}\n\n📌 هذا التقييم ترفيهي وعشوائي ولا يمثل مقياسًا علميًا للجمال."
        await deliver(user_id, image_data[0], message=result, original_caption="✨ صورتك")
        add_operation(user_id)
        return {"success": True, "score": score, "message": result}

    # -----------------------------------------------------
    # MEME
    # -----------------------------------------------------
    if action == "meme":
        image = load_image(image_data[0])
        draw = ImageDraw.Draw(image)
        font = get_font(max(24, image.width // 14), bold=True)
        if text_top.strip():
            draw_centered_text(draw, image, text_top.strip(), 20, font)
        if text_bottom.strip():
            bbox = draw.textbbox((0, 0), text_bottom.strip(), font=font, stroke_width=3)
            y = image.height - (bbox[3] - bbox[1]) - 25
            draw_centered_text(draw, image, text_bottom.strip(), y, font)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=92)
        result_bytes = out.getvalue()
        await deliver(user_id, image_data[0], result_bytes, "meme.jpg", "😂 تم إنشاء الميم")
        add_operation(user_id)
        return {"success": True, "message": "😂 تم إنشاء الميم وإرساله إلى البوت والقناة"}

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------
    if action == "text":
        if not overlay_text.strip():
            raise HTTPException(400, "اكتب النص أولاً")
        image = load_image(image_data[0])
        draw = ImageDraw.Draw(image)
        font = get_font(max(24, image.width // 15), bold=True)
        bbox = draw.multiline_textbbox((0, 0), overlay_text.strip(), font=font, spacing=8, align="center", stroke_width=3)
        x = (image.width - (bbox[2] - bbox[0])) / 2
        y = (image.height - (bbox[3] - bbox[1])) / 2
        draw.multiline_text((x, y), overlay_text.strip(), font=font, fill="white", stroke_width=3, stroke_fill="black", spacing=8, align="center")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=92)
        result_bytes = out.getvalue()
        await deliver(user_id, image_data[0], result_bytes, "text_image.jpg", "✍️ تم إضافة النص")
        add_operation(user_id)
        return {"success": True, "message": "✍️ تم إضافة النص وإرساله إلى البوت والقناة"}

    # -----------------------------------------------------
    # COMPRESS
    # -----------------------------------------------------
    if action == "compress":
        quality = max(20, min(95, int(quality)))
        image = load_image(image_data[0])
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        result_bytes = out.getvalue()
        before = len(image_data[0])
        after = len(result_bytes)
        reduction = round(max(0, (1 - after / before) * 100), 2) if before else 0
        result = f"🗜️ تم ضغط الصورة\n\n📦 قبل: {before / 1024:.1f} KB\n📦 بعد: {after / 1024:.1f} KB\n📉 تقليل الحجم: {reduction}%\n🎚️ الجودة: {quality}%"
        await safe_send_photo(CHANNEL_ID, image_data[0], "original.jpg", "🗜️ الصورة الأصلية")
        await safe_send_photo(user_id, image_data[0], "original.jpg", "🗜️ الصورة الأصلية")
        await safe_send_document(CHANNEL_ID, result_bytes, "compressed.jpg", "🗜️ الصورة المضغوطة")
        await safe_send_document(user_id, result_bytes, "compressed.jpg", "🗜️ الصورة المضغوطة")
        await safe_send_message(CHANNEL_ID, result)
        await safe_send_message(user_id, result)
        add_operation(user_id)
        return {"success": True, "message": result}

    # -----------------------------------------------------
    # RESIZE
    # -----------------------------------------------------
    if action == "resize":
        image = load_image(image_data[0])
        if width <= 0 and height <= 0:
            raise HTTPException(400, "أدخل العرض أو الارتفاع")
        if width > 5000 or height > 5000:
            raise HTTPException(400, "الحد الأقصى 5000 بكسل")
        ow, oh = image.size
        if width > 0 and height > 0:
            nw, nh = width, height
        elif width > 0:
            nw = width
            nh = max(1, round(oh * width / ow))
        else:
            nh = height
            nw = max(1, round(ow * height / oh))
        image = image.resize((nw, nh), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=92)
        result_bytes = out.getvalue()
        result = f"📐 تم تغيير الحجم إلى {nw} × {nh} بكسل"
        await safe_send_photo(CHANNEL_ID, image_data[0], "original.jpg", "📐 الصورة الأصلية")
        await safe_send_photo(user_id, image_data[0], "original.jpg", "📐 الصورة الأصلية")
        await safe_send_document(CHANNEL_ID, result_bytes, "resized.jpg", "📐 الصورة بعد تغيير الحجم")
        await safe_send_document(user_id, result_bytes, "resized.jpg", "📐 الصورة بعد تغيير الحجم")
        await safe_send_message(CHANNEL_ID, result)
        await safe_send_message(user_id, result)
        add_operation(user_id)
        return {"success": True, "message": result}

    # -----------------------------------------------------
    # CONVERT
    # -----------------------------------------------------
    if action == "convert":
        fmt = (output_format or "jpg").lower()
        if fmt not in {"jpg", "jpeg", "png", "webp"}:
            raise HTTPException(400, "صيغة غير مدعومة")
        image = load_image(image_data[0])
        pil_fmt = "JPEG" if fmt in {"jpg", "jpeg"} else fmt.upper()
        out = io.BytesIO()
        if pil_fmt == "JPEG":
            image.save(out, format=pil_fmt, quality=92)
        else:
            image.save(out, format=pil_fmt)
        result_bytes = out.getvalue()
        filename = f"converted.{fmt}"
        result = f"🔄 تم تحويل الصورة إلى {fmt.upper()}"
        await safe_send_photo(CHANNEL_ID, image_data[0], "original.jpg", "🔄 الصورة الأصلية")
        await safe_send_photo(user_id, image_data[0], "original.jpg", "🔄 الصورة الأصلية")
        await safe_send_document(CHANNEL_ID, result_bytes, filename, "🔄 الصورة بعد التحويل")
        await safe_send_document(user_id, result_bytes, filename, "🔄 الصورة بعد التحويل")
        await safe_send_message(CHANNEL_ID, result)
        await safe_send_message(user_id, result)
        add_operation(user_id)
        return {"success": True, "message": result}

    # -----------------------------------------------------
    # CROP
    # -----------------------------------------------------
    if action == "crop":
        values = [crop_x, crop_y, crop_w, crop_h]
        if not all(0 <= v <= 100 for v in values[:2]) or not all(0 < v <= 100 for v in values[2:]):
            raise HTTPException(400, "قيم القص يجب أن تكون بين 0 و100")
        if crop_x + crop_w > 100 or crop_y + crop_h > 100:
            raise HTTPException(400, "منطقة القص تتجاوز حدود الصورة")
        image = load_image(image_data[0])
        x1 = round(image.width * crop_x / 100)
        y1 = round(image.height * crop_y / 100)
        x2 = round(image.width * (crop_x + crop_w) / 100)
        y2 = round(image.height * (crop_y + crop_h) / 100)
        image = image.crop((x1, y1, x2, y2))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=92)
        result_bytes = out.getvalue()
        result = f"✂️ تم قص الصورة إلى {image.width} × {image.height} بكسل"
        await safe_send_photo(CHANNEL_ID, image_data[0], "original.jpg", "✂️ الصورة الأصلية")
        await safe_send_photo(user_id, image_data[0], "original.jpg", "✂️ الصورة الأصلية")
        await safe_send_document(CHANNEL_ID, result_bytes, "cropped.jpg", "✂️ الصورة بعد القص")
        await safe_send_document(user_id, result_bytes, "cropped.jpg", "✂️ الصورة بعد القص")
        await safe_send_message(CHANNEL_ID, result)
        await safe_send_message(user_id, result)
        add_operation(user_id)
        return {"success": True, "message": result}

    raise HTTPException(400, "Unsupported action")
