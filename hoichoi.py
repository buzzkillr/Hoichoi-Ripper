import os
import re
import sys
import json
import time
import argparse
import requests
import subprocess
import asyncio
import platform
import shutil

import dl  # downloader module

from rich.console import Console
from rich.progress import Progress, BarColumn, TransferSpeedColumn, TimeElapsedColumn, TextColumn

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ─── find muxer/extractor executables ─────────────────────────────────────────────────────────
MKVMERGE_CMD = shutil.which("mkvmerge") or "mkvmerge"
# (we don't actually call ffmpeg here, but if you ever do, add the same pattern)

# — Load configuration ——————————————————————————————————————————————————————
_cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
try:
    with open(_cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
except FileNotFoundError:
    console.print(f"[red]Error: Config file not found at {_cfg_path}[/]")
    sys.exit(1)
except json.JSONDecodeError:
    console.print(f"[red]Error: Invalid JSON in config file at {_cfg_path}[/]")
    sys.exit(1)

CONTENT_API_URL   = cfg["CONTENT_API_URL"]
VIDEO_API_URL     = cfg["VIDEO_API_URL"]
SITEID_HEADER     = cfg["SITEID_HEADER"]
DEFAULT_TAG       = cfg["DEFAULT_TAG"]
LANG_SUB          = cfg["LANG_SUB"]
LANG_AUD          = cfg["LANG_AUD"]
MOVIE_TMPL        = cfg["NAMING"]["movie"]
SERIES_FOLDER_TPL = cfg["NAMING"]["series_folder"]
SERIES_FILE_TPL   = cfg["NAMING"]["series_file"]
# ——————————————————————————————————————————————————————————————————————————————

console = Console()

# —── Banner —────────────────────────────────────────────────────────────────────────────────
console.print(r"""
██╗  ██╗ ██████╗ ██╗ ██████╗██╗  ██╗ ██████╗ ██╗      ██████╗ ██╗██████╗ ██████╗ ███████╗██████╗ 
██║  ██║██╔═══██╗██║██╔════╝██║  ██║██╔═══██╗██║      ██╔══██╗██║██╔══██╗██╔══██╗██╔════╝██╔══██╗
███████║██║   ██║██║██║     ███████║██║   ██║██║█████╗██████╔╝██║██████╔╝██████╔╝█████╗  ██████╔╝
██╔══██║██║   ██║██║██║     ██╔══██║██║   ██║██║╚════╝██╔══██╗██║██╔═══╝ ██╔═══╝ ██╔══╝  ██╔══██╗
██║  ██║╚██████╔╝██║╚██████╗██║  ██║╚██████╔╝██║      ██║  ██║██║██║     ██║     ███████╗██║  ██║
╚═╝  ╚═╝ ╚═════╝ ╚═╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝      ╚═╝  ╚═╝╚═╝╚═╝     ╚═╝     ╚══════╝╚═╝  ╚═╝
                                                                                                 
""", style="bold red")
console.print("Hoichoi Downloader\n", style="bold cyan")

TITLE_RE = re.compile(
    r'^(?:https?://(?:www\.)?hoichoi\.tv)?'
    r'(?P<id>/(?:movies|films|shows|webseries)/[a-z0-9\-/]+)',
    re.IGNORECASE
)

def extract_path(url: str) -> str:
    m = TITLE_RE.match(url)
    if m:
        return m.group("id")
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query).get("permalink")
    if q:
        return q[0]
    raise ValueError("Invalid Hoichoi URL")

