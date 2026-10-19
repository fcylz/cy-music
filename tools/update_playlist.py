#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cy-music 歌单生成器
====================

扫描 songs/ 目录，一次性生成两样东西：

  1. index.json   —— 播放器读取的曲目索引（name / mp3 / lrc）
  2. 歌单.txt     —— 给人看的排版歌单（按歌手分组、全局连续编号）

设计要点
--------
* 纯标准库，无第三方依赖，Python 3.8+ 可用。
* 歌单的顺序不是算出来的，是"继承"出来的：首次运行时读取已存在的 歌单.txt
  作为顺序锚点，只把新增的歌插进去、把删掉的歌剔出来。这样每次自动更新的
  git diff 只有几行，而不是整篇重写。
* 若 歌单.txt 不存在，回退到确定性规则：组按首次出现位置，组内按歌名排序。

用法
----
    python tools/update_playlist.py                    # 生成 index.json + 歌单.txt
    python tools/update_playlist.py --check            # 只检查是否过期（CI 用，过期退出码 1）
    python tools/update_playlist.py --dry-run          # 打印结果不写盘
    python tools/update_playlist.py --no-index         # 只更新歌单
    python tools/update_playlist.py --base-url https://xxx/songs

维护须知
--------
文件名约定：`歌名-歌手.mp3`。拆分时按 **第一个** "-"，因为歌手名可能含 "-"
（如 A-Lin），而歌名实测不含。若已有歌单里能认出歌手，则优先用已知切分点兜底。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import OrderedDict
from pathlib import Path

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------

BOX_W = 46                      # 歌单框内宽度（显示宽度，非字符数）
AUDIO_EXTS = {".mp3"}           # 计入曲目的音频扩展名（小写）
OTHER_GROUP = "其他歌手"         # 单曲歌手的归并组名
UNKNOWN_SINGER = "佚名"          # 歌手名为空或被抹掉时的显示名

# 歌手名归一：键为原始写法，值为最终显示写法
SINGER_ALIASES = {
    "G.E.M.邓紫棋": "G.E.M. 邓紫棋",
}

# 文本全局替换：作用于歌名与歌手名（用于清掉版本后缀里的花名）
TEXT_REPLACEMENTS = [
    ("蒋雪儿Snow.J", "蒋雪儿"),
]

SEP = "━" * BOX_W


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def setup_stdout() -> None:
    """Windows 中文控制台默认 cp936，打印 emoji / ━ 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def display_width(text: str) -> int:
    """终端显示宽度：全角/宽字符算 2，其余算 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def clean_text(text: str) -> str:
    """歌名 / 歌手名的通用清洗。"""
    text = text.strip()
    for old, new in TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    # A&B -> A & B（仅在两侧都非空白时补空格，避免重复处理）
    text = re.sub(r"(?<=\S)&(?=\S)", " & ", text)
    return text.strip()


def clean_singer(singer: str) -> str:
    """歌手名归一化。"""
    singer = clean_text(singer)
    if singer and set(singer) <= {"_", "-"}:
        # 有些文件歌手名被抹成了下划线（如 落了白-____________）
        return UNKNOWN_SINGER
    return SINGER_ALIASES.get(singer, singer)


def split_stem(stem: str, known_singers: frozenset = frozenset()) -> tuple:
    """把 `歌名-歌手` 拆成 (歌名, 歌手)。

    按第一个 "-" 拆：实测全部文件名里歌名不含 "-"，而歌手名可能含（A-Lin、
    G.E.M.）。若已知歌手表里能命中某个切分点，优先用它，以兼容歌名含 "-" 的情况。
    """
    dashes = [i for i, ch in enumerate(stem) if ch == "-" and i > 0]
    if not dashes:
        return clean_text(stem), UNKNOWN_SINGER

    for i in dashes:
        candidate = clean_singer(stem[i + 1:])
        if candidate in known_singers:
            return clean_text(stem[:i]), candidate

    i = dashes[0]
    return clean_text(stem[:i]), clean_singer(stem[i + 1:])


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------

