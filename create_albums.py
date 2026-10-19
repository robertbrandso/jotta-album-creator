"""Create one Jottacloud Photos album per subfolder of a folder.

Usage: python3 create_albums.py "Jotta/Sync/Pictures" [--apply] [--quiet]

Dry run by default. Only image/video files directly inside each direct
subfolder are used; deeper folders are reported and skipped. Photos are
identified by md5, which is the same checksum the file system API reports.

A failure in one folder is reported and the other folders are still processed;
the exit code is non-zero if any folder had a problem.
"""
import argparse
import fnmatch
import json
import os
import struct
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

from jotta_http import ApiError, http

# Colors only when writing to a terminal, so logs (run.sh) stay plain. See https://no-color.org
_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
BOLD, DIM, RED, GREEN, YELLOW, CYAN = "1", "2", "31", "32", "33", "36"


def paint(text, *codes):
    return f"\033[{';'.join(codes)}m{text}\033[0m" if _COLOR else text


API = "https://api.jottacloud.com"
JFS = "https://jfs.jottacloud.com/jfs/"
GRPC = "https://grpc.jotta.cloud/no.jotta.openapi.photo.v2.AlbumService/AddPhotosToAlbum"
BATCH = 50
ALBUM_PAGE_SIZE = 500  # the page size the web app uses
MAX_ALBUM_PAGES = 100


class NotFoundError(ApiError):
    """Jottacloud doesn't know one or more of the photos (grpc NOT_FOUND)."""


class ConfigError(ApiError):
    """A .jotta-album-creator file is unreadable or invalid."""


CONFIG_NAME = ".jotta-album-creator"

LIST_EXTENSIONS = "https://grpc.jotta.cloud/no.jotta.openapi.photo.v2.PhotoService/ListSupportedExtensions"
# The extensions Jottacloud Photos accepted when this was written; used if the live list can't be fetched.
FALLBACK_EXT = {"." + e for e in (
    "3fr 3gp arw avi bmp cr2 cr3 crw dcr dng erf fff heic heif jfi jfif jif jp2 jpe jpeg jpg jpx jxl kdc "
    "m2t m2ts m4v mef mos mov mp4 mrw mts nef nrw orf pef png qt raf raw rw2 rwl sr2 srf srw tif tiff x3f"
).split()}
MEDIA_EXT = set(FALLBACK_EXT)


def _read_varint(data, pos):
    shift = value = 0
    while True:
        byte = data[pos]
        value |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return value, pos
        shift += 7


def supported_extensions():
    """Ask Jottacloud Photos which file extensions it accepts (a repeated string field)."""
    resp = http("POST", LIST_EXTENSIONS, b"\x00\x00\x00\x00\x00", {
        "Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1", "Accept": "*/*"})
    extensions, pos = set(), 0
    while pos + 5 <= len(resp):
        flag, length = resp[pos], struct.unpack(">I", resp[pos + 1:pos + 5])[0]
        message = resp[pos + 5:pos + 5 + length]
        pos += 5 + length
        if flag & 0x80:  # trailer
            continue
        i = 0
        while i < len(message):
            tag, i = _read_varint(message, i)
            size, i = _read_varint(message, i)
            if tag != 0x0A:  # field 1, length-delimited
                raise ApiError("unexpected ListSupportedExtensions response")
            extensions.add("." + message[i:i + size].decode().lower())
            i += size
    if not extensions:
        raise ApiError("ListSupportedExtensions returned no extensions")
    return extensions


def list_folder(username, path):
    """Return (subfolder names, [(filename, md5)] of media files, count of other files,
    whether the folder has a CONFIG_NAME file)."""
    url = JFS + urllib.parse.quote(f"{username}/{path}")
    root = ET.fromstring(http("GET", url, headers={"Accept": "application/xml"}))
    folders = [f.get("name") for f in root.findall("./folders/folder") if f.get("deleted") is None]
    files, other, has_config = [], 0, False
    for f in root.findall("./files/file"):
        rev = f.find("currentRevision")
        if f.get("deleted") is not None or rev is None or rev.findtext("state") != "COMPLETED":
            continue
        name = f.get("name")
        if name == CONFIG_NAME:
            has_config = True
        elif "." in name and "." + name.rsplit(".", 1)[1].lower() in MEDIA_EXT:
            files.append((name, rev.findtext("md5")))
        else:
            other += 1
    return folders, files, other, has_config


