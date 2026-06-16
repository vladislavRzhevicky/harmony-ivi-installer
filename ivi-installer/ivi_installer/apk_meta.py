"""Read package / versionName / versionCode / app label from an APK.

The Android manifest inside an APK is in Android's binary XML format
(AXML) — not regular XML. Extracting the few fields the UI cares about
(package id, version, app label) means parsing AXML by hand. This is a
deliberately minimal, dependency-free parser. Just enough to fill the
`ApkCard` widget; not a general-purpose AXML library.

Format notes (https://justanapplication.wordpress.com/2011/09/...):
  * The file is a sequence of chunks, each starting with an 8-byte
    `ResChunk_header` (type:u16, headerSize:u16, size:u32).
  * Top-level chunk is type 0x0003 (XML) wrapping a string-pool, an
    optional resource-id map, and a sequence of XML element chunks.
  * Strings come from the global string pool; element/attribute names
    and string-typed attribute values are all string-pool indices.
  * Attribute typed values resolve via the `Res_value` struct: for
    string attributes the data field is another string-pool index,
    for int attributes it's the literal integer, for resource refs
    it's a 0xPPTTEEEE id we can't resolve without `resources.arsc`.

Label resolution:
  * Apps that hard-code `<application android:label="MyApp">` work
    out of the box (we read the literal string).
  * Apps that reference `@string/app_name` (the common case) leave us
    with a resource id we can't resolve cheaply. We expose `label=None`
    in that case; the caller falls back to a derived name.
"""
from __future__ import annotations

import logging
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ---- chunk types -----------------------------------------------------------
CHUNK_STRING_POOL = 0x0001
CHUNK_XML = 0x0003
CHUNK_XML_START_NAMESPACE = 0x0100
CHUNK_XML_END_NAMESPACE = 0x0101
CHUNK_XML_START_ELEMENT = 0x0102
CHUNK_XML_END_ELEMENT = 0x0103
CHUNK_XML_CDATA = 0x0104
CHUNK_XML_RESOURCE_MAP = 0x0180

# ---- string-pool flags -----------------------------------------------------
SP_FLAG_UTF8 = 0x100

# ---- Res_value typed-value types -------------------------------------------
TYPE_REFERENCE = 0x01    # data is a resource id (PPTTEEEE)
TYPE_STRING = 0x03       # data is a string-pool index
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12


@dataclass(frozen=True)
class ApkMeta:
    """A small subset of an APK's manifest, suitable for UI display."""
    package: str
    version_name: str
    version_code: int | None
    label: str | None  # None if it's a resource reference we can't resolve


def read_meta(path: Path) -> ApkMeta:
    """Open the APK and return its manifest metadata.

    Returns a best-effort `ApkMeta`: any field we couldn't parse is
    blank (`""` for strings, `None` for `version_code` / `label`)
    rather than raising, so the UI can still show *something* if the
    APK has a quirky manifest.
    """
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("AndroidManifest.xml") as f:
                data = f.read()
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        log.warning("apk_meta: can't read AndroidManifest.xml from %s: %s",
                    path, e)
        return ApkMeta("", "", None, None)
    try:
        return _parse(data)
    except Exception:
        # AXML parsing is fiddly — corrupt or non-standard manifests
        # shouldn't crash the UI. Fall back to empty metadata.
        log.exception("apk_meta: AXML parse failed for %s", path)
        return ApkMeta("", "", None, None)


# ---- internals -------------------------------------------------------------

