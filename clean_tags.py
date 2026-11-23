"""AI生成画像(PNG / WebP / JPEG)のメタデータから指定タグを除去、またはメタデータを完全消去するスクリプト。

Version: v2.1
Updated: 2026-11-23

対応形式:
  - PNG  : ComfyUI (prompt/workflow), A1111・Forge (parameters), NovelAI (Description/Comment + ステルス情報)
  - WebP : Exif (UserComment, ImageDescription, Make等) + 可逆RGBA時のステルス情報
  - JPEG : Exif (UserComment, ImageDescription, Make等)

必要ライブラリ:
  pip install Pillow numpy piexif

実行コマンド例:
  # 単一ファイルから指定タグのみ除去する
  python clean_tags.py -i image.png

  # 外部タグリストファイルを使って別ディレクトリへ出力する（元ファイルは上書きしない）
  python clean_tags.py -i ./image_dir -r -f tags.txt -o ./output_dir

  # ファイルを書き換えずに確認する（ドライラン・確認プロンプトなし）
  python clean_tags.py -i ./image_dir -r --dry-run

  # コマンドラインから除去タグを指定する（DEFAULT_TARGET_TAGS は無視）
  python clean_tags.py -i image.png -t artist_tag_1 "artist_tag_(2)"

  # 単一ファイルのメタデータを完全消去する（ステルス情報含む・透過維持）
  python clean_tags.py -i image.png -s

  # 確認プロンプトをスキップしてディレクトリ内のメタデータを完全消去する
  python clean_tags.py -i ./image_dir -r -s -y
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import piexif
import piexif.helper
from PIL import Image

# デフォルトの除去対象タグ（元の表記のみ記述すれば、スペース/アンダースコア揺れや括弧のエスケープは自動対応）
DEFAULT_TARGET_TAGS = [
    "artist_tag_1",
    "artist_tag_(2)",
]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JSON_CHUNK_KEYS = (b"prompt", b"workflow", b"Comment")
TEXT_CHUNK_KEYS = (b"Description",)
A1111_CHUNK_KEYS = (b"parameters",)
STRIP_PNG_CHUNK_TYPES = (b"tEXt", b"iTXt", b"zTXt", b"eXIf")

STEALTH_SIG_ALPHA_COMP = b"stealth_pngcomp"
STEALTH_SIG_ALPHA_RAW = b"stealth_pnginfo"

SUPPORTED_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}


class TagCleaner:
    def __init__(self, target_tags: list[str]) -> None:
        cleaned_tags = [
            re.sub(r"\s+", " ", t.strip(" \t\r\n,"))
            for t in target_tags
        ]
        sorted_tags = sorted(
            (t for t in cleaned_tags if t),
            key=len,
            reverse=True,
        )
        if sorted_tags:
            joined = "|".join(self._build_tag_pattern(t) for t in sorted_tags)
            # artist: プレフィックスおよび :1.2 形式の重みをオプショナルで吸収
            self.tag_pattern = re.compile(
                rf"(?<![a-zA-Z0-9_\-])(?:artist\s*:\s*)?(?:{joined})(?:\s*:\s*[+-]?[0-9.]+)?(?![a-zA-Z0-9_\-])",
                re.IGNORECASE,
            )
        else:
            self.tag_pattern = None

    @staticmethod
    def _build_tag_pattern(tag: str) -> str:
        # 先頭に artist: が含まれている場合は除去して統一（正規表現本体側で自動対応するため）
        normalized = re.sub(r"^artist\s*:\s*", "", tag, flags=re.IGNORECASE)
        normalized = re.sub(r"\\([()])", r"\1", normalized)
        parts = []
        for ch in normalized:
            if ch in ("_", " "):
                parts.append(r"[ _]")
            elif ch == "(":
                parts.append(r"\\?\(")
            elif ch == ")":
                parts.append(r"\\?\)")
            else:
                parts.append(re.escape(ch))
        return "".join(parts)

    def clean_string(self, text: str) -> tuple[str, bool]:
        if not self.tag_pattern:
            return text, False

        cleaned, count = self.tag_pattern.subn("", text)
        if count == 0:
            return text, False

        cleaned = cleaned.replace("\u3000", " ")

        while True:
            prev = cleaned
            cleaned = re.sub(r"[ \t]*,[ \t]*,+", ",", cleaned)
            cleaned = re.sub(r"\(\s*,\s*", "(", cleaned)
            cleaned = re.sub(r"\s*,\s*\)", ")", cleaned)
            cleaned = re.sub(r"\{\s*,\s*", "{", cleaned)
            cleaned = re.sub(r"\s*,\s*\}", "}", cleaned)
            cleaned = re.sub(r"\[\s*,\s*", "[", cleaned)
            cleaned = re.sub(r"\s*,\s*\]", "]", cleaned)
            # NovelAI V4/V5 の数値重み記法 (2.0::...::) 内のカンマ整形・空ブロック削除
            cleaned = re.sub(r"([+-]?[0-9.]+::)\s*,\s*", r"\1", cleaned)
            cleaned = re.sub(r"\s*,\s*::", "::", cleaned)
            cleaned = re.sub(r"[+-]?[0-9.]+::\s*(?:,\s*)*::", "", cleaned)
            cleaned = re.sub(r"^[ \t]*,[ \t]*", "", cleaned, flags=re.MULTILINE)
            cleaned = re.sub(r",[ \t]*$", "", cleaned)
            cleaned = re.sub(r"\(\s*(?:,\s*)*\)", "", cleaned)
            cleaned = re.sub(r"\{\s*(?:,\s*)*\}", "", cleaned)
            cleaned = re.sub(r"\[\s*(?:,\s*)*\]", "", cleaned)
            cleaned = re.sub(r"\(\s*:\s*[+-]?[0-9.]+\s*\)", "", cleaned)

            if cleaned == prev:
                break

        cleaned = re.sub(r" {2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+,", ",", cleaned)
        cleaned = cleaned.strip()
        return cleaned, (cleaned != text)

    def clean_json(self, obj: Any) -> tuple[Any, bool]:
        if isinstance(obj, dict):
            changed = False
            new_dict = {}
            for k, v in obj.items():
                new_v, c = self.clean_json(v)
                new_dict[k] = new_v
                changed = changed or c
            return new_dict, changed

        if isinstance(obj, list):
            changed = False
            new_list = []
            for item in obj:
                new_item, c = self.clean_json(item)
                new_list.append(new_item)
                changed = changed or c
            return new_list, changed

        if isinstance(obj, str):
            stripped = obj.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    nested = json.loads(obj)
                    cleaned_nested, c = self.clean_json(nested)
                    if c:
                        return (
                            json.dumps(
                                cleaned_nested, ensure_ascii=False, separators=(",", ":")
                            ),
                            True,
                        )
                    return obj, False
                except json.JSONDecodeError:
                    pass
            return self.clean_string(obj)

        return obj, False

    def clean_a1111_parameters(self, text: str) -> tuple[str, bool]:
        lines = text.splitlines()
        param_idx = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("Steps:"):
                param_idx = i
                break

        prompt_block = "\n".join(lines[:param_idx])
        footer_block = "\n".join(lines[param_idx:])

        neg_marker = "Negative prompt:"
        if neg_marker in prompt_block:
            pos_part, neg_part = prompt_block.split(neg_marker, 1)
            new_pos, c1 = self.clean_string(pos_part)
            new_neg, c2 = self.clean_string(neg_part)
            if not (c1 or c2):
                return text, False
            rebuilt_prompt = f"{new_pos}\n{neg_marker} {new_neg}".strip()
        else:
            rebuilt_prompt, changed = self.clean_string(prompt_block)
            if not changed:
                return text, False

        if footer_block:
            result = f"{rebuilt_prompt}\n{footer_block}"
        else:
            result = rebuilt_prompt
        return result, (result != text)

    def clean_auto_text(self, text: str, ensure_ascii: bool = False) -> tuple[str, bool]:
        stripped = text.strip()
        prefix = ""
        body = text
        for p in ("prompt:", "workflow:"):
            if stripped.startswith(p):
                idx = text.find(p)
                prefix = text[: idx + len(p)]
                body = text[idx + len(p) :]
                break

        body_stripped = body.strip()
        if body_stripped.startswith("{") and body_stripped.endswith("}"):
            try:
                json_data = json.loads(body)
                cleaned_data, changed = self.clean_json(json_data)
                if changed:
                    dumped = json.dumps(
                        cleaned_data, ensure_ascii=ensure_ascii, separators=(",", ":")
                    )
                    return prefix + dumped, True
                return text, False
            except json.JSONDecodeError:
                pass

        if "Steps:" in text or "Negative prompt:" in text:
            return self.clean_a1111_parameters(text)

        return self.clean_string(text)


# --- ステルス情報（NovelAI）処理 ---


def _extract_stealth_alpha(alpha: np.ndarray) -> tuple[bytes, str] | None:
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

    return sig_bytes, raw_text


def _embed_stealth_alpha(alpha: np.ndarray, sig_bytes: bytes, text: str) -> np.ndarray:
    if sig_bytes == STEALTH_SIG_ALPHA_COMP:
        payload = gzip.compress(text.encode("utf-8"))
    else:
        payload = text.encode("utf-8")

    header = sig_bytes + struct.pack(">I", len(payload) * 8) + payload
    new_bits = np.unpackbits(np.frombuffer(header, dtype=np.uint8))

    flat_alpha = alpha.T.reshape(-1).copy()
    if new_bits.size > flat_alpha.size:
        raise ValueError("ステルス情報のサイズが大きすぎて画像に収まりません")

    flat_alpha[: new_bits.size] = (flat_alpha[: new_bits.size] & 0xFE) | new_bits
    return flat_alpha.reshape(alpha.T.shape).T


def _strip_stealth_alpha(alpha: np.ndarray) -> np.ndarray:
    """透過表現を保ったまま、アルファチャンネルLSBのステルス情報を完全消去する。"""
    return np.where(
        alpha <= 1,
        0,
        np.where(alpha >= 254, 255, alpha & 0xFE),
    ).astype(np.uint8)


def _process_stealth_if_needed(
    img_bytes: bytes,
    cleaner: TagCleaner | None,
    fmt: str = "PNG",
    strip_all: bool = False,
) -> tuple[bytes, bool]:
    with Image.open(io.BytesIO(img_bytes)) as img:
        if img.mode != "RGBA":
            return img_bytes, False
        rgba = np.array(img, dtype=np.uint8)
        exif_bytes = img.info.get("exif")

    extracted = _extract_stealth_alpha(rgba[:, :, 3])
    if extracted is None:
        return img_bytes, False

    if strip_all:
        rgba[:, :, 3] = _strip_stealth_alpha(rgba[:, :, 3])
    else:
        assert cleaner is not None
        sig_bytes, raw_text = extracted
        try:
            json_data = json.loads(raw_text)
            cleaned_data, changed = cleaner.clean_json(json_data)
            if not changed:
                return img_bytes, False
            new_text = json.dumps(cleaned_data, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            new_text, changed = cleaner.clean_string(raw_text)
            if not changed:
                return img_bytes, False

        rgba[:, :, 3] = _embed_stealth_alpha(rgba[:, :, 3], sig_bytes, new_text)

    buf = io.BytesIO()
    if fmt == "PNG":
        Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
        return _merge_ancillary_chunks(original_png=img_bytes, pixel_png=buf.getvalue()), True
    else:
        save_kwargs: dict[str, Any] = {"format": "WEBP", "lossless": True}
        if exif_bytes and not strip_all:
            save_kwargs["exif"] = exif_bytes
        Image.fromarray(rgba, mode="RGBA").save(buf, **save_kwargs)
        return buf.getvalue(), True


# --- PNG 処理 ---


def _merge_ancillary_chunks(original_png: bytes, pixel_png: bytes) -> bytes:
    """pixel_pngのIHDR/IDAT/IENDに対し、original_pngの補助チャンク（色管理・テキスト等）を移植する。"""
    ancillary: list[bytes] = []
    pos = len(PNG_SIGNATURE)
    while pos + 12 <= len(original_png):
        (length,) = struct.unpack(">I", original_png[pos : pos + 4])
        chunk_end = pos + 12 + length
        chunk_type = original_png[pos + 4 : pos + 8]
        if chunk_type not in (b"IHDR", b"IDAT", b"IEND"):
            ancillary.append(original_png[pos:chunk_end])
        pos = chunk_end

    out = [PNG_SIGNATURE]
    pos = len(PNG_SIGNATURE)
    inserted = False
    while pos + 12 <= len(pixel_png):
        (length,) = struct.unpack(">I", pixel_png[pos : pos + 4])
        chunk_end = pos + 12 + length
        chunk_type = pixel_png[pos + 4 : pos + 8]
        if chunk_type == b"IDAT" and not inserted:
            out.extend(ancillary)
            inserted = True
        out.append(pixel_png[pos:chunk_end])
        pos = chunk_end

    return b"".join(out)


def _strip_png_metadata_chunks(data: bytes) -> tuple[bytes, bool]:
    """PNGからテキスト系チャンクおよびeXIfチャンクをすべて削除する（色管理チャンクは維持）。"""
    pos = len(PNG_SIGNATURE)
    new_chunks = [PNG_SIGNATURE]
    modified = False

    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            raise ValueError("PNGチャンクが破損しています")

        chunk_type = data[pos + 4 : pos + 8]
        raw_chunk = data[pos:chunk_end]
        pos = chunk_end

        if chunk_type in STRIP_PNG_CHUNK_TYPES:
            modified = True
            continue

        new_chunks.append(raw_chunk)

    return b"".join(new_chunks), modified


def _clean_text_chunk_value(
    key: bytes, raw_text: str, cleaner: TagCleaner
) -> tuple[str, bool]:
    if key in JSON_CHUNK_KEYS:
        try:
            json_data = json.loads(raw_text)
            cleaned_data, changed = cleaner.clean_json(json_data)
            if changed:
                return (
                    json.dumps(cleaned_data, ensure_ascii=True, separators=(",", ":")),
                    True,
                )
            return raw_text, False
        except json.JSONDecodeError:
            return cleaner.clean_string(raw_text)

    if key in A1111_CHUNK_KEYS:
        return cleaner.clean_a1111_parameters(raw_text)

    if key in TEXT_CHUNK_KEYS:
        return cleaner.clean_string(raw_text)

    return raw_text, False


def _clean_png_text_chunks(data: bytes, cleaner: TagCleaner) -> tuple[bytes, bool]:
    pos = len(PNG_SIGNATURE)
    new_chunks = [PNG_SIGNATURE]
    modified = False

    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            raise ValueError("PNGチャンクが破損しています")

        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]
        raw_chunk = data[pos:chunk_end]
        pos = chunk_end

        if chunk_type == b"tEXt" and b"\x00" in chunk_data:
            key, val = chunk_data.split(b"\x00", 1)
            if key in JSON_CHUNK_KEYS + TEXT_CHUNK_KEYS + A1111_CHUNK_KEYS:
                raw_text = val.decode("utf-8", errors="replace")
                new_text, changed = _clean_text_chunk_value(key, raw_text, cleaner)
                if changed:
                    new_chunk_data = key + b"\x00" + new_text.encode("utf-8")
                    new_len = struct.pack(">I", len(new_chunk_data))
                    new_crc = struct.pack(
                        ">I", zlib.crc32(chunk_type + new_chunk_data) & 0xFFFFFFFF
                    )
                    new_chunks.append(new_len + chunk_type + new_chunk_data + new_crc)
                    modified = True
                    continue

        elif chunk_type == b"iTXt" and b"\x00" in chunk_data:
            key, rest = chunk_data.split(b"\x00", 1)
            if key in JSON_CHUNK_KEYS + TEXT_CHUNK_KEYS + A1111_CHUNK_KEYS and len(rest) >= 2:
                comp_flag = rest[0]
                comp_method = rest[1]
                parts = rest[2:].split(b"\x00", 2)
                if len(parts) == 3:
                    lang, trans_key, text_bytes = parts
                    if comp_flag == 1:
                        text_bytes = zlib.decompress(text_bytes)
                    raw_text = text_bytes.decode("utf-8", errors="replace")
                    new_text, changed = _clean_text_chunk_value(key, raw_text, cleaner)
                    if changed:
                        out_bytes = new_text.encode("utf-8")
                        if comp_flag == 1:
                            out_bytes = zlib.compress(out_bytes)
                        new_chunk_data = (
                            key
                            + b"\x00"
                            + bytes([comp_flag, comp_method])
                            + lang
                            + b"\x00"
                            + trans_key
                            + b"\x00"
                            + out_bytes
                        )
                        new_len = struct.pack(">I", len(new_chunk_data))
                        new_crc = struct.pack(
                            ">I", zlib.crc32(chunk_type + new_chunk_data) & 0xFFFFFFFF
                        )
                        new_chunks.append(new_len + chunk_type + new_chunk_data + new_crc)
                        modified = True
                        continue

        new_chunks.append(raw_chunk)

    return b"".join(new_chunks), modified


# --- WebP / JPEG (Exif) 処理 ---


def _clean_exif_bytes(exif_bytes: bytes, cleaner: TagCleaner) -> tuple[bytes, bool]:
    exif_dict = piexif.load(exif_bytes)
    modified = False

    uc_raw = exif_dict.get("Exif", {}).get(piexif.ExifIFD.UserComment)
    if isinstance(uc_raw, bytes) and uc_raw:
        try:
            uc_text = piexif.helper.UserComment.load(uc_raw)
            encoding = "unicode" if uc_raw.startswith(b"UNICODE") else "ascii"
        except Exception:
            uc_text = uc_raw.decode("utf-8", errors="replace")
            encoding = "unicode"

        new_uc, changed = cleaner.clean_auto_text(uc_text, ensure_ascii=(encoding == "ascii"))
        if changed:
            enc = "unicode" if any(ord(c) > 127 for c in new_uc) else encoding
            exif_dict["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(
                new_uc, encoding=enc
            )
            modified = True

    text_tags = (
        piexif.ImageIFD.ImageDescription,
        piexif.ImageIFD.Make,
        piexif.ImageIFD.Model,
        piexif.ImageIFD.Software,
    )
    zeroth = exif_dict.get("0th", {})
    for tag in text_tags:
        val = zeroth.get(tag)
        if isinstance(val, bytes) and val:
            raw_text = val.decode("utf-8", errors="replace")
            new_text, changed = cleaner.clean_auto_text(raw_text, ensure_ascii=True)
            if changed:
                zeroth[tag] = new_text.encode("utf-8")
                modified = True

    if not modified:
        return exif_bytes, False

    return piexif.dump(exif_dict), True


def _write_atomic(filepath: Path, data: bytes) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _resolve_output_path(
    filepath: Path, input_root: Path, output_dir: Path | None
) -> Path:
    if output_dir is None:
        return filepath
    if input_root.is_dir():
        return output_dir / filepath.relative_to(input_root)
    return output_dir / filepath.name


def clean_image(
    filepath: Path,
    dest_path: Path,
    cleaner: TagCleaner | None,
    dry_run: bool = False,
    strip_all: bool = False,
) -> str:
    ext = filepath.suffix.lower()
    data = filepath.read_bytes()
    action_label = "完全消去" if strip_all else "更新"

    if ext == ".png":
        if not data.startswith(PNG_SIGNATURE):
            print(f"スキップ (PNGではありません): {filepath}")
            return "skipped"

        if strip_all:
            data, text_mod = _strip_png_metadata_chunks(data)
            data, stealth_mod = _process_stealth_if_needed(
                data, None, fmt="PNG", strip_all=True
            )
        else:
            assert cleaner is not None
            data, text_mod = _clean_png_text_chunks(data, cleaner)
            data, stealth_mod = _process_stealth_if_needed(
                data, cleaner, fmt="PNG", strip_all=False
            )

        if not (text_mod or stealth_mod):
            print(f"変更なし: {filepath}")
            return "unchanged"

        detail = "+".join(
            label for label, flag in (("テキスト", text_mod), ("ステルス", stealth_mod)) if flag
        )
        dest_info = f" -> {dest_path}" if dest_path != filepath else ""
        if dry_run:
            print(f"[DRY-RUN] {action_label}予定 ({detail}): {filepath}{dest_info}")
            return "updated"

        _write_atomic(dest_path, data)
        print(f"{action_label}完了 ({detail}): {filepath}{dest_info}")
        return "updated"

    if ext in (".webp", ".jpg", ".jpeg"):
        exif_mod = False
        stealth_mod = False

        with Image.open(io.BytesIO(data)) as img:
            exif_bytes = img.info.get("exif")

        if exif_bytes:
            if strip_all:
                out_buf = io.BytesIO()
                piexif.remove(data, out_buf)
                data = out_buf.getvalue()
                exif_mod = True
            else:
                assert cleaner is not None
                new_exif, exif_mod = _clean_exif_bytes(exif_bytes, cleaner)
                if exif_mod:
                    out_buf = io.BytesIO()
                    piexif.insert(new_exif, data, out_buf)
                    data = out_buf.getvalue()

        if ext == ".webp":
            data, stealth_mod = _process_stealth_if_needed(
                data, cleaner, fmt="WEBP", strip_all=strip_all
            )

        if not (exif_mod or stealth_mod):
            print(f"変更なし: {filepath}")
            return "unchanged"

        detail = "+".join(
            label for label, flag in (("Exif", exif_mod), ("ステルス", stealth_mod)) if flag
        )
        dest_info = f" -> {dest_path}" if dest_path != filepath else ""
        if dry_run:
            print(f"[DRY-RUN] {action_label}予定 ({detail}): {filepath}{dest_info}")
            return "updated"

        _write_atomic(dest_path, data)
        print(f"{action_label}完了 ({detail}): {filepath}{dest_info}")
        return "updated"

    print(f"スキップ (未対応形式): {filepath}")
    return "skipped"


def load_tags_from_file(filepath: Path) -> list[str]:
    if not filepath.is_file():
        raise FileNotFoundError(f"タグファイルが見つかりません: {filepath}")
    tags: list[str] = []
    for line in filepath.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tags.append(stripped)
    return tags


def collect_target_files(target: Path, recursive: bool) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        candidates = target.rglob("*") if recursive else target.glob("*")
        return sorted(
            p for p in candidates if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    raise FileNotFoundError(f"指定されたパスが見つかりません: {target}")


def confirm_execution(
    target: Path,
    output_dir: Path | None,
    file_count: int,
    recursive: bool,
    strip_all: bool,
    tags: list[str] | None,
) -> bool:
    if target.is_dir():
        scope = "サブディレクトリ含む" if recursive else "直下のみ"
        print(f"対象: {target} 内の画像 {file_count} 件（{scope}）")
    else:
        print(f"対象: {target}")

    if output_dir is not None:
        print(f"出力: {output_dir}（元ファイルは上書きしません）")
    else:
        print("出力: 元ファイルを上書き")

    if strip_all:
        print("処理: メタデータ（テキスト / Exif / ステルス情報）を完全消去します")
    else:
        print("処理: メタデータから指定タグを除去します")
        tag_list = ", ".join(tags) if tags else "（なし）"
        print(f"タグ: {tag_list}")

    try:
        answer = input("実行してよろしいですか？ [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    return answer in ("y", "yes")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI生成画像(PNG / WebP / JPEG)のメタデータから指定タグを除去、または完全消去します。"
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        type=Path,
        help="処理対象の画像ファイル、またはディレクトリ",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="出力先ディレクトリ（省略時は元ファイルを上書き）",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="ディレクトリ指定時にサブディレクトリも再帰的に処理する",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-t",
        "--tags",
        nargs="+",
        default=None,
        help="除去するタグをスペース区切りで指定（指定時は DEFAULT_TARGET_TAGS を無視）",
    )
    mode_group.add_argument(
        "-f",
        "--tags-file",
        type=Path,
        default=None,
        help="除去するタグを1行ずつ記述したテキストファイルを指定（#始まりはコメント行）",
    )
    mode_group.add_argument(
        "-s",
        "--strip-all",
        action="store_true",
        help="メタデータ（テキストチャンク / Exif / ステルス情報）をすべて完全消去する",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイルを書き換えずに更新対象のみ表示する",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="実行前の確認プロンプトをスキップする",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.strip_all:
        active_tags = None
    elif args.tags_file is not None:
        active_tags = load_tags_from_file(args.tags_file)
    elif args.tags is not None:
        active_tags = args.tags
    else:
        active_tags = DEFAULT_TARGET_TAGS

    cleaner = None if args.strip_all else TagCleaner(active_tags or [])
    files = collect_target_files(args.input, args.recursive)

    if not files:
        print("対象の画像ファイルが見つかりませんでした。")
        return

    if not args.dry_run and not args.yes:
        if not confirm_execution(
            target=args.input,
            output_dir=args.output,
            file_count=len(files),
            recursive=args.recursive,
            strip_all=args.strip_all,
            tags=active_tags,
        ):
            print("処理をキャンセルしました。")
            return

    counts = {"updated": 0, "unchanged": 0, "skipped": 0}
    for filepath in files:
        try:
            dest_path = _resolve_output_path(filepath, args.input, args.output)
            status = clean_image(
                filepath,
                dest_path,
                cleaner,
                dry_run=args.dry_run,
                strip_all=args.strip_all,
            )
            counts[status] += 1
        except Exception as exc:
            print(f"エラー ({filepath}): {exc}")
            counts["skipped"] += 1

    action = "完全消去" if args.strip_all else "更新"
    label = f"{action}予定" if args.dry_run else action
    print(
        f"\n完了: 全{len(files)}件 "
        f"({label}: {counts['updated']}, 変更なし: {counts['unchanged']}, スキップ/エラー: {counts['skipped']})"
    )


if __name__ == "__main__":
    main()