def scan_tracks(songs_dir: Path) -> list:
    """递归扫描音频文件，配对同名歌词。返回按文件名排序的曲目列表。"""
    if not songs_dir.is_dir():
        raise SystemExit(f"找不到歌曲目录：{songs_dir}")

    found = []
    for path in songs_dir.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(songs_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.suffix.lower() not in AUDIO_EXTS:
            continue

        lyric = find_lyric(path)
        found.append({
            "path": path,
            "rel": path.relative_to(songs_dir).as_posix(),
            "stem": path.stem,
            "lyric": lyric,
            "lyric_rel": lyric.relative_to(songs_dir).as_posix() if lyric is not None else None,
        })

    # 与旧版 index.json 的排序保持一致：按文件名（去扩展名）码点序
    found.sort(key=lambda item: item["stem"])
    return found


def find_lyric(audio: Path):
    """找配对的 .lrc（大小写不敏感）。找不到返回 None。"""
    direct = audio.with_suffix(".lrc")
    if direct.exists():
        return direct
    for sibling in audio.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() == ".lrc" and sibling.stem == audio.stem:
            return sibling
    return None


# --------------------------------------------------------------------------
# 顺序锚点：从已有歌单.txt 里读出人工调好的顺序
# --------------------------------------------------------------------------

def parse_playlist(path: Path):
    """解析已有歌单，返回 (组顺序, {组合: [歌名]}, [(歌名, 歌手)])。

    返回 None 表示文件不存在或解析不出任何组（视为首次生成）。
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    order, by_group, other = [], {}, []
    current = None
    for line in text.splitlines():
        header = re.match(r"^【\s*(.+?)\s*】\s*\d+\s*首\s*$", line)
        if header:
            current = header.group(1).strip()
            order.append(current)
            by_group.setdefault(current, [])
            continue
        item = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
        if item and current:
            shown = item.group(1)
            if current == OTHER_GROUP:
                if " — " in shown:
                    title, singer = shown.rsplit(" — ", 1)
                else:
                    title, singer = shown, UNKNOWN_SINGER
                other.append((title.strip(), singer.strip()))
            else:
                by_group[current].append(shown)

    if not order:
        return None
    return order, by_group, other


# --------------------------------------------------------------------------
# 组装分组
# --------------------------------------------------------------------------

def build_playlist(tracks: list, anchors) -> list:
    """把曲目列表组装成 [(组名, [(歌名, 歌手)], 是否其他歌手组)]。

    独立分组门槛：该歌手 >= 2 首；否则并入 "其他歌手"。
    """
    known = frozenset()
    order, by_group, other_prev = [], {}, []
    if anchors:
        order, by_group, other_prev = anchors
        known = frozenset(
            {clean_singer(name) for name in order if name != OTHER_GROUP}
            | {singer for _, singer in other_prev}
        )

    grouped = OrderedDict()
    for track in tracks:
        title, singer = split_stem(track["stem"], known)
        track["title"] = title
        track["singer"] = singer

    for track in tracks:
        grouped.setdefault(track["singer"], []).append(track)

    independent = [singer for singer in grouped if len(grouped[singer]) >= 2]

    # ---- 组顺序：锚点优先，新歌手追加 ----
    ordered = []
    all_anchor_singers = [n for n in order if n != OTHER_GROUP] + [s for _, s in other_prev]
    for singer in all_anchor_singers:
        if singer in independent and singer not in ordered:
            ordered.append(singer)
    for singer in grouped:                       # 剩余新歌手，按扫描顺序
        if singer in independent and singer not in ordered:
            ordered.append(singer)

    # ---- 组内顺序：锚点优先，新歌按歌名追加 ----
    def sort_group(singer, items):
        previous = by_group.get(singer, []) if anchors else []
        if not previous and anchors:
            # 从"其他歌手"升级上来的组：继承它在原组里的相对顺序
            previous = [title for title, owner in other_prev if owner == singer]
        rank = {title: index for index, title in enumerate(previous)}
        return sorted(items, key=lambda t: (rank.get(t["title"], len(previous)), t["title"]))

    result = []
    for singer in ordered:
        result.append((singer, [(t["title"], singer) for t in sort_group(singer, grouped[singer])], False))

    # ---- 其他歌手组 ----
    other_items = [(t["title"], singer) for singer, items in grouped.items()
                   if singer not in independent for t in items]
    prev_rank = {(title, singer): index for index, (title, singer) in enumerate(other_prev)}
    other_items.sort(key=lambda pair: (prev_rank.get(pair, len(prev_rank)), pair[1], pair[0]))

    if other_items:
        result.append((OTHER_GROUP, other_items, True))
    return result


def count_singers(groups: list) -> int:
    """涵盖的歌手数 = 独立组数 + 其他歌手中的不同歌手数。"""
    total = len([g for g in groups if not g[2]])
    other = {singer for _, items, is_other in groups if is_other for _, singer in items}
    return total + len(other)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def render_playlist(groups: list) -> str:
    total = sum(len(items) for _, items, _ in groups)
    singers = count_singers(groups)

    title = f"🎵 歌  单  (共 {total} 首)"
    title_width = display_width(title)
    left = (BOX_W - title_width) // 2 - 1        # -1 是为与历史文件逐字符对齐
    right = BOX_W - title_width - left
    if left < 0:
        left, right = 0, max(0, BOX_W - title_width)

    lines = [
        "╔" + "═" * BOX_W + "╗",
        "║" + " " * left + title + " " * right + "║",
        "╚" + "═" * BOX_W + "╝",
        "",
    ]

    index = 0
    for group_index, (name, items, is_other) in enumerate(groups):
        if group_index:
            lines.append("")
        lines.append(SEP)
        lines.append(f"【 {name} 】 {len(items)} 首")
        lines.append(SEP)
        for title_text, singer in items:
            index += 1
            line = f"{index:3d}. {title_text}"
            if is_other:
                line += f" — {singer}"
            lines.append(line)

    lines += [
        "",
        SEP,
        " " * 10 + f"总计: {total} 首  |  涵盖 {singers} 组歌手",
        SEP,
    ]
    return "\n".join(lines) + "\n"


def render_index(tracks: list, base_url: str) -> str:
    base = base_url.rstrip("/")
    entries = []
    for track in tracks:
        entry = {
            "name": track["stem"],
            "mp3": f"{base}/{track['rel']}",
        }
        if track["lyric_rel"] is not None:
            entry["lrc"] = f"{base}/{track['lyric_rel']}"
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def write_text(path: Path, content: str) -> None:
    """统一 UTF-8 + LF，避免 Windows 写出 CRLF 污染 git diff。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def main(argv=None) -> int:
    setup_stdout()

    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="扫描 songs/ 生成 index.json 与 歌单.txt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--songs-dir", default=str(repo_root / "songs"), help="歌曲目录（默认仓库内 songs/）")
    parser.add_argument("--index", default=str(repo_root / "index.json"), help="index.json 输出路径")
    parser.add_argument("--playlist", default=str(repo_root / "歌单.txt"), help="歌单.txt 输出路径")
    parser.add_argument("--base-url", default="https://fcylz.github.io/cy-music/songs",
                        help="index.json 里的音频根地址")
    parser.add_argument("--no-index", action="store_true", help="不写 index.json")
    parser.add_argument("--no-playlist", action="store_true", help="不写 歌单.txt")
    parser.add_argument("--check", action="store_true", help="只检查是否过期；过期退出码 1")
    parser.add_argument("--dry-run", action="store_true", help="不写盘，只打印摘要")
    args = parser.parse_args(argv)

    songs_dir = Path(args.songs_dir).resolve()
    index_path = Path(args.index).resolve()
    playlist_path = Path(args.playlist).resolve()

    tracks = scan_tracks(songs_dir)
    if not tracks:
        print(f"⚠️  {songs_dir} 下没有找到任何音频文件", file=sys.stderr)
        return 2

    anchors = parse_playlist(playlist_path)
    if anchors is None:
        print("ℹ️  未读到已有歌单，使用默认排序（组按首现位置、组内按歌名）")

    groups = build_playlist(tracks, anchors)
    playlist_text = render_playlist(groups)
    index_text = render_index(tracks, args.base_url)

    total = sum(len(items) for _, items, _ in groups)
    with_lyric = sum(1 for t in tracks if t["lyric"] is not None)
    print(f"🎵 {total} 首 | 有歌词 {with_lyric} 首 | 分组 {len(groups)} 个 | 歌手 {count_singers(groups)} 组")

    stale = []
    if not args.no_playlist:
        old = playlist_path.read_text(encoding="utf-8", errors="replace") if playlist_path.is_file() else None
        if old != playlist_text:
            stale.append("歌单.txt")
            if not (args.check or args.dry_run):
                write_text(playlist_path, playlist_text)
    if not args.no_index:
        old = index_path.read_text(encoding="utf-8", errors="replace") if index_path.is_file() else None
        if old != index_text:
            stale.append("index.json")
            if not (args.check or args.dry_run):
                write_text(index_path, index_text)

    if args.check:
        if stale:
            print(f"❌ 已过期：{', '.join(stale)}（重新运行本脚本即可生成）")
            return 1
        print("✅ 已是最新，无需更新")
        return 0

    if not stale:
        print("✅ 已是最新，无需更新")
    elif args.dry_run:
        print(f"（dry-run）将更新：{', '.join(stale)}")
    else:
        print(f"✅ 已更新：{', '.join(stale)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
