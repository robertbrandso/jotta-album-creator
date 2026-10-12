"""Delete Jottacloud Photos albums. The photos and files in them are NOT deleted.

Usage:
  python3 delete_albums.py --all                       # dry run: list every album
  python3 delete_albums.py --title "2008*"             # dry run: only albums matching a glob
  python3 delete_albums.py --all --limit 1 --apply     # delete just the first album, then check
  python3 delete_albums.py --all --apply               # delete everything (asks you to confirm)

Dry run by default. You must give --title (repeatable) or --all, so a bare run can't select
everything by accident. Only regular albums are considered (not shared or automatic ones).
Deletion asks for a typed confirmation, so it refuses to run without a terminal and can't be
scheduled by mistake. Deleted albums can't be restored, but create_albums.py can recreate the
ones that came from folders.
"""
import argparse
import fnmatch
import sys
import time

from create_albums import BOLD, CYAN, DIM, GREEN, RED, _field, grpc_call, list_albums, paint
from jotta_http import ApiError

DELETE_ALBUM = "https://grpc.jotta.cloud/no.jotta.openapi.photo.v2.AlbumService/DeleteAlbum"
MAX_CONSECUTIVE_FAILURES = 5


def select_albums(albums, titles, limit):
    """Albums whose title matches any glob in `titles` (all if empty), sorted by title."""
    patterns = [t.lower() for t in titles]
    chosen = [a for a in albums
              if not patterns or any(fnmatch.fnmatchcase(a["title"].lower(), p) for p in patterns)]
    chosen.sort(key=lambda a: a["title"].lower())
    return chosen[:limit] if limit else chosen


def main():
    ap = argparse.ArgumentParser(description="Delete Jottacloud Photos albums (never the photos in them).")
    ap.add_argument("--all", action="store_true", help="select every album")
    ap.add_argument("--title", action="append", default=[], metavar="GLOB",
                    help="select albums whose title matches (case-insensitive, * and ?); repeatable")
    ap.add_argument("--limit", type=int, metavar="N", help="only the first N selected albums (sorted by title)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()
    if not args.all and not args.title:
        ap.error("give --title GLOB (repeatable) or --all")
    if args.all and args.title:
        ap.error("use either --all or --title, not both")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be at least 1")

    albums = list_albums()
    chosen = select_albums(albums, args.title, args.limit)
    print(f"{len(albums)} albums in total; {len(chosen)} selected"
          + (f" (limited to {args.limit})" if args.limit else "") + "\n")
    for a in chosen:
        print(f"- {paint(a['title'], BOLD)} " + paint(f"({a.get('total', '?')} photos)", DIM))
    if not chosen:
        return
    print("\n" + paint("The photos and files themselves are not deleted, only the albums.", CYAN))

    if not args.apply:
        print(paint("Dry run: nothing deleted. Add --apply to delete.", BOLD))
        return

    if not sys.stdin.isatty():
        sys.exit("Refusing to delete without a terminal to confirm in.")
    phrase = f"delete {len(chosen)}"
    answer = input(paint(f"\nType '{phrase}' to permanently delete {len(chosen)} album(s): ", BOLD, RED))
    if answer.strip() != phrase:
        sys.exit("Not confirmed, nothing deleted.")

    deleted, failed, streak = 0, [], 0
    for i, album in enumerate(chosen, 1):
        try:
            grpc_call(DELETE_ALBUM, _field(1, album["id"]))
            deleted += 1
            streak = 0
            print(paint(f"[{i}/{len(chosen)}] deleted {album['title']}", GREEN))
        except ApiError as e:
            failed.append((album["title"], str(e)))
            streak += 1
            print(paint(f"[{i}/{len(chosen)}] FAILED {album['title']}: {e}", RED))
            if streak >= MAX_CONSECUTIVE_FAILURES:
                print(paint(f"Stopping after {streak} failures in a row.", RED))
                break
        time.sleep(0.1)

    print(f"\nDeleted {deleted} of {len(chosen)} album(s).")
    if failed:
        print(paint(f"\nProblems ({len(failed)}):", BOLD, RED))
        for title, reason in failed:
            print(f"- {paint(title, BOLD)}: {paint(reason, RED)}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
