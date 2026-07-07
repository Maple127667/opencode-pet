"""preprocess_gifs.py — key-out white backgrounds via flood-fill from corners
and re-save as transparent GIFs, overwriting the originals.

Algorithm: BFS from all 4 corners. Only background-connected white is removed;
interior white fur/eyes stay intact. Result is saved as a transparency-enabled
GIF (palette index 255 = transparent), so pet.py needs no runtime processing.

Backup: originals copied to <name>-orig.gif before overwrite.

Usage:
    python preprocess_gifs.py
"""
import os
import shutil
from collections import deque
from PIL import Image, ImageSequence

GIF_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifs")
WHITE_BG_GIFS  = ["running-left.gif", "waving.gif"]
WHITE_BG_RGB   = (252, 252, 252)
WHITE_TOL      = 20
TRANSPARENT_IDX = 255


def key_out_bg(rgba):
    """Flood-fill from the 4 corners; transparent only the connected white area."""
    px = rgba.load()
    w, h = rgba.size
    visited = bytearray(w * h)
    queue = deque()

    def is_bg(x, y):
        r, g, b, _ = px[x, y]
        return (abs(r - WHITE_BG_RGB[0]) <= WHITE_TOL
                and abs(g - WHITE_BG_RGB[1]) <= WHITE_TOL
                and abs(b - WHITE_BG_RGB[2]) <= WHITE_TOL)

    for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        idx = cy * w + cx
        if is_bg(cx, cy) and not visited[idx]:
            visited[idx] = 1
            queue.append((cx, cy))

    while queue:
        x, y = queue.popleft()
        px[x, y] = (0, 0, 0, 0)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                idx = ny * w + nx
                if not visited[idx] and is_bg(nx, ny):
                    visited[idx] = 1
                    queue.append((nx, ny))
    return rgba


def rgba_to_p(rgba):
    """RGBA -> P mode with palette index 255 reserved for transparent pixels."""
    p = rgba.convert("RGB").quantize(colors=254, method=Image.MEDIANCUT)
    px_p = p.load()
    px_a = rgba.load()
    w, h = rgba.size
    for y in range(h):
        for x in range(w):
            if px_a[x, y][3] == 0:
                px_p[x, y] = TRANSPARENT_IDX
    pal = p.getpalette()
    pal[TRANSPARENT_IDX * 3:TRANSPARENT_IDX * 3 + 3] = [0, 0, 0]
    p.putpalette(pal)
    p.info["transparency"] = TRANSPARENT_IDX
    return p


def main():
    for fname in WHITE_BG_GIFS:
        src = os.path.join(GIF_DIR, fname)
        if not os.path.exists(src):
            print(f"  [skip] {fname} not found")
            continue

        # backup original (only if backup doesn't yet exist)
        backup = src.replace(".gif", "-orig.gif")
        if not os.path.exists(backup):
            shutil.copy(src, backup)

        # read all frames into memory while file is open
        frames_p = []
        durations = []
        loop = 0
        with Image.open(src) as im:
            loop = im.info.get("loop", 0)
            for fr in ImageSequence.Iterator(im):
                durations.append(fr.info.get("duration", 100))
                rgba = fr.convert("RGBA").copy()
                rgba = key_out_bg(rgba)
                frames_p.append(rgba_to_p(rgba))

        # overwrite original with transparent version
        frames_p[0].save(
            src,
            save_all=True,
            append_images=frames_p[1:],
            loop=loop,
            duration=durations,
            disposal=2,                  # restore to background each frame
            transparency=TRANSPARENT_IDX,
        )
        print(f"  [ok] {fname}: {len(frames_p)} frames, transparent bg saved")


if __name__ == "__main__":
    main()
