# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Host-side image and PDF output for composed render images."""

from __future__ import annotations

import io
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .project import ProjectError
from .palettes import background_rgba


def _pdf_modules(poster: bool = False):
    try:
        import img2pdf
    except ImportError as exc:
        raise ProjectError("PDF output requires img2pdf") from exc
    if not poster:
        return img2pdf, None
    try:
        import pikepdf
    except ImportError as exc:
        raise ProjectError("poster PDF output requires pikepdf") from exc
    return img2pdf, pikepdf


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ProjectError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProjectError(f"{label} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ProjectError(f"{label} must be a {'positive ' if positive else ''}finite number")
    return result


def _pair(value: object, label: str, *, integer: bool = False) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ProjectError(f"{label} must contain two {'positive integers' if integer else 'positive numbers'}")
    if integer:
        if any(type(item) is not int or item <= 0 for item in value):
            raise ProjectError(f"{label} must contain two positive integers")
        return tuple(value)
    return tuple(_number(item, label, positive=True) for item in value)


@dataclass(frozen=True)
class Poster:
    grid: tuple[int, int]
    page_size_pt: tuple[float, float]
    margin_pt: float
    overlap_pt: float

    @property
    def printable(self) -> tuple[float, float]:
        return tuple(size - 2 * self.margin_pt for size in self.page_size_pt)


def poster_options(value: object) -> Poster:
    if not isinstance(value, dict):
        raise ProjectError("[render.poster] must be a table")
    grid = _pair(value.get("grid"), "[render.poster].grid", integer=True)
    width_mm, height_mm = _pair(value.get("page_size_mm", [210, 297]),
                                "[render.poster].page_size_mm")
    margin_mm = _number(value.get("margin_mm", 10), "[render.poster].margin_mm")
    overlap_mm = _number(value.get("overlap_mm", 0), "[render.poster].overlap_mm")
    if margin_mm < 0 or overlap_mm < 0:
        raise ProjectError("[render.poster] margin_mm and overlap_mm must be nonnegative")
    factor = 72 / 25.4
    page_size = width_mm * factor, height_mm * factor
    if any(size < 3 or size > 14400 for size in page_size):
        raise ProjectError("[render.poster].page_size_mm is outside the PDF page-size range")
    margin, overlap = margin_mm * factor, overlap_mm * factor
    printable = page_size[0] - 2 * margin, page_size[1] - 2 * margin
    if min(printable) <= 0 or overlap >= min(printable):
        raise ProjectError("[render.poster] margins or overlap leave no printable area")
    return Poster(grid, page_size, margin, overlap)


def opaque_rgb(image):
    """Return RGB only for fully opaque images; preserve all other RGBA samples."""
    if image.getextrema()[3] == (255, 255):
        return image.convert("RGB")
    return None


def jpeg_image(image, background: tuple[int, int, int]):
    from PIL import Image
    flat = Image.new("RGBA", image.size, background + (255,))
    flat.alpha_composite(image)
    result = flat.convert("RGB")
    flat.close()
    return result


def jpeg_background(render: dict) -> tuple[int, int, int]:
    rgba = background_rgba(render.get("jpeg_background", "#ffffff"))
    if rgba[3] != 255:
        raise ProjectError("[render].jpeg_background must be opaque")
    return rgba[:3]


def write_pdf(image, target: Path, dpi: float) -> None:
    img2pdf, _ = _pdf_modules()
    rgb = opaque_rgb(image)
    source = rgb if rgb is not None else image
    try:
        with tempfile.TemporaryDirectory(dir=target.parent) as directory:
            png = Path(directory) / "image.png"
            pdf = Path(directory) / "image.pdf"
            source.save(png, "PNG")
            layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
            with pdf.open("wb") as output:
                img2pdf.convert(str(png), layout_fun=layout, outputstream=output)
            pdf.replace(target)
    finally:
        if rgb is not None:
            rgb.close()


def _page_crop(image, poster: Poster, col: int, row: int):
    """Crop on the source pixel grid and return its PDF position in the sheet."""
    width, height = image.size
    printable_w, printable_h = poster.printable
    cols, rows = poster.grid
    canvas_w = cols * printable_w - (cols - 1) * poster.overlap_pt
    canvas_h = rows * printable_h - (rows - 1) * poster.overlap_pt
    scale = min(canvas_w / width, canvas_h / height)
    image_left = (canvas_w - width * scale) / 2
    image_top = (canvas_h - height * scale) / 2
    window_left = col * (printable_w - poster.overlap_pt)
    window_top = row * (printable_h - poster.overlap_pt)
    x0 = max(0, math.floor((window_left - image_left) / scale))
    x1 = min(width, math.ceil((window_left + printable_w - image_left) / scale))
    y0 = max(0, math.floor((window_top - image_top) / scale))
    y1 = min(height, math.ceil((window_top + printable_h - image_top) / scale))
    if x0 >= x1 or y0 >= y1:
        return None
    page_x = poster.margin_pt + image_left + x0 * scale - window_left
    page_top = poster.margin_pt + image_top + y0 * scale - window_top
    page_y = poster.page_size_pt[1] - page_top - (y1 - y0) * scale
    return (x0, y0, x1, y1), (page_x, page_y), scale


def write_poster(image, target: Path, poster: Poster) -> None:
    img2pdf, pikepdf = _pdf_modules(poster=True)
    opaque = image.getextrema()[3] == (255, 255)
    printable_w, printable_h = poster.printable
    with pikepdf.Pdf.new() as document, tempfile.TemporaryDirectory(
            dir=target.parent) as directory:
        for row in range(poster.grid[1]):
            for col in range(poster.grid[0]):
                crop_info = _page_crop(image, poster, col, row)
                if crop_info is None:
                    document.add_blank_page(page_size=poster.page_size_pt)
                    continue
                box, (page_x, page_y), scale = crop_info
                crop = image.crop(box)
                try:
                    if opaque:
                        rgb = crop.convert("RGB")
                        crop.close()
                        crop = rgb
                    with tempfile.NamedTemporaryFile(suffix=".png", dir=target.parent) as png:
                        crop.save(png, "PNG")
                        png.flush()
                        image_w = crop.width * scale
                        image_h = crop.height * scale
                        layout = lambda _w, _h, _dpi: (
                            *poster.page_size_pt, image_w, image_h)
                        page_pdf = img2pdf.convert(png.name, layout_fun=layout)
                finally:
                    crop.close()
                with pikepdf.Pdf.open(io.BytesIO(page_pdf)) as one_page:
                    document.pages.append(one_page.pages[0])
                page = document.pages[-1]
                content = (f"q\n{poster.margin_pt:.10f} {poster.margin_pt:.10f} "
                           f"{printable_w:.10f} {printable_h:.10f} re W n\n"
                           f"{image_w:.10f} 0 0 {image_h:.10f} "
                           f"{page_x:.10f} {page_y:.10f} cm\n/Im0 Do\nQ").encode("ascii")
                page.Contents = document.make_stream(content)
        pdf = Path(directory) / "poster.pdf"
        document.save(pdf)
        pdf.replace(target)
