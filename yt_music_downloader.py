#!/usr/bin/env python3
"""
YT-M Downloader
A small GUI that downloads a YouTube / YouTube Music playlist (e.g. your
"Liked Music") to MP3 files. Built on yt-dlp + ffmpeg.

Workflow:
  1. Paste the playlist URL (your Liked Music = https://music.youtube.com/playlist?list=LM)
  2. Set how many of the most-recent songs to grab (e.g. 1000 / 2000)
  3. Click "Validate" -> it lists the tracks and confirms they're reachable
  4. Click "Download" -> only enabled after a successful validate

Requires: Python 3.8+, yt-dlp, and ffmpeg on PATH (run setup.bat once).
"""

import os
import sys
import re
import csv
import json
import queue
import shutil
import threading
import subprocess
from datetime import datetime

from urllib.parse import urlparse, parse_qs

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ----------------------------------------------------------------------------
# Config / defaults
# ----------------------------------------------------------------------------
APP_TITLE = "YT-M Downloader"
DEFAULT_URL = "https://music.youtube.com/playlist?list=LM"  # your Liked Music
BROWSER = "firefox"  # cookies are pulled from this browser's logged-in session
# (Firefox is most reliable: Edge/Chrome encrypt their cookie store, which often
#  blocks reading cookies with "Unable to get key for cookie decryption".)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTDIR = os.path.join(SCRIPT_DIR, "downloads")


def find_executable(name):
    """Return a usable path/command for an executable, or None."""
    # 1) on PATH
    p = shutil.which(name)
    if p:
        return p
    # 2) python -m yt_dlp fallback
    if name == "yt-dlp":
        try:
            subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, check=True)
            return [sys.executable, "-m", "yt_dlp"]
        except Exception:
            return None
    return None


def as_list(cmd):
    return cmd if isinstance(cmd, list) else [cmd]


def limit_args(limit):
    """0 (or less) means no limit -> grab the whole playlist."""
    return ["--playlist-end", str(limit)] if limit and limit > 0 else []


def normalize_playlist_url(url):
    """A 'watch?v=...&list=LM' URL only yields the ~100-song mix queue.
    If a list= id is present, rewrite it to the full playlist URL."""
    try:
        q = parse_qs(urlparse(url).query)
        lst = q.get("list", [None])[0]
        if lst:
            return f"https://music.youtube.com/playlist?list={lst}"
    except Exception:
        pass
    return url


def playlist_id(url):
    """Return the list= id from a URL, or '' if none."""
    try:
        return parse_qs(urlparse(url).query).get("list", [""])[0] or ""
    except Exception:
        return ""


# -----------------------------------------------------------------------------
# Reading a tracklist FILE (e.g. the .txt / .csv that Playlist Song Finder
# writes) into a batch of things to download. Each entry becomes either a
# direct YouTube URL (when the line has one) or a "ytsearch1:<query>" search.
# -----------------------------------------------------------------------------
_YT_URL = re.compile(
    r"https?://(?:www\.|music\.)?(?:youtube\.com/watch\?\S*v=[\w-]+|youtu\.be/[\w-]+)\S*",
    re.I)
# A leading timestamp like "[1:02:03] " or "01:50 " that PSF puts on each line.
_TS_PREFIX = re.compile(r"^\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*")


def _make_entry(label, url):
    """One download target: prefer a real URL, else a YouTube search."""
    label = (label or "").strip()
    url = (url or "").strip()
    if url:
        return {"label": label or url, "target": url}
    return {"label": label, "target": "ytsearch1:" + label}


