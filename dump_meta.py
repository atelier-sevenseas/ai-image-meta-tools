"""AI生成画像(PNG / WebP / JPEG)のメタデータをテキストファイルに書き出すスクリプト。

Version: v1.0
Updated: 2026-11-23

出力仕様:
  - 元画像と同じディレクトリに `<ファイル名>_<拡張子>_meta.txt` をUTF-8で出力します。
    例: image.png -> image_png_meta.txt / image.webp -> image_webp_meta.txt
  - メタデータが一切見つからない画像はファイルを出力せず、コンソールにのみ表示します。

必要ライブラリ:
  pip install Pillow numpy piexif

実行コマンド例:
  # 単一画像のメタデータを書き出す
  python dump_meta.py -i image.png

  # ディレクトリ直下の画像のメタデータを一括で書き出す
  python dump_meta.py -i ./image_dir

  # サブディレクトリも含めて再帰的に書き出す
  python dump_meta.py -i ./image_dir -r
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import piexif
import piexif.helper
from PIL import Image

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
STEALTH_SIG_ALPHA_COMP = b"stealth_pngcomp"
STEALTH_SIG_ALPHA_RAW = b"stealth_pnginfo"
SUPPORTED_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}


def _format_maybe_json(text: str) -> str:
    """JSON文字列であればインデント整形して返す。先頭に prompt:/workflow: が付いていても対応。"""
    stripped = text.strip()
    prefix = ""
    body = text
    for p in ("prompt:", "workflow:"):
        if stripped.startswith(p):
            idx = text.find(p)
            prefix = text[: idx + len(p)] + "\n"
            body = text[idx + len(p) :]
            break

    body_stripped = body.strip()
    if (body_stripped.startswith("{") and body_stripped.endswith("}")) or (
        body_stripped.startswith("[") and body_stripped.endswith("]")
    ):
        try:
            parsed = json.loads(body)
            return prefix + json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    return text


def _extract_png_text_chunks(data: bytes) -> list[tuple[str, str]]:
    """PNGの tEXt / iTXt / zTXt チャンクをすべて抽出する。"""
    sections: list[tuple[str, str]] = []
    pos = len(PNG_SIGNATURE)

    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            break

        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos = chunk_end

        if chunk_type == b"tEXt" and b"\x00" in chunk_data:
            key, val = chunk_data.split(b"\x00", 1)
            k_str = key.decode("latin-1", errors="replace")
            v_str = val.decode("utf-8", errors="replace")
            sections.append((f"PNG tEXt: {k_str}", _format_maybe_json(v_str)))

        elif chunk_type == b"zTXt" and b"\x00" in chunk_data:
            key, rest = chunk_data.split(b"\x00", 1)
            if len(rest) >= 1:
                k_str = key.decode("latin-1", errors="replace")
                try:
                    v_str = zlib.decompress(rest[1:]).decode("utf-8", errors="replace")
                except Exception:
                    v_str = "(zlib展開エラー)"
                sections.append((f"PNG zTXt: {k_str}", _format_maybe_json(v_str)))

        elif chunk_type == b"iTXt" and b"\x00" in chunk_data:
            key, rest = chunk_data.split(b"\x00", 1)
            if len(rest) >= 2:
                comp_flag = rest[0]
                parts = rest[2:].split(b"\x00", 2)
                if len(parts) == 3:
                    k_str = key.decode("latin-1", errors="replace")
                    text_bytes = parts[2]
                    if comp_flag == 1:
                        try:
                            v_str = zlib.decompress(text_bytes).decode("utf-8", errors="replace")
                        except Exception:
                            v_str = "(zlib展開エラー)"
                    else:
                        v_str = text_bytes.decode("utf-8", errors="replace")
                    sections.append((f"PNG iTXt: {k_str}", _format_maybe_json(v_str)))

    return sections


def _extract_exif_sections(exif_bytes: bytes) -> list[tuple[str, str]]:
    """Exifバイナリからテキスト系タグを抽出する。"""
    sections: list[tuple[str, str]] = []
    try:
        exif_dict = piexif.load(exif_bytes)
    except Exception as exc:
        return [("Exif (読み込みエラー)", str(exc))]

    uc = exif_dict.get("Exif", {}).get(piexif.ExifIFD.UserComment)
    if isinstance(uc, bytes) and uc:
        try:
            text = piexif.helper.UserComment.load(uc)
        except Exception:
            text = uc.decode("utf-8", errors="replace")
        sections.append(("Exif: UserComment", _format_maybe_json(text)))

    text_tags = (
        ("ImageDescription", piexif.ImageIFD.ImageDescription),
        ("Make", piexif.ImageIFD.Make),
        ("Model", piexif.ImageIFD.Model),
        ("Software", piexif.ImageIFD.Software),
    )
    zeroth = exif_dict.get("0th", {})
    for name, tag in text_tags:
        val = zeroth.get(tag)
        if isinstance(val, bytes) and val:
            text = val.decode("utf-8", errors="replace")
            sections.append((f"Exif: {name}", _format_maybe_json(text)))

    return sections


def _extract_stealth_section(img: Image.Image) -> tuple[str, str] | None:
    """RGBA画像からNovelAIのステルス情報を抽出する。"""
    if img.mode != "RGBA":
        return None

    alpha = np.array(img, dtype=np.uint8)[:, :, 3]
    bits = (alpha.T.reshape(-1) & 1).astype(np.uint8)
    sig_len_bits = len(STEALTH_SIG_ALPHA_COMP) * 8
    if bits.size < sig_len_bits + 32:
        return None

    sig_bytes = np.packbits(bits[:sig_len_bits]).tobytes()
    if sig_bytes not in (STEALTH_SIG_ALPHA_COMP, STEALTH_SIG_ALPHA_RAW):
        return None

    len_bytes = np.packbits(bits[sig_len_bits : sig_len_bits + 32]).tobytes()
    (payload_bit_len,) = struct.unpack(">I", len_bytes)
    start = sig_len_bits + 32
    end = start + payload_bit_len
    if payload_bit_len == 0 or end > bits.size or payload_bit_len % 8 != 0:
        return None

    payload = np.packbits(bits[start:end]).tobytes()
    try:
        if sig_bytes == STEALTH_SIG_ALPHA_COMP:
            raw_text = gzip.decompress(payload).decode("utf-8")
        else:
            raw_text = payload.decode("utf-8")
    except Exception:
        return None

    return (f"Stealth ({sig_bytes.decode()})", _format_maybe_json(raw_text))


def dump_image_metadata(filepath: Path) -> str:
    ext = filepath.suffix.lower()
    data = filepath.read_bytes()
    sections: list[tuple[str, str]] = []

    if ext == ".png":
        if not data.startswith(PNG_SIGNATURE):
            print(f"スキップ (PNGではありません): {filepath}")
            return "skipped"
        sections.extend(_extract_png_text_chunks(data))

    with Image.open(io.BytesIO(data)) as img:
        exif_bytes = img.info.get("exif")
        if exif_bytes:
            sections.extend(_extract_exif_sections(exif_bytes))

        if ext in (".png", ".webp"):
            stealth = _extract_stealth_section(img)
            if stealth is not None:
                sections.append(stealth)

    if not sections:
        print(f"メタデータなし: {filepath}")
        return "empty"

    ext_label = ext.lstrip(".")
    out_path = filepath.with_name(f"{filepath.stem}_{ext_label}_meta.txt")

    lines = [f"Source: {filepath.name}", ""]
    for title, content in sections:
        lines.append(f"=== {title} ===")
        lines.append(content)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"出力完了: {out_path}")
    return "dumped"


def collect_target_files(target: Path, recursive: bool) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        candidates = target.rglob("*") if recursive else target.glob("*")
        return sorted(
            p for p in candidates if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    raise FileNotFoundError(f"指定されたパスが見つかりません: {target}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI生成画像(PNG / WebP / JPEG)のメタデータをテキストファイルに書き出します。"
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        type=Path,
        help="処理対象の画像ファイル、またはディレクトリ",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="ディレクトリ指定時にサブディレクトリも再帰的に処理する",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    files = collect_target_files(args.input, args.recursive)

    if not files:
        print("対象の画像ファイルが見つかりませんでした。")
        return

    counts = {"dumped": 0, "empty": 0, "skipped": 0}
    for filepath in files:
        try:
            status = dump_image_metadata(filepath)
            counts[status] += 1
        except Exception as exc:
            print(f"エラー ({filepath}): {exc}")
            counts["skipped"] += 1

    print(
        f"\n完了: 全{len(files)}件 "
        f"(出力: {counts['dumped']}, メタデータなし: {counts['empty']}, スキップ/エラー: {counts['skipped']})"
    )


if __name__ == "__main__":
    main()