def _parse(buf: bytes) -> ApkMeta:
    if len(buf) < 8:
        raise ValueError("manifest too small")
    type_, header_size, _file_size = struct.unpack_from("<HHI", buf, 0)
    if type_ != CHUNK_XML:
        raise ValueError(f"not AXML (root chunk type 0x{type_:04x})")

    # Walk the top-level children of the XML chunk.
    pos = header_size
    strings: list[str] = []

    pkg = ""
    version_name = ""
    version_code: int | None = None
    label: str | None = None
    label_found = False  # only the first <application> tag matters

    while pos + 8 <= len(buf):
        ctype, cheader, csize = struct.unpack_from("<HHI", buf, pos)
        if csize == 0 or pos + csize > len(buf):
            break
        if ctype == CHUNK_STRING_POOL:
            strings = _read_string_pool(buf, pos)
        elif ctype == CHUNK_XML_START_ELEMENT:
            tag, attrs = _read_start_element(buf, pos, cheader, strings)
            if tag == "manifest":
                p = attrs.get("package")
                if isinstance(p, str):
                    pkg = p
                vn = attrs.get("versionName")
                if isinstance(vn, str):
                    version_name = vn
                vc = attrs.get("versionCode")
                if isinstance(vc, int):
                    version_code = vc
            elif tag == "application" and not label_found:
                lbl = attrs.get("label")
                if isinstance(lbl, str) and lbl:
                    label = lbl
                label_found = True
        pos += csize

    return ApkMeta(
        package=pkg,
        version_name=version_name,
        version_code=version_code,
        label=label,
    )


def _read_string_pool(buf: bytes, off: int) -> list[str]:
    type_, header_size, chunk_size = struct.unpack_from("<HHI", buf, off)
    if type_ != CHUNK_STRING_POOL:
        raise ValueError("expected string pool")
    string_count, _style_count, flags, strings_start, _styles_start = (
        struct.unpack_from("<IIIII", buf, off + 8)
    )
    is_utf8 = bool(flags & SP_FLAG_UTF8)
    offsets_pos = off + header_size
    offsets = struct.unpack_from(
        f"<{string_count}I", buf, offsets_pos
    ) if string_count else ()
    data_start = off + strings_start
    data_end = off + chunk_size
    out: list[str] = []
    for o in offsets:
        p = data_start + o
        if p < 0 or p >= data_end:
            out.append("")
            continue
        try:
            if is_utf8:
                # UTF-8 strings: char-len varint, byte-len varint, bytes,
                # NUL terminator. Length varints can be 1 or 2 bytes.
                _char_len, p = _read_varint8(buf, p)
                byte_len, p = _read_varint8(buf, p)
                s = buf[p:p + byte_len].decode("utf-8", errors="replace")
            else:
                length, p = _read_varint16(buf, p)
                s = buf[p:p + 2 * length].decode("utf-16-le",
                                                  errors="replace")
        except (struct.error, IndexError):
            s = ""
        out.append(s)
    return out


def _read_varint8(buf: bytes, p: int) -> tuple[int, int]:
    b1 = buf[p]
    if b1 & 0x80:
        b2 = buf[p + 1]
        return ((b1 & 0x7F) << 8) | b2, p + 2
    return b1, p + 1


def _read_varint16(buf: bytes, p: int) -> tuple[int, int]:
    w1 = struct.unpack_from("<H", buf, p)[0]
    if w1 & 0x8000:
        w2 = struct.unpack_from("<H", buf, p + 2)[0]
        return ((w1 & 0x7FFF) << 16) | w2, p + 4
    return w1, p + 2


def _read_start_element(
    buf: bytes, pos: int, header_size: int, strings: list[str]
) -> tuple[str, dict[str, object]]:
    """Return ``(tag_name, attributes)`` for a START_ELEMENT chunk.

    Attributes resolve to:
      * ``str`` for string-typed values
      * ``int`` for integer-typed values
      * ``None`` for resource references (caller's choice to handle)
    """
    # ResXMLTree_node: 16 bytes total (chunk_header 8 + lineNumber 4
    # + comment 4). The attrExt struct begins right after, at
    # `pos + header_size`.
    base = pos + header_size
    (
        _ns_ref, name_ref, attr_start, attr_size, attr_count,
        _id_idx, _class_idx, _style_idx,
    ) = struct.unpack_from("<IIHHHHHH", buf, base)
    tag = strings[name_ref] if 0 <= name_ref < len(strings) else ""
    attrs: dict[str, object] = {}
    attr_pos = base + attr_start
    for _ in range(attr_count):
        if attr_pos + 20 > len(buf):
            break
        a_ns, a_name, a_raw, a_size, _a_pad, a_type, a_data = (
            struct.unpack_from("<IIIHBBI", buf, attr_pos)
        )
        attr_pos += attr_size or 20
        name = strings[a_name] if 0 <= a_name < len(strings) else ""
        if not name:
            continue
        if a_type == TYPE_STRING:
            val: object = (
                strings[a_data] if 0 <= a_data < len(strings) else ""
            )
        elif a_type == TYPE_INT_DEC or a_type == TYPE_INT_HEX:
            val = a_data
        elif a_type == TYPE_INT_BOOLEAN:
            val = bool(a_data)
        elif a_type == TYPE_REFERENCE:
            # Resource reference — can't resolve without resources.arsc.
            # Try the raw value as a fallback (some manifests still
            # include the original `@string/...` token there).
            raw = (
                strings[a_raw] if 0 <= a_raw < len(strings) else ""
            )
            val = raw or None
        else:
            val = None
        attrs[name] = val
    return tag, attrs