def empty_rules():
    return {"include": [], "exclude": [], "merge": [], "skip": False, "skip_files": []}


def drop_skipped_files(files, rules):
    """Remove files whose name matches a 'skip <glob>' rule. Returns (kept, number dropped)."""
    if not rules["skip_files"]:
        return files, 0
    patterns = [p.lower() for p in rules["skip_files"]]
    kept = [(n, m) for n, m in files if not any(fnmatch.fnmatchcase(n.lower(), p) for p in patterns)]
    return kept, len(files) - len(kept)


def drop_root_skipped_files(files, folder, root_rules):
    """Apply 'skip <glob>' rules from the root folder's file to an album folder's files.

    A pattern containing '/' is matched against the path below the root ("Folder/File.jpg",
    "Folder/Day 1/File.jpg" for merged files); one without is matched against the file name.
    Returns (kept, number dropped).
    """
    patterns = [p.lower() for p in root_rules["skip_files"]]
    if not patterns:
        return files, 0

    def skipped(name):
        full = f"{folder}/{name}".lower()
        base = name.rsplit("/", 1)[-1].lower()
        return any(fnmatch.fnmatchcase(full if "/" in p else base, p) for p in patterns)

    kept = [(n, m) for n, m in files if not skipped(n)]
    return kept, len(files) - len(kept)


def parse_rules(text, source):
    """Parse a CONFIG_NAME file: 'include <glob>', 'exclude <glob>', 'merge <glob>', 'skip',
    'skip <file glob>'; '#' starts a comment."""
    rules = empty_rules()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        directive, _, argument = line.partition(" ")
        argument = argument.strip()
        if directive == "skip" and not argument:
            rules["skip"] = True
        elif directive == "skip":
            rules["skip_files"].append(argument)
        elif directive in ("include", "exclude", "merge") and argument:
            rules[directive].append(argument)
        else:
            raise ConfigError(f"{source}, line {number}: cannot understand {line!r}")
    return rules


