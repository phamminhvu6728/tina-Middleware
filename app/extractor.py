import io
import logging
import re
from typing import Tuple
from PIL import Image
import pymupdf

logger = logging.getLogger(__name__)

_paddle_ocr = None
_tesseract_available = None

def get_paddle_ocr():
    global _paddle_ocr
    if _paddle_ocr is None:
        try:
            from paddleocr import PaddleOCR
            _paddle_ocr = PaddleOCR(use_angle_cls=True, lang='vi', show_log=False)
            logger.info("PaddleOCR initialized successfully.")
        except Exception:
            _paddle_ocr = False
    return _paddle_ocr if _paddle_ocr is not False else None

def check_tesseract_available() -> bool:
    global _tesseract_available
    if _tesseract_available is None:
        try:
            import pytesseract
            _tesseract_available = True
        except Exception:
            _tesseract_available = False
    return _tesseract_available

def clean_extracted_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()

def run_ocr_on_pil_image(image: Image.Image) -> str:
    """Runs PaddleOCR or Tesseract on a PIL Image."""
    ocr = get_paddle_ocr()
    if ocr is not None:
        try:
            import numpy as np
            img_np = np.array(image.convert("RGB"))
            result = ocr.ocr(img_np, cls=True)
            lines = []
            if result and result[0]:
                for line in result[0]:
                    if line and len(line) >= 2 and line[1]:
                        lines.append(line[1][0])
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"PaddleOCR execution error: {e}")

    if check_tesseract_available():
        try:
            import pytesseract
            try:
                return pytesseract.image_to_string(image, lang='vie+eng')
            except Exception:
                return pytesseract.image_to_string(image, lang='eng')
        except Exception as e:
            logger.error(f"Tesseract execution error: {e}")

    return ""

def extract_text_from_pdf(pdf_bytes: bytes) -> Tuple[str, str]:
    """
    Hybrid PDF extraction:
    1. Fast path: PyMuPDF digital text layer
    2. Fallback: OCR on rendered page images if digital text is empty (< 100 chars)
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.error(f"PyMuPDF failed to open PDF: {e}")
        return "", "error"

    # Step 1: Digital text layer
    page_texts = []
    for page in doc:
        page_texts.append(page.get_text())

    full_digital_text = clean_extracted_text("\n".join(page_texts))
    if len(full_digital_text) >= 100:
        logger.info(f"Digital text layer extracted: {len(full_digital_text)} chars")
        return full_digital_text, "text_layer"

    # Step 2: Scanned / Image PDF fallback -> Render pages to images and OCR
    logger.info(f"Digital text layer has only {len(full_digital_text)} chars. Falling back to OCR...")
    ocr_texts = []
    for page_idx, page in enumerate(doc):
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        page_ocr = run_ocr_on_pil_image(img)
        logger.info(f"Page {page_idx+1} OCR extracted {len(page_ocr)} chars")
        ocr_texts.append(page_ocr)

    full_ocr_text = clean_extracted_text("\n".join(ocr_texts))
    if full_ocr_text:
        return full_ocr_text, "ocr"
    
    return full_digital_text, "text_layer_sparse"

def extract_text(file_bytes: bytes, content_type: str = "") -> Tuple[str, str]:
    """
    Universal text extractor for PDF, Images, or plain text.
    """
    if not file_bytes:
        return "", "empty"

    trimmed_bytes = file_bytes.lstrip(b'\r\n\t ')
    if trimmed_bytes.startswith(b"%PDF") or b"%PDF" in file_bytes[:1024] or "pdf" in content_type.lower():
        return extract_text_from_pdf(trimmed_bytes)

    try:
        img = Image.open(io.BytesIO(file_bytes))
        ocr_result = clean_extracted_text(run_ocr_on_pil_image(img))
        return ocr_result, "ocr_image"
    except Exception:
        pass

    try:
        decoded = file_bytes.decode('utf-8', errors='replace')
        return clean_extracted_text(decoded), "raw_text"
    except Exception:
        return "", "unsupported"
