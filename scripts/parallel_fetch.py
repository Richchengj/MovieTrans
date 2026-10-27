"""并行分段下载大文件（针对 dl.fbaipublicfiles.com 单流限速 ~0.1MB/s 的场景）。

用法: python scripts/parallel_fetch.py <url> <dest> [并发数=12]
支持 Range 断点（.part 分片保留，重跑只补缺失分片）。只依赖标准库。
"""
from __future__ import annotations

import concurrent.futures
import sys
import time
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CHUNK = 16 * 1024 * 1024  # 每分片 16MB


def head_size(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers["Content-Length"])


def fetch_range(url: str, start: int, end: int, dest: Path, idx: int) -> None:
    part = dest.parent / f"{dest.name}.part{idx}"
    want = end - start + 1
    if part.exists() and part.stat().st_size == want:
        return
    last_err = None
    for attempt in range(8):
        try:
            have = part.stat().st_size if part.exists() else 0
            if have >= want:
                return
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Range": f"bytes={start + have}-{end}"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            if part.stat().st_size == want:
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"分片 {idx} 下载失败: {last_err}")


def main() -> int:
    url, dest = sys.argv[1], Path(sys.argv[2])
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = head_size(url)
    t0 = time.time()
    ranges = []
    for i, start in enumerate(range(0, total, CHUNK)):
        ranges.append((i, start, min(start + CHUNK, total) - 1))
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(fetch_range, url, s, e, dest, i) for i, s, e in ranges]
        done_bytes = 0
        for fut in concurrent.futures.as_completed(futs):
            fut.result()
            done_bytes += CHUNK
        # 进度按分片完成数打印
    # 校验并拼接
    sizes = []
    for i, s, e in ranges:
        p = dest.parent / f"{dest.name}.part{i}"
        assert p.stat().st_size == e - s + 1, f"分片 {i} 大小不符"
        sizes.append(p)
    with open(dest, "wb") as out:
        for p in sizes:
            out.write(p.read_bytes())
    for p in sizes:
        p.unlink()
    dt = time.time() - t0
    print(f"[ok] {dest.name}: {total/1e6:.0f} MB，{dt:.0f}s，{total/dt/1e6:.1f} MB/s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