# ---- name + initials helpers ----------------------------------------------

# Generic fragments that don't make a useful display name on their own.
# We strip these and pick the first remaining segment — package ids
# like `com.spotify.music` resolve to "Spotify", not "Music"; the
# top-level company / brand is almost always more recognizable than a
# product or category suffix.
_PACKAGE_NOISE = frozenset({
    # TLD-style prefixes
    "com", "org", "net", "io", "co", "ai",
    # platform / vendor namespaces
    "android", "google", "huawei", "ohos", "hms", "hwid",
    "samsung", "xiaomi", "miui", "oppo", "vivo",
    # generic product nouns (when used as a suffix)
    "app", "apps", "application", "client", "lib", "sdk",
    # variant tags
    "free", "lite", "pro", "premium", "beta", "dev", "debug",
    # form-factor tags
    "mobile", "phone", "auto", "car", "tv", "wear", "watch",
})


def derive_display_name(meta: ApkMeta, fallback: str) -> str:
    """Pick a human-friendly name for the APK card.

    Order of preference:
      1. The literal `android:label` from the manifest (rare in
         production apps — most reference `@string/app_name`).
      2. The most "interesting" segment of the package id, with simple
         camelCase / snake_case → " " splitting and Title-casing.
      3. The caller's fallback (typically the file stem).
    """
    if meta.label:
        return meta.label.strip() or fallback
    if meta.package:
        candidate = _pick_package_segment(meta.package)
        if candidate:
            return _humanize(candidate)
    return fallback


def derive_initials(name: str, package: str = "") -> str:
    """Two uppercase letters for the rounded icon chip.

    Tries the display name first (split on camelCase / snake_case /
    whitespace boundaries; pick the first letter of the first one or
    two words). Falls back to the first two alphanumerics of the
    package's last segment, and finally "AP".
    """
    parts = _split_words(name)
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if len(parts) == 1 and len(parts[0]) >= 2:
        return parts[0][:2].upper()
    if package:
        squashed = "".join(c for c in package.split(".")[-1] if c.isalnum())
        if len(squashed) >= 2:
            return squashed[:2].upper()
    return "AP"


def _pick_package_segment(package: str) -> str:
    segments = [s for s in package.split(".") if s]
    interesting = [s for s in segments if s.lower() not in _PACKAGE_NOISE]
    # First non-noise segment usually beats the last — for
    # `com.spotify.music` the recognizable token is "spotify", not
    # "music"; for `org.telegram.messenger` it's "telegram", not
    # "messenger". If everything's noise (e.g. `com.app`) fall back
    # to the last raw segment so we still produce *something*.
    if interesting:
        return interesting[0]
    return segments[-1] if segments else ""


def _humanize(token: str) -> str:
    # camelCase → "camel Case", snake_case / kebab-case → spaces,
    # then title-case the result. "appmarket" stays one word and
    # title-cases to "Appmarket"; that's an acceptable degradation.
    return " ".join(w.capitalize() for w in _split_words(token))


def _split_words(token: str) -> list[str]:
    """Tokenize a string on whitespace, camelCase, snake_case, kebab-case
    and other non-alphanumerics. ``"YouTube"`` → ``["You", "Tube"]``,
    ``"input_method"`` → ``["input", "method"]``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", token)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return [p for p in re.split(r"[\s\W_]+", spaced) if p]
