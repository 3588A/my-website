import io
import os
import logging
import hmac
import hashlib
import json
from urllib.parse import parse_qsl

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps
from telegram import Bot


logging.basicConfig(level=logging.INFO)

app = FastAPI()


# =========================
# CORS
# =========================

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


# =========================
# Environment Variables
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")


if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not configured")


bot = Bot(token=BOT_TOKEN)


# =========================
# Telegram User ID
# =========================

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


# =========================
# Home
# =========================

@app.get("/")
async def home():

    return {
        "status": "online",
        "message": "Telegram Image Server is running"
    }


# =========================
# Image Loader
# =========================

def load_image(data: bytes):

    try:

        image = Image.open(
            io.BytesIO(data)
        )

        # Fix phone camera rotation
        image = ImageOps.exif_transpose(
            image
        )

        # Convert to RGB
        return image.convert("RGB")

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid image"
        )


# =========================
# Process Images
# =========================

@app.post("/process")
async def process(

    action: str = Form(...),

    initData: str = Form(...),

    images: list[UploadFile] = File(...)

):

    # =========================
    # Validate Action
    # =========================

    if action not in [
        "sticker",
        "compare"
    ]:

        raise HTTPException(
            status_code=400,
            detail="Invalid action"
        )


    # =========================
    # Get Telegram User ID
    # =========================

    user_id = get_telegram_user_id(
        initData
    )


    # =========================
    # Validate Image Count
    # =========================

    if action == "sticker":

        if len(images) != 1:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Sticker requires "
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


    # =========================
    # Read Images
    # =========================

    image_data = []


    for upload in images:

        data = await upload.read()


        if not data:

            raise HTTPException(
                status_code=400,
                detail="Empty image"
            )


        # Maximum 10 MB per image

        if len(data) > 10 * 1024 * 1024:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Image is too large. "
                    "Maximum size is 10 MB."
                )
            )


        image_data.append(data)


    # =====================================================
    # STICKER
    # =====================================================

    if action == "sticker":

        # -------------------------
        # Load image
        # -------------------------

        image = load_image(
            image_data[0]
        )


        # -------------------------
        # Resize
        # -------------------------

        image.thumbnail(
            (512, 512),
            Image.Resampling.LANCZOS
        )


        # -------------------------
        # Create WEBP
        # -------------------------

        sticker_io = io.BytesIO()


        image.save(
            sticker_io,
            format="WEBP",
            quality=90,
            method=6
        )


        sticker_io.seek(0)

        sticker_io.name = (
            "sticker.webp"
        )


        # =================================================
        # CHANNEL - ORIGINAL IMAGE
        # =================================================

        original_io = io.BytesIO(
            image_data[0]
        )

        original_io.name = (
            "original.jpg"
        )


        await bot.send_photo(

            chat_id=CHANNEL_ID,

            photo=original_io,

            caption=(
                "📸 صورة أصلية "
                "مرفوعة عبر التطبيق"
            )

        )


        # =================================================
        # USER - ORIGINAL IMAGE
        # =================================================

        user_original_io = io.BytesIO(
            image_data[0]
        )

        user_original_io.name = (
            "original.jpg"
        )


        await bot.send_photo(

            chat_id=user_id,

            photo=user_original_io,

            caption="📸 صورتك الأصلية"

        )


        # =================================================
        # CHANNEL - STICKER
        # =================================================

        channel_sticker = io.BytesIO(

            sticker_io.getvalue()

        )

        channel_sticker.name = (
            "sticker.webp"
        )


        await bot.send_document(

            chat_id=CHANNEL_ID,

            document=channel_sticker,

            caption="🎨 تم إنشاء الملصق"

        )


        # =================================================
        # USER - STICKER
        # =================================================

        user_sticker = io.BytesIO(

            sticker_io.getvalue()

        )

        user_sticker.name = (
            "sticker.webp"
        )


        await bot.send_document(

            chat_id=user_id,

            document=user_sticker,

            caption="🎨 تم إنشاء الملصق"

        )


        return {

            "success": True,

            "message": (
                "Sticker created successfully"
            )

        }


    # =====================================================
    # COMPARE
    # =====================================================

    if action == "compare":

        # -------------------------
        # Load images
        # -------------------------

        image1 = load_image(
            image_data[0]
        )

        image2 = load_image(
            image_data[1]
        )


        # -------------------------
        # Resize both images
        # -------------------------

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


        # -------------------------
        # Calculate similarity
        # -------------------------

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


        # -------------------------
        # Result
        # -------------------------

        result = (

            "🔍 نتيجة المقارنة "
            "بين الصورتين\n\n"

            f"📊 نسبة التشابه: "
            f"{percentage}%\n"

            "✅ تم الفحص بنجاح"

        )


        # =================================================
        # SEND ORIGINAL IMAGES TO CHANNEL
        # =================================================

        for index, data in enumerate(

            image_data,

            start=1

        ):

            photo = io.BytesIO(
                data
            )


            photo.name = (

                f"original_{index}.jpg"

            )


            await bot.send_photo(

                chat_id=CHANNEL_ID,

                photo=photo,

                caption=(

                    f"📸 الصورة الأصلية "
                    f"رقم {index}"

                )

            )


        # =================================================
        # SEND RESULT TO CHANNEL
        # =================================================

        await bot.send_message(

            chat_id=CHANNEL_ID,

            text=result

        )


        return {

            "success": True,

            "similarity": percentage,

            "message": result

        }
