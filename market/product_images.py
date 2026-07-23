import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024
CLIENT_EXTENSION_TO_FORMAT = {
    'jpg': 'jpeg',
    'jpeg': 'jpeg',
    'png': 'png',
    'webp': 'webp',
}
FORMAT_TO_SERVER_EXTENSION = {
    'jpeg': 'jpg',
    'png': 'png',
    'webp': 'webp',
}
SERVER_IMAGE_EXTENSIONS = ('jpg', 'png', 'webp')
MIME_BY_EXTENSION = {
    'jpg': 'image/jpeg',
    'png': 'image/png',
    'webp': 'image/webp',
}


class ProductImageError(Exception):
    """Base class for product image validation/storage failures."""


class ProductImageValidationError(ProductImageError):
    """Raised when an uploaded product image violates the upload policy."""


class ProductImageStorageError(ProductImageError):
    """Raised when a validated product image cannot be safely stored."""


@dataclass
class StagedProductImage:
    temp_path: Path
    final_path: Path
    extension: str
    mime_type: str


@dataclass
class ProductImageInfo:
    path: Path
    extension: str
    mime_type: str


def has_product_image_upload(file_storage):
    return file_storage is not None and bool(file_storage.filename)


def product_image_filename(product_id, extension):
    return f'{product_id}.{extension}'


def product_image_path(upload_dir, product_id, extension):
    return Path(upload_dir) / product_image_filename(product_id, extension)


def product_image_candidates(upload_dir, product_id):
    base_dir = Path(upload_dir)
    return [base_dir / product_image_filename(product_id, ext) for ext in SERVER_IMAGE_EXTENSIONS]


def get_product_image(upload_dir, product_id):
    for extension in SERVER_IMAGE_EXTENSIONS:
        path = product_image_path(upload_dir, product_id, extension)
        if path.is_file():
            return ProductImageInfo(
                path=path,
                extension=extension,
                mime_type=MIME_BY_EXTENSION[extension],
            )
    return None


def product_image_exists(upload_dir, product_id):
    return get_product_image(upload_dir, product_id) is not None


def stage_product_image(file_storage, product_id, upload_dir, max_bytes):
    data, extension, mime_type = _validate_uploaded_image(file_storage, max_bytes)
    upload_path = Path(upload_dir)

    try:
        upload_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProductImageStorageError() from exc

    temp_path = None
    final_path = product_image_path(upload_path, product_id, extension)

    try:
        fd, temp_name = tempfile.mkstemp(
            dir=str(upload_path),
            prefix=f'.{product_id}.',
            suffix='.upload',
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, 'wb') as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
    except OSError as exc:
        if temp_path is not None:
            remove_file_if_exists(temp_path)
        raise ProductImageStorageError() from exc

    return StagedProductImage(
        temp_path=temp_path,
        final_path=final_path,
        extension=extension,
        mime_type=mime_type,
    )


def install_staged_product_image(staged_image):
    try:
        os.replace(staged_image.temp_path, staged_image.final_path)
    except OSError as exc:
        raise ProductImageStorageError() from exc


def cleanup_staged_product_image(staged_image):
    if staged_image is not None:
        remove_file_if_exists(staged_image.temp_path)


def backup_existing_product_images(upload_dir, product_id):
    backups = []
    try:
        for image_path in product_image_candidates(upload_dir, product_id):
            if image_path.is_file():
                backup_path = image_path.with_name(
                    f'.{image_path.name}.{uuid.uuid4().hex}.bak'
                )
                os.replace(image_path, backup_path)
                backups.append((backup_path, image_path))
    except OSError as exc:
        restore_product_image_backups(backups)
        raise ProductImageStorageError() from exc

    return backups


def restore_product_image_backups(backups):
    for backup_path, original_path in reversed(backups):
        try:
            if backup_path.exists():
                os.replace(backup_path, original_path)
        except OSError:
            pass


def discard_product_image_backups(backups):
    for backup_path, _original_path in backups:
        remove_file_if_exists(backup_path)


def remove_product_images(upload_dir, product_id):
    for image_path in product_image_candidates(upload_dir, product_id):
        remove_file_if_exists(image_path)


def remove_file_if_exists(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _validate_uploaded_image(file_storage, max_bytes):
    extension = _client_extension(file_storage.filename)
    expected_format = CLIENT_EXTENSION_TO_FORMAT.get(extension)
    if expected_format is None:
        raise ProductImageValidationError()

    try:
        data = file_storage.stream.read(max_bytes + 1)
    except OSError as exc:
        raise ProductImageValidationError() from exc

    if not data:
        raise ProductImageValidationError()
    if len(data) > max_bytes:
        raise ProductImageValidationError()
    if _looks_like_unsupported_active_content(data):
        raise ProductImageValidationError()

    detected_format = _detect_image_format(data)
    if detected_format is None or detected_format != expected_format:
        raise ProductImageValidationError()

    normalized_extension = FORMAT_TO_SERVER_EXTENSION[detected_format]
    return data, normalized_extension, MIME_BY_EXTENSION[normalized_extension]


def _client_extension(filename):
    return Path(filename or '').suffix.lower().lstrip('.')


def _detect_image_format(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    return None


def _looks_like_unsupported_active_content(data):
    stripped = data[:128].lstrip().lower()
    blocked_prefixes = (
        b'<!doctype html',
        b'<html',
        b'<script',
        b'<svg',
        b'<?xml',
        b'gif87a',
        b'gif89a',
        b'mz',
        b'\x7felf',
        b'#!',
    )
    if stripped.startswith(blocked_prefixes):
        return True

    lowered = data[:512].lower()
    blocked_markers = (
        b'<script',
        b'<svg',
        b'<!doctype html',
        b'<html',
    )
    return any(marker in lowered for marker in blocked_markers)
