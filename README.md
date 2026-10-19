# jotta-album-creator

Automatically turn your Jottacloud folders into [Jottacloud](https://www.jottacloud.com) photo albums.

Jottacloud requires you to manually select individual photos from a timeline to create albums. jotta-album-creator automates this process by mirroring your existing folder structure: point it at a root directory, and every subfolder is converted into a corresponding Jottacloud album.

> [!NOTE]
> **Unofficial and unsupported.** This project is not affiliated with or endorsed by Jottacloud. Jottacloud has no documented API for albums, so the tool uses the same internal endpoints as the jottacloud.com web app. They can change or break at any time, and their terms of service may restrict automated use. Use it only against your own account, and try it on a small test folder first.

## Features

- One album per direct subfolder, titled with the folder name.
- Dry run by default; nothing is changed until you pass `--apply`.
- Idempotent: existing albums are reused and only missing photos are added, so you can re-run it as your folders grow.
- `create_albums.py` never deletes albums, photos or files.

## Requirements

Python 3.8 or newer. Photos must already be uploaded to Jottacloud and indexed in Photos (for example in a synced folder).

## Authentication

1. Create a personal login token in Jottacloud (Settings → Security).
2. Pass it to the first run in the `JOTTA_LOGIN_TOKEN` environment variable:
   ```zsh
   JOTTA_LOGIN_TOKEN="<paste token>" python3 explore_fs.py
   ```
3. That's it. The login token is **single use**: the first run exchanges it for a refresh token, which is stored in `~/.config/jotta-album-creator/token.json` (mode 600, unencrypted) and rotated on every run. Later runs, including scheduled ones, need no variable. To log out or log in again, delete that file and repeat with a new login token.

> [!TIP]
> A login token left in your shell history is worthless once it has been used. The file with the refresh token is the secret to protect. Tokens are never printed by the tool; don't paste them into issues or logs.

## Usage

Paths are relative to your Jottacloud file system root, in the form `<device>/<mountpoint>/<folder>`. The built-in device is called `Jotta`, and its mountpoints correspond to Jottacloud's features:

| Feature | Path starts with | Example |
|---|---|---|
| Sync | `Jotta/Sync` | `Jotta/Sync/Pictures` |
| Archive | `Jotta/Archive` | `Jotta/Archive/Pictures` |
| Backup | `<computer name>/<backed-up folder>` | `My MacBook/Pictures` |

Start by browsing to find your folder. Run `explore_fs.py` without a path to list your devices, then work your way down (read-only):

```zsh
python3 explore_fs.py              # devices, e.g. Jotta and any computers with Backup
python3 explore_fs.py "Jotta"      # mountpoints: Archive, Photos, Shared, Sync
python3 explore_fs.py "Jotta/Sync" # folders in Sync
```

Then run the tool on the folder whose subfolders should become albums. It is a dry run unless you pass `--apply`:

```zsh
# Sync
python3 create_albums.py "Jotta/Sync/Pictures"         # dry run
python3 create_albums.py "Jotta/Sync/Pictures" --apply # create albums

# Archive
python3 create_albums.py "Jotta/Archive/Pictures"
python3 create_albums.py "Jotta/Archive/Pictures" --apply

# Backup (a computer you back up appears as its own device; use the name that
# explore_fs.py shows for it)
python3 create_albums.py "My MacBook/Pictures"
python3 create_albums.py "My MacBook/Pictures" --apply
```


### Notes

- `--quiet` only prints folders where something was (or would be) added or that need attention.
- Only media files directly inside each subfolder are used. Nested folders are reported and skipped.
- If several albums already share a folder's title, that folder is skipped and counted as a problem.
- A failure in one folder is reported and the other folders are still processed; the exit code is non-zero if any folder had a problem.
- Which files count as photos or videos is decided by the file types Jottacloud Photos accepts, which the tool asks Jottacloud for on every run (with a built-in list as fallback). Other files, such as `.wmv`, are ignored and counted per folder. A folder with no supported files gets no album.
- If a new album is created and Jottacloud then rejects all its photos for another reason (for example, they aren't indexed yet), the empty album is left in place and filled by a later run; the tool never deletes albums.
- Album contents are read page by page (500 photos per page, following the paging the web app uses). If paging doesn't work, the tool reports an error instead of guessing.
- Known limit: folder listings are read in a single request, so very large folders may be incomplete.
- Photos not yet indexed in Photos are reported as missing after the run; run the tool again later.

## Skipping and selecting folders

Put a plain text file named `.jotta-album-creator` in a folder in Jottacloud (it syncs like any other file) to control what happens there. Lines starting with `#` are comments.

**In the folder you run the tool on**, choose which subfolders get an album:

```
# Pictures/.jotta-album-creator
exclude 2010-07*
exclude Misc
include 2015-*
include 2021-*
```

- `exclude <pattern>` and `include <pattern>` match the subfolder name. Patterns are globs (`*`, `?`, `[abc]`), case-insensitive; everything after the keyword is the pattern, so spaces are fine.
- With no `include` lines, all subfolders are used except the excluded ones. With `include` lines, only matching subfolders are used. `exclude` wins if both match.

**In a subfolder**, opt that one folder out:

```
# Pictures/2010-07 - Holiday/.jotta-album-creator
skip
```

To leave out single files instead (for example photos Jottacloud Photos doesn't accept, which would otherwise be reported as a warning on every run), give `skip` a file name pattern. The files stay where they are; they just aren't added to the album:

```
# Pictures/2010-07 - Holiday/.jotta-album-creator
skip Bilde10.jpg
skip Photo (*).jpg
```

Patterns are case-insensitive globs matched against the file name, and a folder can have several `skip` lines. A bare `skip` still skips the whole folder. In a merged nested folder, its own `.jotta-album-creator` file applies to its files.

`skip` lines also work in the file of the folder you run the tool on, where a pattern containing `/` is matched against the path below that folder, so you can list files from several albums in one place:

```
# Pictures/.jotta-album-creator
skip 2010-07 - Holiday/Photo10.jpg
skip 2015-07 - Summer 2015/Day 1/IMG_0042.jpg
```

A pattern with a `/` is matched against the whole path (`Folder/File.jpg`, or `Folder/Subfolder/File.jpg` for merged files). A pattern without `/` matches that file name in every folder. In this file a bare `skip` is an error, since it would mean skipping everything.

**Including nested folders in the parent's album.** By default only the files directly inside an album folder are used, and nested folders are skipped. To put the photos of a nested folder in the album of the folder it sits in, add `merge` lines to the `.jotta-album-creator` file of the album folder:

```
# Pictures/2015-07 - Summer 2015/.jotta-album-creator
merge Day *
merge Extra photos
```

```
Pictures/2015-07 - Summer 2015/      -> album "2015-07 - Summer 2015", with the photos from
  IMG_1.jpg                             the folder itself and from "Day 1" and "Day 2"
  Day 1/IMG_2.jpg
  Day 2/IMG_3.jpg
  Misc/IMG_4.jpg                     -> not merged (no matching 'merge' line), reported as skipped
```

- `merge <pattern>` matches the names of the folder's direct subfolders (same glob rules as `include` and `exclude`; use `merge *` for all of them).
- It is recursive: to merge a folder's own subfolders too, put `merge` lines in that folder's `.jotta-album-creator` file. A merged folder containing `skip` is left out.
- Merged folders don't get an album of their own, and `merge` is not allowed in the file of the folder you run the tool on, since those subfolders are the albums.

Skipped folders show up under "Skipped" with the reason. Existing albums for them are left alone, because the tool never deletes anything. If a rules file can't be read or understood, the tool stops (or, for a subfolder, reports a problem and skips that folder) rather than risk making albums you asked it not to.

## Removing albums

`create_albums.py` never deletes anything. To remove albums (for example to start over), use `delete_albums.py`. It deletes only the albums, never the photos or files in them. It is a dry run unless you pass `--apply`, and you must choose what to select:

```zsh
python3 delete_albums.py --all                      # list every album (dry run)
python3 delete_albums.py --title "2010*"            # only albums whose title matches a glob
python3 delete_albums.py --all --limit 1 --apply    # delete one album first, and check that your photos are intact
python3 delete_albums.py --all --apply              # delete everything; you must type the confirmation phrase
```

Deleting asks for a typed confirmation and refuses to run without a terminal, so it can't be scheduled by accident. Deleted albums can't be restored, although `create_albums.py` can recreate the ones that came from folders.

## Running on a schedule (macOS)

`run.sh` is a wrapper for unattended runs. It runs `create_albums.py --apply --quiet` for each folder you give it, one after the other under one lock so runs never overlap. It appends to `~/Library/Logs/jotta-album-creator.log` (rotated at 1 MB) and shows a macOS notification if any folder fails; a failure in one folder doesn't stop the others.

1. Log in once interactively (see Authentication), so the refresh token exists.
2. Try the wrapper by hand, with one or more folders:
   ```zsh
   ./run.sh "Jotta/Sync/Pictures" "Jotta/Archive/Old pictures"
   tail ~/Library/Logs/jotta-album-creator.log
   ```
3. Install the LaunchAgent template. Fill in `REPO_PATH` (absolute path to this repo) and edit the folders in the `ProgramArguments` list, one `<string>` per folder:
   ```zsh
   mkdir -p ~/Library/LaunchAgents
   sed "s|REPO_PATH|$PWD|" launchd/jotta-album-creator.plist.template \
       > ~/Library/LaunchAgents/local.jotta-album-creator.plist
   open -e ~/Library/LaunchAgents/local.jotta-album-creator.plist   # replace FOLDER_1 and FOLDER_2
   plutil -lint ~/Library/LaunchAgents/local.jotta-album-creator.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.jotta-album-creator.plist
   ```
4. Run it right away to check the setup: `launchctl kickstart gui/$(id -u)/local.jotta-album-creator`.

To stop it: `launchctl bootout gui/$(id -u)/local.jotta-album-creator`. The template runs daily at 03:00; edit `StartCalendarInterval` to change that.

If the refresh token stops working (you'll get a notification), delete `~/.config/jotta-album-creator/token.json`, create a new login token and run the tool once by hand.

Note that launchd agents run under your user, so the job needs the Mac to be awake or to wake later. If the machine is off for long periods, run it on an always-on machine instead (copy the token file there once).

## How it works

- Folders and files are listed through the file system API (`jfs.jottacloud.com`), which reports an md5 checksum per file.
- Albums are created with `POST api.jottacloud.com/photos/v1/albums`.
- Photos are added with the gRPC-web call `AlbumService/AddPhotosToAlbum`, where photos are identified by that same md5 checksum. The protobuf message is built by hand (field 1 = album id, field 10 = repeated md5).

The endpoints were found by watching the web app's network traffic, and the login flow follows [rclone's Jottacloud backend](https://rclone.org/jottacloud/).

## Contributing

Issues and pull requests are welcome, especially reports of API changes. When sharing network traces or logs, remove tokens, cookies and personal data first.

## License

[MIT](LICENSE)