def fetch_page_metadata(path: str) -> dict:
    resp = requests.get("https://hoichoi.tv" + path, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    pushes = re.findall(r'self\.__next_f\.push\(\[1,"([\s\S]*?)"\]\)', resp.text)
    blob = max(pushes, key=len).split(":", 1)[1]
    details = json.loads(blob.encode("utf-8").decode("unicode_escape"))[3]["detailsData"]
    return {
        "title": details.get("title", ""),
        "contentType": details.get("contentType", ""),
        "contentId": details.get("contentId", ""),
        "releaseYear": details.get("releaseYear", ""),
        "tentativeReleaseDate": details.get("tentativeReleaseDate","")
    }

def fetch_manifest(cid: str) -> str:
    resp = requests.get(VIDEO_API_URL, params={"platform": "ROKU", "language": "english", "contentIds": cid}, headers=SITEID_HEADER, timeout=10)
    data = resp.json()
    if isinstance(data, list):
        data = data[0]
    rend = data.get("renditions", [])
    if rend:
        return rend[0]["mainManifestUrl"].replace("hoichoicdn.com", "vhoichoi.viewlift.com")
    return ""

def fetch_captions(cid: str) -> list:
    resp = requests.get(VIDEO_API_URL, params={"platform": "LG", "language": "english", "contentIds": cid}, headers=SITEID_HEADER, timeout=10)
    arr = resp.json()
    if isinstance(arr, list):
        arr = arr[0]
    return arr.get("closedCaptions", [])

def fetch_audio_languages(cid: str) -> list:
    resp = requests.get(VIDEO_API_URL, params={"platform": "LG", "language": "english", "contentIds": cid}, headers=SITEID_HEADER, timeout=10)
    arr = resp.json()
    if isinstance(arr, list):
        arr = arr[0]
    return arr.get("audioLanguages", [])

def sanitize(s: str) -> str:
    return re.sub(r'[\\/*?:"<>|,]', "", s).strip().replace(" ", ".")

def fetch_series_data(series_id: str) -> list:
    """Fetch all episodes for each season of a series."""
    resp = requests.get(CONTENT_API_URL, params={"platform": "LG", "language": "english", "contentIds": series_id}, headers=SITEID_HEADER, timeout=10)
    arr = resp.json()
    if not arr or "seasons" not in arr[0]:
        return []
    seasons = []
    for season in arr[0]["seasons"]:
        eps = []
        for ep in season.get("episodes", []):
            cid = ep.get("contentId", "")
            eps.append({
                "title": ep.get("title", ""),
                "contentId": cid,
                "manifest": fetch_manifest(cid)
            })
        seasons.append({"episodes": eps})
    return seasons

def progress(mp4_in: str, mkv_out: str, audio_lang=None, srt_path=None, srt_lang=None):
    """Display muxing progress using rich progress bar."""
    total = os.path.getsize(mp4_in)
    cmd = [MKVMERGE_CMD, "-o", mkv_out]
    if audio_lang:
        cmd += ["--language", f"1:{audio_lang}"]
    cmd.append(mp4_in)
    if srt_path:
        cmd += ["--language", f"0:{srt_lang}", srt_path]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with Progress(
        TextColumn("[bold green]Muxing[/]"), BarColumn(), TransferSpeedColumn(), TimeElapsedColumn(), console=console
    ) as prog:
        task = prog.add_task("mux", total=total)
        while proc.poll() is None:
            if os.path.exists(mkv_out):
                prog.update(task, completed=os.path.getsize(mkv_out))
            time.sleep(0.1)
        prog.update(task, completed=os.path.getsize(mkv_out))
    out, _ = proc.communicate()
    if proc.returncode != 0:
        console.print(f"[bold red]mkvmerge failed (exit {proc.returncode})[/]")
        console.print(out)
        sys.exit(1)

def download_and_mux(manifest_url: str, out_dir: str, context: dict, captions: list, audio_langs: list, maxc: int, mp4c: int):
    """Download stream and mux subtitles/audio into MKV."""
    start = time.time()
    os.makedirs(out_dir, exist_ok=True)
    dl.main.source_url = manifest_url
    dl.main.output_dir = out_dir
    dl.main.max_connections = maxc
    dl.main.mp4_connections = mp4c
    dl.main.output_name = "temp_vid"
    asyncio.run(dl.main())

    mp4_in = os.path.join(out_dir, "temp_vid.mp4")
    if not os.path.exists(mp4_in):
        console.print(f"[red]Downloaded file missing: {mp4_in}[/]")
        sys.exit(1)

    # Subtitle download
    srt_path = None; srt_lang = None
    for cap in captions:
        if cap.get("srtFile"):
            try:
                raw = cap["language"].lower()
                srt_lang = LANG_SUB.get(raw, raw[:3])
                srt_path = os.path.join(out_dir, f"temp_sub.{srt_lang}.srt")
                r = requests.get(cap["srtFile"], stream=True, timeout=10)
                with open(srt_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                break
            except:
                continue

    # Audio language selection
    audio_lang = None
    if audio_langs:
        raw = audio_langs[0].lower()
        audio_lang = LANG_AUD.get(raw, raw)

    # Final output name
    raw_q = getattr(dl.main, "selected_quality", "1080p")
    quality = f"{raw_q.split('x')[1]}p" if "x" in raw_q else raw_q
    tpl = MOVIE_TMPL if context["type"] == "movie" else SERIES_FILE_TPL
    final_name = tpl.format(**{**context, "quality": quality})
    mkv_out = os.path.join(out_dir, final_name)

    progress(mp4_in, mkv_out, audio_lang, srt_path, srt_lang)

    # Cleanup
    os.remove(mp4_in)
    if srt_path and os.path.exists(srt_path):
        os.remove(srt_path)

    console.print(f"[yellow] ✓ Muxed:[/] {final_name}")
    console.print(f"[bold]Completed in {time.time() - start:.1f}s\n")

def main():
    parser = argparse.ArgumentParser(description="Hoichoi metadata + downloader")
    parser.add_argument("url", help="Hoichoi movie or series URL")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--raw", action="store_true", help="Try to fetch RAW MP4 if available")
    parser.add_argument("-o", "--output-dir", default="downloads")
    parser.add_argument("-c", "--max-connections", type=int, default=dl.DEFAULT_MAX_CONNECTIONS)
    parser.add_argument("--mp4-connections", type=int, default=dl.DEFAULT_MP4_CONNECTIONS)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    args = parser.parse_args()

    try:
        path = extract_path(args.url)
        meta = fetch_page_metadata(path)
    except Exception as e:
        console.print(f"[red]Error:[/] {e}")
        sys.exit(1)

    title  = meta["title"]
    ctype  = meta["contentType"].lower()
    cid    = meta["contentId"]
    year   = meta["releaseYear"]
    safe   = sanitize(title)

    console.print(f"[bold]Title       :[/] {title}")
    console.print(f"[bold]ContentType :[/] {ctype}")
    console.print(f"[bold]ContentId   :[/] {cid}")
    console.print(f"[bold]ReleaseYear :[/] {year}\n")

    # Process Series
    if args.download and ctype == "series":
        try:
            seasons = fetch_series_data(cid)
        except Exception as e:
            console.print(f"[red]Error fetching series data: {e}[/]")
            sys.exit(1)

        console.print("Available seasons:")
        for i in range(1, len(seasons) + 1):
            console.print(f"  {i}. Season {i:02d}")

        raw = input("Select seasons (e.g. 1,3 or all): ").strip().lower()
        sel_seasons = list(range(1, len(seasons)+1)) if raw in ("all", "a") else [int(x) for x in re.split(r"[,\s]+", raw) if x.isdigit()]

        for si in sel_seasons:
            eps = seasons[si-1]["episodes"]
            console.print(f"\n[bold]Season {si:02d}[/]")
            for j, ep in enumerate(eps, start=1):
                console.print(f"  {j}. {ep['title']}")

            raw_ep = input(f"Select episodes for S{si:02d} (e.g. 1-3 or all): ").strip().lower()
            sel_eps = list(range(1, len(eps)+1)) if raw_ep in ("all", "a") else []
            for part in raw_ep.split(","):
                if "-" in part:
                    lo, hi = part.split("-", 1)
                    sel_eps += range(int(lo), int(hi)+1)
                elif part.isdigit():
                    sel_eps.append(int(part))

            folder = SERIES_FOLDER_TPL.format(title=safe, season=si, tag=args.tag)
            season_dir = os.path.join(args.output_dir, folder)
            os.makedirs(season_dir, exist_ok=True)

            for ei in sel_eps:
                ep = eps[ei-1]
                base = {
                    "type": "series", "title": safe, "season": si, "episode": ei,
                    "episode_title": sanitize(ep["title"]), "year": year, "tag": args.tag
                }

                try:
                    caps = fetch_captions(ep["contentId"])
                    auds = fetch_audio_languages(ep["contentId"])
                except Exception as e:
                    console.print(f"[red]Failed fetching audio/subs: {e}[/]")
                    continue

                # Use RAW if requested
                if args.raw:
                    raw_match = re.search(r"/Renditions/(\d{8})/", ep["manifest"])
                    if raw_match:
                        ymd = raw_match.group(1)
                        stem = os.path.basename(ep["manifest"]).rsplit(".", 1)[0]
                        raw_url = f"https://vhoichoi.viewlift.com/MezzFiles/{ymd[:4]}/{ymd[4:6]}/{stem}.mp4"
                        console.print("[bold]Checking for RAW file…[/]")
                        try:
                            head = requests.head(raw_url, timeout=10)
                            if head.status_code == 200:
                                console.print("[green]RAW file found! Proceeding with download...[/]\n")
                                dl.main.source_url = raw_url
                                dl.main.output_dir = season_dir
                                dl.main.output_name = f"S{si:02d}E{ei:02d}"
                                dl.main.max_connections = args.max_connections
                                dl.main.mp4_connections = args.mp4_connections
                                asyncio.run(dl.main())

                                mp4_in = os.path.join(season_dir, f"S{si:02d}E{ei:02d}.mp4")
                                srt_path, srt_lang = None, None
                                for cap in caps:
                                    if cap.get("srtFile"):
                                        raw_l = cap["language"].lower()
                                        srt_lang = LANG_SUB.get(raw_l, raw_l[:3])
                                        srt_path = os.path.join(season_dir, f"temp_sub.{srt_lang}.srt")
                                        r = requests.get(cap["srtFile"], stream=True, timeout=10)
                                        with open(srt_path, "wb") as f:
                                            for chunk in r.iter_content(8192): f.write(chunk)
                                        break
                                audio_lang = LANG_AUD.get(auds[0].lower(), auds[0].lower()) if auds else None
                                quality = "RAW"
                                final_name = SERIES_FILE_TPL.format(**{**base, "quality": quality})
                                mkv_out = os.path.join(season_dir, final_name)
                                progress(mp4_in, mkv_out, audio_lang, srt_path, srt_lang)
                                try:
                                    os.remove(mp4_in)
                                    if srt_path: os.remove(srt_path)
                                except OSError as e:
                                    console.print(f"[yellow]Warning: Could not clean up temporary files: {e}[/]")
                                console.print(f"[yellow] ✓ Muxed RAW File:[/] {final_name}\n")
                                continue
                            else:
                                console.print(f"[red]RAW not available (HTTP {head.status_code}), falling back…[/]\n")
                        except requests.RequestException as e:
                            console.print(f"[red]Error checking RAW file: {e}, falling back…[/]\n")
                    else:
                        console.print("[red]Cannot derive RAW URL, falling back…[/]\n")

                # Standard HLS path
                try:
                    download_and_mux(ep["manifest"], season_dir, base, caps, auds, args.max_connections, args.mp4_connections)
                except Exception as e:
                    console.print(f"[red]Failed: {e}[/]")

    elif args.download:  # Movie
        manifest = fetch_manifest(cid)
        # ——— If there's no streaming manifest yet, bail out with the Coming Soon date ——————————————————
        if not manifest:
            coming = meta.get("tentativeReleaseDate", "TBA")
            console.print(f"[bold yellow]Streaming From: {coming}[/]")
            sys.exit(0)
        # ——— RAW support for movie —————————————————————————————————————————————————————   
        if args.raw:
            m = re.search(r"/Renditions/(\d{8})/", manifest)
            if m:
                ymd = m.group(1)
                stem = os.path.basename(manifest).rsplit(".", 1)[0]
                raw_url = f"https://vhoichoi.viewlift.com/MezzFiles/{ymd[:4]}/{ymd[4:6]}/{stem}.mp4"
                console.print("[bold]Checking for RAW file…[/]")
                try:
                    head = requests.head(raw_url, timeout=10)
                    if head.status_code == 200:
                        console.print("[green]RAW file found! Proceeding with download...[/]\n")
                        dl.main.source_url = raw_url
                        dl.main.output_dir = args.output_dir
                        dl.main.output_name = safe
                        dl.main.max_connections = args.max_connections
                        dl.main.mp4_connections = args.mp4_connections
                        asyncio.run(dl.main())

                        mp4_in = os.path.join(args.output_dir, f"{safe}.mp4")
                        caps = fetch_captions(cid)
                        srt_path, srt_lang = None, None
                        for cap in caps:
                            if cap.get("srtFile"):
                                raw_l = cap["language"].lower()
                                srt_lang = LANG_SUB.get(raw_l, raw_l[:3])
                                srt_path = os.path.join(args.output_dir, f"temp_sub.{srt_lang}.srt")
                                r = requests.get(cap["srtFile"], stream=True, timeout=10)
                                with open(srt_path, "wb") as f:
                                    for chunk in r.iter_content(8192): f.write(chunk)
                                break
                        auds = fetch_audio_languages(cid)
                        audio_lang = LANG_AUD.get(auds[0].lower(), auds[0].lower()) if auds else None
                        quality = "RAW"
                        tpl_ctx = {"type": "movie", "title": safe, "year": year, "quality": quality, "tag": args.tag}
                        final_name = MOVIE_TMPL.format(**tpl_ctx)
                        mkv_out = os.path.join(args.output_dir, final_name)
                        progress(mp4_in, mkv_out, audio_lang, srt_path, srt_lang)
                        try:
                            os.remove(mp4_in)
                            if srt_path: os.remove(srt_path)
                        except OSError as e:
                            console.print(f"[yellow]Warning: Could not clean up temporary files: {e}[/]")
                        console.print(f"[yellow] ✓ Muxed RAW File:[/] {final_name}\n")
                        return
                    else:
                        console.print(f"[red]RAW not available (HTTP {head.status_code}), falling back…[/]\n")
                except requests.RequestException as e:
                    console.print(f"[red]Error checking RAW file: {e}, falling back…[/]\n")

        caps = fetch_captions(cid)
        auds = fetch_audio_languages(cid)
        download_and_mux(manifest, args.output_dir, {"type": "movie", "title": safe, "year": year, "tag": args.tag}, caps, auds, args.max_connections, args.mp4_connections)

if __name__ == "__main__":
    main()
