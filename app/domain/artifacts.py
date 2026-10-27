"""Artifact names, content classification and safe, bounded preview rendering."""
import hashlib
from io import BytesIO
from pathlib import PurePosixPath
import re
import warnings

from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError

IMAGE_TYPES = {'.png': ('PNG', 'image/png'), '.jpg': ('JPEG', 'image/jpeg'),
               '.jpeg': ('JPEG', 'image/jpeg'), '.webp': ('WEBP', 'image/webp')}
ACTIVE_EXTENSIONS = {'.html', '.htm', '.svg', '.xml', '.xhtml', '.js', '.mjs'}
PREVIEW_TEXT_BYTES = 16384


def validate_artifact_name(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 256 or len(name.split('/')) > 5
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', part) for part in name.split('/'))):
        raise AppError('ARTIFACT_UNSAFE_NAME', '产物名称或目录范围无效', 422)
    return name


def image_info(content, expected):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=['PNG', 'JPEG', 'WEBP']) as image:
                if (image.format != expected or image.width * image.height > 4_000_000
                        or max(image.size) > 4096 or getattr(image, 'n_frames', 1) != 1):
                    raise ValueError()
                image.verify()
            with Image.open(BytesIO(content), formats=[expected]) as image:
                image.load()
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AppError('ARTIFACT_INVALID_CONTENT', '图片内容、格式或像素范围无法通过校验', 422) from None


def classify_artifact(name, content):
    validate_artifact_name(name)
    if not isinstance(content, bytes) or len(content) > 2097152:
        raise AppError('ARTIFACT_SIZE_LIMIT', '产物内容超过上限', 413)
    extension = PurePosixPath(name).suffix.lower()
    if extension in IMAGE_TYPES:
        format_name, media_type = IMAGE_TYPES[extension]
        image_info(content, format_name)
        return media_type, 'image'
    if extension in ACTIVE_EXTENSIONS:
        return 'application/octet-stream', 'none'
    try:
        value = content.decode('utf-8')
        if any(ord(char) < 32 and char not in '\n\r\t' or 127 <= ord(char) <= 159 for char in value):
            raise ValueError()
    except (UnicodeError, ValueError):
        return 'application/octet-stream', 'none'
    return 'text/plain', 'text'


def verify_artifact(row):
    media_type, preview_kind = classify_artifact(row.name, row.content)
    if (row.size_bytes != len(row.content) or row.sha256 != hashlib.sha256(row.content).hexdigest()
            or (row.media_type, row.preview_kind) != (media_type, preview_kind)):
        raise AppError('ARTIFACT_CONTENT_CHANGED', '产物内容与保存的回执不一致', 409)


def render_artifact_preview(row):
    verify_artifact(row)
    if row.preview_kind == 'text':
        return row.content[:PREVIEW_TEXT_BYTES].decode('utf-8', errors='replace').encode('utf-8'), 'text/plain; charset=utf-8'
    if row.preview_kind != 'image':
        raise AppError('ARTIFACT_PREVIEW_UNAVAILABLE', '此格式仅支持附件下载', 415)
    with Image.open(BytesIO(row.content)) as image:
        image.thumbnail((1600, 1600))
        # Paste into a fresh image to discard metadata, profiles and trailing data.
        clean = Image.new('RGBA', image.size)
        clean.paste(image.convert('RGBA'))
        output = BytesIO()
        clean.save(output, format='PNG')
    return output.getvalue(), 'image/png'
