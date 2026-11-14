"""Download + unpack BIRD dev into config.BIRD_DIR (idempotent).

    python -m nlsql.fetch_data
"""
import shutil
import sys
import urllib.request
import zipfile

from . import config

URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip"


def _have() -> bool:
    return config.DEV_JSON.is_file() and config.DB_ROOT.is_dir()


def _download(dest) -> None:
    with urllib.request.urlopen(URL, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if done - last >= 50 << 20:
                last = done
                print(f"  downloaded {done >> 20} / {total >> 20} MB", flush=True)
    if total and done != total:
        raise RuntimeError(f"truncated download: {done} of {total} bytes")


def ensure_bird_data() -> None:
    if _have():
        print(f"BIRD data present at {config.BIRD_DIR}")
        return
    parent = config.BIRD_DIR.parent
    zpath = parent / "dev.zip"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if not zpath.is_file():
            print(f"Downloading {URL} -> {zpath}", flush=True)
            tmp = zpath.with_suffix(".zip.part")
            _download(tmp)
            tmp.rename(zpath)
        print("Unzipping dev.zip", flush=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(parent)
        inner = config.BIRD_DIR / "dev_databases.zip"
        if not config.DB_ROOT.is_dir():
            print("Unzipping dev_databases.zip", flush=True)
            with zipfile.ZipFile(inner) as z:
                z.extractall(config.BIRD_DIR)
        shutil.rmtree(parent / "__MACOSX", ignore_errors=True)
    except Exception as e:
        sys.exit(f"fetch_data failed: {type(e).__name__}: {e}\n"
                 f"Fetch {URL} manually and unpack so that {config.DEV_JSON} and {config.DB_ROOT} exist "
                 f"(or set BIRD_DIR).")
    if not _have():
        sys.exit(f"fetch_data: unpacked, but {config.DEV_JSON} / {config.DB_ROOT} still missing "
                 f"(unexpected zip layout under {parent})")
    print(f"BIRD data ready at {config.BIRD_DIR}")


if __name__ == "__main__":
    ensure_bird_data()
