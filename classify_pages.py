#!/usr/bin/env python3

import argparse
import csv
from pathlib import Path

import fitz


TEXT_CHARACTER_THRESHOLD = 30
IMAGE_AREA_THRESHOLD = 0.02
FULL_PAGE_IMAGE_THRESHOLD = 0.75


def call_compatible(obj, method_names, *args, **kwargs):
    """
    Call the first available method.

    Supports API differences such as:
        page.get_text(...)
        page.getText(...)
    """
    last_error = None

    for method_name in method_names:
        method = getattr(obj, method_name, None)

        if method is None:
            continue

        try:
            return method(*args, **kwargs)
        except TypeError as exc:
            # Some older versions do not accept newer keyword arguments,
            # such as sort=True.
            last_error = exc

            if kwargs:
                try:
                    return method(*args)
                except Exception as retry_exc:
                    last_error = retry_exc
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    raise AttributeError(
        f"None of these methods exist: {', '.join(method_names)}"
    )


def rectangle_coordinates(rectangle):
    """
    Return normalized x0, y0, x1, y1 coordinates.

    Avoids depending on Rect.get_area(), getArea(),
    is_empty or isEmpty.
    """
    rect = fitz.Rect(rectangle)

    x0 = min(rect.x0, rect.x1)
    y0 = min(rect.y0, rect.y1)
    x1 = max(rect.x0, rect.x1)
    y1 = max(rect.y0, rect.y1)

    return x0, y0, x1, y1


def rectangle_area(rectangle):
    """Calculate rectangle area without Rect.get_area()."""
    x0, y0, x1, y1 = rectangle_coordinates(rectangle)

    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)

    return width * height


def clipped_area(bbox, page_rectangle):
    """
    Calculate the intersection area between a bounding box and the page.

    This implementation does not depend on Rect.intersect().
    """
    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = rectangle_coordinates(bbox)
    page_x0, page_y0, page_x1, page_y1 = rectangle_coordinates(page_rectangle)

    intersection_x0 = max(bbox_x0, page_x0)
    intersection_y0 = max(bbox_y0, page_y0)
    intersection_x1 = min(bbox_x1, page_x1)
    intersection_y1 = min(bbox_y1, page_y1)

    width = max(intersection_x1 - intersection_x0, 0.0)
    height = max(intersection_y1 - intersection_y0, 0.0)

    return width * height


def extract_text(page):
    """Extract plain text using old or new fitz method names."""
    try:
        return call_compatible(
            page,
            ("get_text", "getText"),
            "text",
            sort=True,
        )
    except Exception:
        return ""


def extract_words(page):
    """Extract words using old or new fitz method names."""
    try:
        return call_compatible(
            page,
            ("get_text", "getText"),
            "words",
            sort=True,
        )
    except Exception:
        return []


def extract_image_info(page):
    """
    Return image information containing at least a bbox when available.

    Preferred:
        page.get_image_info()

    Fallback:
        image blocks from page.get_text("dict")
    """
    get_image_info = getattr(page, "get_image_info", None)

    if get_image_info is not None:
        try:
            return get_image_info()
        except Exception:
            pass

    # Older versions may expose camelCase.
    get_image_info_old = getattr(page, "getImageInfo", None)

    if get_image_info_old is not None:
        try:
            return get_image_info_old()
        except Exception:
            pass

    # Fallback to the structured text dictionary. Image blocks have type 1.
    try:
        page_dictionary = call_compatible(
            page,
            ("get_text", "getText"),
            "dict",
        )
    except Exception:
        return []

    image_info = []

    for block in page_dictionary.get("blocks", []):
        if block.get("type") != 1:
            continue

        bbox = block.get("bbox")

        if bbox is not None:
            image_info.append(
                {
                    "bbox": bbox,
                    "width": block.get("width"),
                    "height": block.get("height"),
                    "extension": block.get("ext"),
                }
            )

    return image_info


def extract_vector_drawings(page):
    """Extract PDF vector drawing paths."""
    try:
        return call_compatible(
            page,
            ("get_drawings", "getDrawings"),
        )
    except Exception:
        return []


def detect_tables(page):
    """
    Detect tables when Page.find_tables() is available.

    Older fitz/PyMuPDF versions may not support this method.
    """
    find_tables = getattr(page, "find_tables", None)

    if find_tables is None:
        return {
            "supported": False,
            "count": 0,
            "error": "",
        }

    try:
        table_finder = find_tables()
        tables = getattr(table_finder, "tables", [])

        return {
            "supported": True,
            "count": len(tables),
            "error": "",
        }
    except Exception as exc:
        return {
            "supported": True,
            "count": 0,
            "error": str(exc),
        }


