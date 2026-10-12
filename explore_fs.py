"""Read-only browse of the Jottacloud file system API.

Usage: python3 explore_fs.py [path]
  path is relative to /jfs/<user>/, e.g. "Jotta/Archive" or "Jotta/Archive/Pictures".
Prints folder names, plus the first few files with their md5 checksums.
"""
import json
import sys
import urllib.parse
import xml.etree.ElementTree as ET

from jotta_http import ApiError, http

CUSTOMER_URL = "https://api.jottacloud.com/account/v1/customer"
JFS = "https://jfs.jottacloud.com/jfs/"
MAX_FILES = 5


def main():
    path = sys.argv[1].strip("/") if len(sys.argv) > 1 else ""
    username = json.loads(http("GET", CUSTOMER_URL, headers={"Accept": "application/json"}))["username"]
    url = JFS + urllib.parse.quote(f"{username}/{path}".rstrip("/"))
    root = ET.fromstring(http("GET", url, headers={"Accept": "application/xml"}))

    print("tag:", root.tag)
    for tag in ("device", "mountPoint", "folder"):
        for el in root.iter(tag):
            name = el.get("name") or (el.findtext("name"))
            if name:
                print(f"  [{tag}] {name}")
    files = list(root.iter("file"))
    print(f"files: {len(files)}")
    for f in files[:MAX_FILES]:
        rev = f.find("currentRevision")
        md5 = rev.findtext("md5") if rev is not None else None
        print(f"  {f.get('name')}  md5={md5}")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