def parse_tracklist_file(path):
    """Parse a Playlist-Song-Finder .txt/.csv (or any simple song list) into a
    list of {'label', 'target'} dicts. Header/blank/comment lines are skipped.
    Works with:
      - our .txt lines: "[00:00] Artist - Title  https://youtu.be/ID"
      - our .csv columns: timestamp,title,artist,source,youtube_url
      - a plain hand-written list (one "Artist - Title" per line -> searched)
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = [(h or "").lower() for h in (reader.fieldnames or [])]
            if "title" in fields or "youtube_url" in fields:
                entries = []
                for row in reader:
                    row = {(k or "").lower(): (v or "") for k, v in row.items()}
                    title = row.get("title", "").strip()
                    artist = row.get("artist", "").strip()
                    url = row.get("youtube_url", "").strip()
                    label = (f"{artist} - {title}" if artist else title).strip(" -")
                    if url or label:
                        entries.append(_make_entry(label, url))
                return entries
        # Not our CSV shape -> fall through and read it as plain text lines.

    entries = []
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith("tracklist for:") or low.startswith("generated "):
                continue
            m = _YT_URL.search(line)
            url = m.group(0) if m else ""
            label = line.replace(url, " ") if url else line
            label = _TS_PREFIX.sub("", label)
            label = re.sub(r"\s{2,}", " ", label).strip(" -–—•·|\t")
            if url or label:
                entries.append(_make_entry(label, url))
    return entries


# -----------------------------------------------------------------------------
# YouTube Music "Liked Music" (LM) needs the API, not the playlist endpoint:
# yt-dlp can only ever read the first ~100 songs of LM. ytmusicapi paginates
# the real liked list (up to YouTube's 10,000 cap).
# -----------------------------------------------------------------------------
YTM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def cookie_header_from_file(path):
    """Build a Cookie: header string from a Netscape cookies.txt file."""
    import http.cookiejar
    cj = http.cookiejar.MozillaCookieJar()
    cj.load(path, ignore_discard=True, ignore_expires=True)
    parts = [f"{c.name}={c.value}" for c in cj if "youtube" in (c.domain or "")]
    if not parts:
        raise RuntimeError("No youtube.com cookies found in that cookies.txt.")
    return "; ".join(parts)


def cookie_header_from_browser(browser):
    """Build a Cookie: header string directly from the browser's cookie store."""
    import browser_cookie3 as bc3
    fn = {"firefox": bc3.firefox, "edge": bc3.edge,
          "chrome": bc3.chrome, "brave": bc3.brave}.get(browser, bc3.firefox)
    cj = fn(domain_name="youtube.com")
    parts = [f"{c.name}={c.value}" for c in cj]
    if not parts:
        raise RuntimeError(f"No youtube.com cookies found in {browser}. "
                           "Log into YouTube Music there first.")
    return "; ".join(parts)


def build_ytmusic(cookie_str):
    """Create an authenticated YTMusic client from a raw cookie string."""
    from ytmusicapi import YTMusic
    if "__Secure-3PAPISID" not in cookie_str and "SAPISID" not in cookie_str:
        raise RuntimeError("Cookies don't include the YouTube login (SAPISID). "
                           "Make sure you're signed in.")
    headers = {
        "user-agent": YTM_UA,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.5",
        "content-type": "application/json",
        "origin": "https://music.youtube.com",
        "x-goog-authuser": "0",
        "cookie": cookie_str,
        "authorization": "SAPISIDHASH 0_0",  # recomputed per request by ytmusicapi
    }
    return YTMusic(auth=headers)