def classify_page(page):
    page_rectangle = fitz.Rect(page.rect)
    page_area = max(rectangle_area(page_rectangle), 1.0)

    # Text layer
    text = extract_text(page)
    words = extract_words(page)

    text_characters = len("".join(text.split()))
    word_count = len(words)

    # Distinguish any text layer from a substantial body of text.
    has_text_layer = text_characters > 0
    has_text = text_characters >= TEXT_CHARACTER_THRESHOLD

    # Raster images
    image_info = extract_image_info(page)

    image_area = 0.0

    for image in image_info:
        bbox = image.get("bbox")

        if bbox is None:
            continue

        try:
            image_area += clipped_area(bbox, page_rectangle)
        except Exception:
            continue

    # Multiple overlapping images could otherwise produce a value above 1.
    image_area_ratio = min(image_area / page_area, 1.0)

    image_count = len(image_info)
    has_raster_image = (
        image_count > 0
        and image_area_ratio >= IMAGE_AREA_THRESHOLD
    )

    # Vector graphics
    vector_drawings = extract_vector_drawings(page)
    vector_path_count = len(vector_drawings)
    has_vector_graphics = vector_path_count > 0

    # Tables
    table_result = detect_tables(page)
    table_count = table_result["count"]
    has_table = table_count > 0

    # A scan-backed page generally consists of one large raster image.
    scan_backed = (
        image_count > 0
        and image_area_ratio >= FULL_PAGE_IMAGE_THRESHOLD
    )

    needs_ocr = scan_backed and not has_text_layer

    labels = []

    if has_text_layer:
        labels.append("text")

    if has_table:
        labels.append("table")

    if has_raster_image:
        labels.append("raster-image")

    if has_vector_graphics:
        labels.append("vector-graphics")

    if scan_backed:
        labels.append("scan-backed")

    if not labels:
        labels.append("blank-or-unknown")

    return {
        "labels": ",".join(labels),
        "has_text_layer": has_text_layer,
        "has_text": has_text,
        "has_table": has_table,
        "has_raster_image": has_raster_image,
        "has_vector_graphics": has_vector_graphics,
        "scan_backed": scan_backed,
        "needs_ocr": needs_ocr,
        "text_characters": text_characters,
        "word_count": word_count,
        "table_count": table_count,
        "image_count": image_count,
        "image_area_ratio": round(image_area_ratio, 4),
        "vector_path_count": vector_path_count,
        "table_detection_supported": table_result["supported"],
        "table_detection_error": table_result["error"],
    }


def find_page_pdfs(root):
    """
    Find PDFs generated by the earlier page-splitting step.

    Expected filename:
        document_page_0001.pdf
    """
    for path in sorted(root.rglob("*.pdf")):
        if "_page_" in path.stem:
            yield path


def main():
    parser = argparse.ArgumentParser(
        description="Classify individual PDF pages using fitz."
    )

    parser.add_argument(
        "root",
        type=Path,
        help="Root directory containing page-level PDF files",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("page_classification.csv"),
        help="Output CSV filename",
    )

    args = parser.parse_args()

    fields = [
        "page_file",
        "labels",
        "has_text_layer",
        "has_text",
        "has_table",
        "has_raster_image",
        "has_vector_graphics",
        "scan_backed",
        "needs_ocr",
        "text_characters",
        "word_count",
        "table_count",
        "image_count",
        "image_area_ratio",
        "vector_path_count",
        "table_detection_supported",
        "table_detection_error",
        "error",
    ]

    processed = 0
    failed = 0

    with args.output.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=fields,
        )
        writer.writeheader()

        for pdf_path in find_page_pdfs(args.root):
            row = {
                "page_file": str(pdf_path),
                "error": "",
            }

            try:
                document = fitz.open(str(pdf_path))

                try:
                    if len(document) == 0:
                        raise ValueError("PDF contains no pages")

                    page = document[0]
                    row.update(classify_page(page))
                    processed += 1
                finally:
                    document.close()

            except Exception as exc:
                row["error"] = str(exc)
                failed += 1

            writer.writerow(row)

    print(f"Successfully classified: {processed}")
    print(f"Failed: {failed}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()