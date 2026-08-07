"""Image normalisation shared by the builder and the prefetcher."""
from __future__ import annotations

import io


def to_jpeg(data: bytes) -> bytes:
    """Transcode anything that is not already JPEG.

    Some sources serve PNG or BMP regardless of the URL extension, but our
    linksets declare image/jpeg and VLM APIs reject BMP outright.
    """
    if data[:3] == b"\xff\xd8\xff":
        return data
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()