def read_rules(username, path):
    """Read and parse the CONFIG_NAME file in a folder (it must exist)."""
    source = f"{path}/{CONFIG_NAME}"
    url = JFS + urllib.parse.quote(f"{username}/{source}") + "?mode=bin"
    try:
        text = http("GET", url).decode("utf-8-sig")
    except (ApiError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {source}: {e}")
    return parse_rules(text, source)


def merge_nested(username, path, nested, rules, prefix=""):
    """Collect media from the nested folders of `path` that its `rules` say to merge.

    Merging is recursive: each merged folder's own rules decide about its subfolders, and a
    merged folder with 'skip' is left out. Returns (files with names relative to the album
    folder, labels of merged folders, labels of nested folders left out).
    """
    files, merged, ignored, skipped_files = [], [], [], 0
    for sub in nested:
        label = f"{prefix}{sub}"
        if not any(fnmatch.fnmatchcase(sub.lower(), p.lower()) for p in rules["merge"]):
            ignored.append(label)
            continue
        sub_path = f"{path}/{sub}"
        sub_nested, sub_files, _, has_config = list_folder(username, sub_path)
        sub_rules = read_rules(username, sub_path) if has_config else empty_rules()
        if sub_rules["skip"]:
            ignored.append(f"{label} ('skip')")
            continue
        merged.append(label)
        sub_files, dropped = drop_skipped_files(sub_files, sub_rules)
        skipped_files += dropped
        files += [(f"{label}/{fname}", md5) for fname, md5 in sub_files]
        deeper = merge_nested(username, sub_path, sub_nested, sub_rules, f"{label}/")
        files += deeper[0]
        merged += deeper[1]
        ignored += deeper[2]
        skipped_files += deeper[3]
    return files, merged, ignored, skipped_files


def exclusion_reason(name, rules):
    """Why a subfolder is left out by its parent's rules, or None if it is selected."""
    lowered = name.lower()
    for pattern in rules["exclude"]:
        if fnmatch.fnmatchcase(lowered, pattern.lower()):
            return f"excluded by 'exclude {pattern}'"
    if rules["include"] and not any(fnmatch.fnmatchcase(lowered, p.lower()) for p in rules["include"]):
        return "not matched by any 'include'"
    return None


def list_albums():
    """All regular albums, as the dicts the API returns (id, title, total, ...)."""
    body = json.loads(http("GET", f"{API}/photos/v1/albums_by_type/NORMAL_ALBUM?comments=false",
                           headers={"Accept": "application/json"}))
    return body["result"]


def existing_albums():
    albums = {}
    for a in list_albums():
        albums.setdefault(a["title"], []).append(a["id"])
    return albums


def create_album(title):
    data = urllib.parse.urlencode({"title": title, "id_list": ""}).encode()
    body = http("POST", f"{API}/photos/v1/albums", data,
                {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    return json.loads(body)["id"]


def album_md5s(album_id):
    """Read the md5 of every photo in an album.

    The web app pages with GET /albums/{id}/{timestamp}?limit=N, where timestamp is
    that of the last photo seen; photos come in ascending timestamp order.
    """
    md5s, cursor = set(), None
    for _ in range(MAX_ALBUM_PAGES):
        path = album_id if cursor is None else f"{album_id}/{cursor}"
        body = json.loads(http("GET", f"{API}/photos/v1/albums/{path}?limit={ALBUM_PAGE_SIZE}&comments=false",
                               headers={"Accept": "application/json"}))
        photos = body.get("photos") or []
        new = {p["md5"] for p in photos if p.get("md5")} - md5s
        md5s |= new
        if len(photos) < ALBUM_PAGE_SIZE:
            return md5s
        # A full page: continue after its last photo, unless that can't make progress.
        next_cursor = photos[-1].get("timestamp")
        if not new or not next_cursor or next_cursor == cursor:
            raise ApiError(f"could not page through album {album_id}: "
                           f"got {len(md5s)} photos and no way to continue")
        cursor = next_cursor
    raise ApiError(f"album {album_id} has more than {MAX_ALBUM_PAGES * ALBUM_PAGE_SIZE} photos")


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number, value):
    raw = value.encode()
    return _varint(number << 3 | 2) + _varint(len(raw)) + raw


def grpc_call(url, msg):
    """Call a unary gRPC-web method with an encoded message.

    Raises ApiError unless grpc-status is 0 (NotFoundError for NOT_FOUND, status 5).
    The response payload is not returned, as no caller needs it.
    """
    method = url.rsplit("/", 1)[1]
    body = b"\x00" + struct.pack(">I", len(msg)) + msg
    resp, headers = http("POST", url, body, {
        "Content-Type": "application/grpc-web+proto",
        "X-Grpc-Web": "1",
        "Accept": "*/*",
    }, with_headers=True)
    # grpc-web: data frames (flag 0) followed by a trailer frame (flag 0x80) with
    # "grpc-status: N" lines. A trailers-only response puts them in the HTTP headers.
    fields = {}
    pos = 0
    while pos + 5 <= len(resp):
        flag, length = resp[pos], struct.unpack(">I", resp[pos + 1:pos + 5])[0]
        if flag & 0x80:
            for line in resp[pos + 5:pos + 5 + length].decode(errors="replace").splitlines():
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
        pos += 5 + length
    status = fields.get("grpc-status", headers.get("grpc-status"))
    if status is None:
        raise ApiError(f"{method}: response carried no grpc-status")
    if status.strip() != "0":
        message = fields.get("grpc-message", headers.get("grpc-message", ""))
        error = NotFoundError if status.strip() == "5" else ApiError  # 5 = NOT_FOUND
        raise error(f"{method} failed: grpc-status {status} {message}".strip())


def add_photos(album_id, md5s):
    """AddPhotosToAlbum: field 1 = album id, field 10 (repeated) = md5."""
    grpc_call(GRPC, _field(1, album_id) + b"".join(_field(10, m) for m in md5s))


def add_photos_skip_unknown(album_id, md5s):
    """Add photos, returning the md5s Jottacloud doesn't know about.

    A batch containing an unknown photo is rejected as a whole, so on NOT_FOUND the
    batch is split in halves until the unknown photos are isolated and the rest added.
    """
    try:
        add_photos(album_id, md5s)
        return []
    except NotFoundError:
        if len(md5s) == 1:
            return list(md5s)
        mid = len(md5s) // 2
        return (add_photos_skip_unknown(album_id, md5s[:mid])
                + add_photos_skip_unknown(album_id, md5s[mid:]))


def sync_folder(username, root, name, albums, apply, root_rules):
    """Handle one subfolder.

    Returns (output lines, notable, problem, skips): problem is a reason string or None,
    skips lists why the folder (or part of it) was not processed.
    """
    path = f"{root}/{name}"
    nested, files, other, has_config = list_folder(username, path)
    rules = read_rules(username, path) if has_config else empty_rules()
    if rules["skip"]:
        reason = f"'skip' in {CONFIG_NAME}"
        return ([f"- {paint(name, BOLD)}", paint(f"  skipped: {reason}", DIM)],
                False, None, [reason], [])
    files, skipped_files = drop_skipped_files(files, rules)
    extra, merged, ignored, merged_skipped = merge_nested(username, path, nested, rules)
    files += extra
    skipped_files += merged_skipped
    files, dropped = drop_root_skipped_files(files, name, root_rules)
    skipped_files += dropped
    lines = [f"- {paint(name, BOLD)}: {len(files)} media files"
             + (f", merged {len(merged)} nested folder(s)" if merged else "")
             + (f", {len(ignored)} nested folders skipped" if ignored else "")
             + (f", {skipped_files} file(s) skipped by rule" if skipped_files else "")
             + (f", {other} other files ignored" if other else "")]
    if merged:
        lines.append(paint(f"  merged: {', '.join(merged)}", DIM))
    notable = False  # worth printing in --quiet mode
    problem = None
    skips, warnings = [], []
    if ignored:
        skips.append(f"{len(ignored)} nested folder(s) not processed: {', '.join(ignored[:3])}"
                     + (" ..." if len(ignored) > 3 else ""))

    def warn(text):
        warnings.append(text)
        lines.append(paint(f"  warning: {text}", YELLOW))

    no_md5 = [n for n, m in files if not m]
    if no_md5:
        warn(f"{len(no_md5)} file(s) without md5 skipped (still processing?): {no_md5[:3]}")
    md5s = list(dict.fromkeys(m for _, m in files if m))  # unique, keeps order
    if not md5s:
        skips.append("no media files")
        return lines, bool(warnings), problem, skips, warnings

    ids = albums.get(name, [])
    if len(ids) > 1:
        reason = f"{len(ids)} albums already have this title"
        lines.append(paint(f"  skipped: {reason}", RED))
        skips.append(reason)
        return lines, True, reason, skips, warnings

    have = album_md5s(ids[0]) if ids else set()
    todo = [m for m in md5s if m not in have]
    action = "reuse existing album" if ids else "create album"
    summary = f"  {action}; add {len(todo)} photo(s), {len(md5s) - len(todo)} already present"
    lines.append(paint(summary, CYAN) if todo else paint(summary, DIM))
    if apply and todo:
        album_id = ids[0] if ids else create_album(name)
        unknown = []
        for i in range(0, len(todo), BATCH):
            unknown += add_photos_skip_unknown(album_id, todo[i:i + BATCH])
        if unknown:
            filenames = {}
            for fname, md5 in files:
                filenames.setdefault(md5, fname)
            shown = [filenames[m] for m in unknown[:20]]
            more = f" (+{len(unknown) - 20} more)" if len(unknown) > 20 else ""
            warn(f"{len(unknown)} file(s) not known to Jottacloud Photos, not added "
                 f"(unsupported type, or not indexed yet): {shown}{more}")
        time.sleep(1)
        missing = set(md5s) - set(unknown) - album_md5s(album_id)
        if missing:
            warn(f"{len(missing)} photo(s) not found in album afterwards "
                 f"(not yet indexed in Photos?): {sorted(missing)[:3]}")
        else:
            lines.append(paint("  done", GREEN))
    return lines, bool(warnings) or bool(todo), problem, skips, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help='path under /jfs/<user>/, e.g. "Jotta/Sync/Pictures"')
    ap.add_argument("--apply", action="store_true", help="actually create albums (default: dry run)")
    ap.add_argument("--quiet", action="store_true",
                    help="only print folders where something was (or would be) added, or that need attention")
    args = ap.parse_args()

    global MEDIA_EXT
    try:
        MEDIA_EXT = supported_extensions()
    except (ApiError, IndexError, UnicodeDecodeError) as e:
        print(paint(f"warning: could not fetch Jottacloud's supported file types ({e}); "
                    "using the built-in list", YELLOW), file=sys.stderr)

    username = json.loads(http("GET", f"{API}/account/v1/customer",
                               headers={"Accept": "application/json"}))["username"]
    root = args.folder.strip("/")
    subfolders, _, _, has_config = list_folder(username, root)
    # Fail closed: if the rules can't be read, stop rather than make albums that were meant to be skipped.
    rules = read_rules(username, root) if has_config else empty_rules()
    if rules["merge"]:
        raise ConfigError(f"{root}/{CONFIG_NAME}: 'merge' belongs in the file of an album folder, "
                          "not in the folder you run the tool on")
    if rules["skip"]:
        raise ConfigError(f"{root}/{CONFIG_NAME}: a bare 'skip' belongs in the file of an album folder; "
                          "in the folder you run the tool on, use 'skip <Folder/File.jpg>' for single files")
    albums = existing_albums()
    if not args.quiet:
        mode = paint("APPLY", BOLD, RED) if args.apply else paint("DRY RUN", BOLD, CYAN)
        print(f"{len(subfolders)} subfolders in {root!r}; {mode}\n")

    problems, skipped, warned = [], [], []  # (folder, reason)
    for name in sorted(subfolders):
        reason = exclusion_reason(name, rules)
        if reason:
            skipped.append((name, reason))
            if not args.quiet:
                print(f"- {paint(name, BOLD)}\n" + paint(f"  skipped: {reason}", DIM))
            continue
        try:
            lines, notable, problem, skips, warnings = sync_folder(username, root, name, albums, args.apply, rules)
        except ApiError as e:
            lines = [f"- {paint(name, BOLD)}", paint(f"  ERROR: {e}", RED)]
            notable, problem, skips, warnings = True, str(e), [], []
        if problem:
            problems.append((name, problem))
        skipped.extend((name, reason) for reason in skips)
        warned.extend((name, text) for text in warnings)
        if notable or not args.quiet:
            print("\n".join(lines))

    for title, entries, color in (("Warnings", warned, YELLOW),
                                  ("Skipped", [] if args.quiet else skipped, DIM),
                                  ("Problems", problems, RED)):
        if entries:
            print("\n" + paint(f"{title} ({len(entries)}):", BOLD, color))
            for name, text in entries:
                print(f"- {paint(name, BOLD)}: {paint(text, color)}")
    if problems:
        sys.exit(paint(f"{len(problems)} folder(s) had problems", RED) if sys.stderr.isatty()
                 else f"{len(problems)} folder(s) had problems")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