def fetch_liked_tracks(cookie_str, limit):
    """Return [(videoId, title, artists), ...] for liked songs, recent first."""
    yt = build_ytmusic(cookie_str)
    n = limit if (limit and limit > 0) else 100000
    data = yt.get_liked_songs(limit=n)
    out = []
    for t in data.get("tracks", []):
        vid = t.get("videoId")
        if not vid:
            continue
        artists = ", ".join(a.get("name", "") for a in (t.get("artists") or [])
                            if a.get("name"))
        out.append((vid, t.get("title", ""), artists))
    if limit and limit > 0:
        out = out[:limit]  # API pages in ~100s, so trim to the exact count asked
    return out


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("760x560")
        root.minsize(680, 480)

        self.log_q = queue.Queue()
        self.validated = False
        self.liked_tracks = None  # cached track list when validating the LM playlist
        self.worker = None

        self._build_ui()
        self._poll_log()
        self._check_deps()

    # --- UI ----------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="Playlist URL:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        ttk.Entry(frm, textvariable=self.url_var, width=70).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=4)

        ttk.Label(frm, text="Max songs (0 = all):").grid(row=1, column=0, sticky="w")
        self.limit_var = tk.StringVar(value="0")
        ttk.Entry(frm, textvariable=self.limit_var, width=10).grid(
            row=1, column=1, sticky="w", padx=4)

        ttk.Label(frm, text="Browser:").grid(row=1, column=2, sticky="e")
        self.browser_var = tk.StringVar(value=BROWSER)
        ttk.Combobox(frm, textvariable=self.browser_var, width=10, state="readonly",
                     values=["firefox", "edge", "chrome", "brave"]).grid(
            row=1, column=3, sticky="w", padx=4)

        ttk.Label(frm, text="Save to:").grid(row=2, column=0, sticky="w")
        self.out_var = tk.StringVar(value=DEFAULT_OUTDIR)
        ttk.Entry(frm, textvariable=self.out_var, width=58).grid(
            row=2, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(frm, text="Browse...", command=self._browse).grid(
            row=2, column=3, sticky="e")

        # Optional cookies.txt. If set, used instead of reading the browser.
        ttk.Label(frm, text="Cookies file (optional):").grid(row=3, column=0, sticky="w")
        self.cookie_var = tk.StringVar(value=self._auto_cookie_file())
        ttk.Entry(frm, textvariable=self.cookie_var, width=58).grid(
            row=3, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(frm, text="Browse...", command=self._browse_cookie).grid(
            row=3, column=3, sticky="e")

        frm.columnconfigure(1, weight=1)

        # buttons
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.validate_btn = ttk.Button(btns, text="1. Validate", command=self.validate)
        self.validate_btn.pack(side="left", padx=4)
        self.download_btn = ttk.Button(btns, text="2. Download", command=self.download,
                                       state="disabled")
        self.download_btn.pack(side="left", padx=4)
        self.file_btn = ttk.Button(btns, text="From file...",
                                   command=self.download_from_file)
        self.file_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Open folder", command=self._open_folder).pack(side="right", padx=4)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=12)

        # log box
        logfrm = ttk.Frame(self.root)
        logfrm.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = tk.Text(logfrm, wrap="word", height=18, bg="#111", fg="#ddd",
                           insertbackground="#ddd")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logfrm, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or SCRIPT_DIR)
        if d:
            self.out_var.set(d)

    def _auto_cookie_file(self):
        """Use cookies.txt sitting next to the script, if present."""
        p = os.path.join(SCRIPT_DIR, "cookies.txt")
        return p if os.path.isfile(p) else ""

    def _browse_cookie(self):
        f = filedialog.askopenfilename(
            initialdir=SCRIPT_DIR,
            title="Select cookies.txt",
            filetypes=[("Cookies/text", "*.txt"), ("All files", "*.*")])
        if f:
            self.cookie_var.set(f)

    def _cookie_args(self):
        """Prefer a cookies.txt file; otherwise read from the browser."""
        cf = self.cookie_var.get().strip()
        if cf and os.path.isfile(cf):
            return ["--cookies", cf], f"cookies file ({os.path.basename(cf)})"
        browser = self.browser_var.get().strip() or BROWSER
        return ["--cookies-from-browser", browser], f"{browser} browser"

    def _cookie_header(self):
        """Cookie string for the YouTube Music API (file preferred, else browser)."""
        cf = self.cookie_var.get().strip()
        if cf and os.path.isfile(cf):
            return cookie_header_from_file(cf), f"cookies file ({os.path.basename(cf)})"
        browser = self.browser_var.get().strip() or BROWSER
        return cookie_header_from_browser(browser), f"{browser} browser"

    def _open_folder(self):
        d = self.out_var.get()
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)  # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", d])

    # --- logging -----------------------------------------------------------
    def logln(self, text=""):
        self.log_q.put(text + "\n")

    def _poll_log(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log.insert("end", line)
                self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def set_status(self, s):
        self.status_var.set(s)

    # --- dependency check --------------------------------------------------
    def _check_deps(self):
        self.ytdlp = find_executable("yt-dlp")
        self.ffmpeg = find_executable("ffmpeg")
        if not self.ytdlp:
            self.logln("[!] yt-dlp not found. Run setup.bat (or: pip install yt-dlp).")
        else:
            self.logln("[ok] yt-dlp found.")
        if not self.ffmpeg:
            self.logln("[!] ffmpeg not found. Run setup.bat or install ffmpeg, "
                       "then add it to PATH. (Needed to make MP3s.)")
        else:
            self.logln("[ok] ffmpeg found.")

        # YouTube now needs a JS runtime to solve the download "n challenge".
        js = shutil.which("deno") or shutil.which("node") or shutil.which("phantomjs")
        if js:
            self.logln(f"[ok] JS runtime found: {os.path.basename(js)}")
        else:
            self.logln("[!] No JS runtime (deno/node) found -> downloads will fail with")
            self.logln("    'Requested format is not available'. Run setup.bat to install")
            self.logln("    Deno, then FULLY CLOSE this app and reopen it so PATH updates.")
        self.logln("")

    # --- command runner ----------------------------------------------------
    def _run_cmd(self, cmd, on_done):
        """Run a command in a background thread, streaming output to the log."""
        self._cancelled = False

        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        def target():
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                for line in self.proc.stdout:
                    if self._cancelled:
                        break
                    self.logln(line.rstrip())
                self.proc.wait()
                rc = self.proc.returncode
            except FileNotFoundError as e:
                self.logln(f"[error] {e}")
                rc = -1
            except Exception as e:
                self.logln(f"[error] {e}")
                rc = -1
            self.root.after(0, lambda: on_done(rc))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _run_func(self, func, on_done):
        """Run a Python callable in a background thread. on_done(ok, result)."""
        def target():
            try:
                result = func()
                ok, payload = True, result
            except ImportError:
                self.logln("[error] Missing libraries. Run setup.bat to install "
                           "ytmusicapi and browser-cookie3.")
                ok, payload = False, None
            except Exception as e:
                self.logln(f"[error] {e}")
                ok, payload = False, None
            self.root.after(0, lambda: on_done(ok, payload))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _busy(self, busy):
        state = "disabled" if busy else "normal"
        self.validate_btn.config(state=state)
        self.file_btn.config(state=state)
        self.download_btn.config(
            state="normal" if (not busy and self.validated) else "disabled")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    def cancel(self):
        self._cancelled = True
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.set_status("Cancelled.")
        self.logln("[cancelled]")
        self._busy(False)

    # --- validate ----------------------------------------------------------
    def validate(self):
        if not self.ytdlp:
            messagebox.showerror(APP_TITLE, "yt-dlp not found. Run setup.bat first.")
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror(APP_TITLE, "Paste a playlist URL first.")
            return
        url = normalize_playlist_url(url)  # normalized internally; box is left as-is
        try:
            limit = int(self.limit_var.get() or 0)
        except ValueError:
            messagebox.showerror(APP_TITLE, "Max songs must be a number (0 = all).")
            return

        self.validated = False
        self.liked_tracks = None
        self._busy(True)
        self.set_status("Validating...")
        self.logln(f"=== Validate @ {datetime.now():%H:%M:%S} ===")
        self.logln(f"URL: {url}")
        scope = f"first {limit}" if limit > 0 else "ALL"

        if playlist_id(url) == "LM":
            self._validate_liked(limit, scope)
        else:
            self._validate_playlist(url, limit, scope)

    def _validate_liked(self, limit, scope):
        self.logln(f"Liked Music detected -> using the YouTube Music API ({scope} songs).")
        self.logln("(yt-dlp can only read the first ~100 of the LM playlist; the API gets them all.)")
        try:
            cookie_str, src = self._cookie_header()
        except Exception as e:
            self.logln(f"\n[error] {e}")
            self.set_status("Validation failed — see log.")
            self._busy(False)
            return
        self.logln(f"Reading your login from {src}...\n")

        def done(ok, tracks):
            if ok and tracks:
                self.liked_tracks = tracks
                self.validated = True
                n = len(tracks)

                def show(idx, item):
                    title, artists = item[1], item[2]
                    self.logln(f"{idx:04d}  {title}  -  {artists}")

                if n <= 100:
                    for i, t in enumerate(tracks, 1):
                        show(i, t)
                else:
                    for i, t in enumerate(tracks[:50], 1):
                        show(i, t)
                    self.logln(f"          ...  {n - 100} more in between  ...")
                    for i, t in enumerate(tracks[-50:], n - 49):
                        show(i, t)
                self.set_status(f"Validation OK — {n} songs ready.")
                self.logln(f"\n[ok] Found {n} liked songs. Click '2. Download'.")
            else:
                self.set_status("Validation failed — see log.")
                self.logln("\n[!] Couldn't read your liked songs. Make sure you're logged")
                self.logln("    into YouTube Music in the selected browser (Firefox is")
                self.logln("    easiest), or provide a cookies.txt in the box above.")
            self._busy(False)

        self._run_func(lambda: fetch_liked_tracks(cookie_str, limit), done)

    def _validate_playlist(self, url, limit, scope):
        cookie_args, cookie_src = self._cookie_args()
        self.logln(f"Checking {scope} entries (using {cookie_src})...\n")
        cmd = as_list(self.ytdlp) + [
            "--flat-playlist",
            *limit_args(limit),
            *cookie_args,
            "--print", "%(playlist_index)s\t%(title)s\t%(id)s",
            "--no-warnings",
            url,
        ]

        def done(rc):
            if rc == 0:
                self.validated = True
                self.set_status("Validation OK — ready to download.")
                self.logln("\n[ok] Playlist reachable. Click '2. Download'.")
            else:
                self.set_status("Validation failed — see log.")
                self.logln("\n[!] Could not read the playlist. Common causes:")
                self.logln("    - Not logged in / cookies unreadable (try Firefox or a cookies.txt).")
                self.logln("    - Wrong URL, or playlist is empty/region-locked.")
            self._busy(False)

        self._run_cmd(cmd, done)

    # --- download ----------------------------------------------------------
    def _common_dl_args(self, archive):
        """yt-dlp flags shared by every download path (mp3, resumable, etc.)."""
        return [
            "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--embed-thumbnail", "--add-metadata",
            "--download-archive", archive,   # skip songs already done -> resumable
            "--ignore-errors",               # keep going if one track fails
            "--no-overwrites",
            "--newline",
            # YouTube now requires solving a JS "n challenge" to get audio.
            # This lets yt-dlp fetch its solver scripts; needs a JS runtime
            # (Deno) installed -- see setup.bat.
            "--remote-components", "ejs:github",
        ]

    def download_from_file(self):
        """Pick a tracklist file (e.g. Playlist Song Finder's .txt/.csv) and
        download every song in it: direct YouTube links when present, otherwise
        an 'Artist - Title' YouTube search."""
        if not self.ytdlp:
            messagebox.showerror(APP_TITLE, "yt-dlp not found. Run setup.bat first.")
            return

        # Default to the sibling Playlist-Song-Finder\tracklists folder if present.
        init = os.path.join(os.path.dirname(SCRIPT_DIR),
                            "Playlist-Song-Finder", "tracklists")
        if not os.path.isdir(init):
            init = SCRIPT_DIR
        path = filedialog.askopenfilename(
            initialdir=init,
            title="Choose a tracklist (.txt or .csv)",
            filetypes=[("Tracklists", "*.txt *.csv"), ("All files", "*.*")])
        if not path:
            return

        try:
            entries = parse_tracklist_file(path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Couldn't read that file:\n{e}")
            return
        if not entries:
            messagebox.showinfo(APP_TITLE, "No songs found in that file.")
            return

        outdir = self.out_var.get().strip() or DEFAULT_OUTDIR
        os.makedirs(outdir, exist_ok=True)

        self._busy(True)
        self.set_status(f"Downloading {len(entries)} songs from file...")
        self.logln(f"\n=== Download from file @ {datetime.now():%H:%M:%S} ===")
        self.logln(f"File: {path}")
        n_urls = sum(1 for e in entries if not e["target"].startswith("ytsearch"))
        self.logln(f"{len(entries)} songs -> {outdir}")
        self.logln(f"  {n_urls} have direct links; "
                   f"{len(entries) - n_urls} will be searched on YouTube.\n")
        for i, e in enumerate(entries, 1):
            tag = "" if not e["target"].startswith("ytsearch") else "  (search)"
            self.logln(f"{i:03d}  {e['label']}{tag}")
        self.logln("")

        batch = os.path.join(outdir, "._fromfile_urls.txt")
        with open(batch, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(e["target"] + "\n")

        archive = os.path.join(outdir, ".download-archive.txt")
        cookie_args, _ = self._cookie_args()
        outtmpl = os.path.join(outdir, "%(autonumber)03d - %(title)s [%(id)s].%(ext)s")
        cmd = as_list(self.ytdlp) + cookie_args + self._common_dl_args(archive) + [
            "--autonumber-start", "1",
            "-o", outtmpl,
            "--batch-file", batch,
        ]

        def done(rc):
            if rc == 0:
                self.set_status("Done.")
                self.logln("\n[ok] Finished. (Already-downloaded songs are skipped "
                           "on reruns.)")
            else:
                self.set_status("Finished with some errors -- see log.")
                self.logln("\n[!] Done, but some tracks may have failed (a search "
                           "found nothing, or a video was unavailable). Re-run to "
                           "retry the missing ones.")
            self._busy(False)

        self._run_cmd(cmd, done)

    def download(self):
        if not self.validated:
            messagebox.showinfo(APP_TITLE, "Validate first.")
            return
        url = normalize_playlist_url(self.url_var.get().strip())
        limit = int(self.limit_var.get() or 0)
        outdir = self.out_var.get().strip() or DEFAULT_OUTDIR
        os.makedirs(outdir, exist_ok=True)

        self._busy(True)
        self.set_status("Downloading...")
        self.logln(f"\n=== Download @ {datetime.now():%H:%M:%S} ===")
        self.logln(f"Saving MP3s to: {outdir}\n")

        archive = os.path.join(outdir, ".download-archive.txt")
        cookie_args, _ = self._cookie_args()
        common = self._common_dl_args(archive)

        if self.liked_tracks is not None:
            # Liked Music: download each song by video id (bypasses the 100 cap).
            batch = os.path.join(outdir, "._liked_urls.txt")
            with open(batch, "w", encoding="utf-8") as f:
                for vid, _title, _artists in self.liked_tracks:
                    f.write(f"https://music.youtube.com/watch?v={vid}\n")
            self.logln(f"Downloading {len(self.liked_tracks)} liked songs...\n")
            outtmpl = os.path.join(outdir, "%(autonumber)03d - %(title)s [%(id)s].%(ext)s")
            cmd = as_list(self.ytdlp) + cookie_args + common + [
                "--autonumber-start", "1",
                "-o", outtmpl,
                "--batch-file", batch,
            ]
        else:
            outtmpl = os.path.join(outdir, "%(playlist_index)03d - %(title)s [%(id)s].%(ext)s")
            cmd = as_list(self.ytdlp) + cookie_args + limit_args(limit) + common + [
                "-o", outtmpl,
                url,
            ]

        def done(rc):
            if rc == 0:
                self.set_status("Done.")
                self.logln("\n[ok] Finished. (Already-downloaded songs are skipped on reruns.)")
            else:
                self.set_status("Finished with some errors — see log.")
                self.logln("\n[!] Done, but some tracks may have failed. "
                           "Re-run Download to retry the missing ones.")
            self._busy(False)

        self._run_cmd(cmd, done)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
