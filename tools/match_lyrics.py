#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cy-music MP3 ↔ LRC 自动配对
============================

扫描歌曲目录，把散落的歌词文件自动对上号，并统一命名成 `<音频名>.lrc`。

为什么要这个脚本
----------------
播放器只认"扩展名前逐字符相同"的歌词文件。从各个歌词站下载回来的 LRC 往往长这样：

    songs/人间-王菲.mp3        ← 音频
    songs/人间 - 王菲.lrc      ← 歌词（中间多了空格）

旧逻辑 `name + '.lrc'` 直接判为无歌词，歌就哑了。本脚本把这类差异自动收敛。

配对分四层，从强到弱
--------------------
  L1  去扩展名后逐字符相同          → 已经配好，不动
  L2  规范化后相同（全半角/大小写/空格/横线/括号）→ 自动改名
  L3  读 LRC 里的 [ti:][ar:] 或首行 `歌名 - 歌手`，与文件名交叉匹配 → 自动改名
  L4  歌名相似度 >= 0.85 且歌手一致   → **只报告，不改名**（模糊匹配不配动手）
  多候选                              → 报歧义，交给人工

用法
----
    python tools/match_lyrics.py                 # 预览 → 确认后执行
    python tools/match_lyrics.py --dry-run       # 只出报告，不动文件（CI 用这个）
    python tools/match_lyrics.py --yes           # 跳过确认（自动化）
    python tools/match_lyrics.py --report out.txt   # 报告另存一份
    python tools/match_lyrics.py --collect       # songs/ 之外的音频和歌词全部收进来

`--collect` 是另一码事：它只负责**搬运**，把 songs/ 之外（含子目录）的所有
mp3/lrc 一律搬进 songs/，命名规范成 `歌名-歌手`。配对只决定歌词叫什么名字，
不决定要不要搬——配不上的孤立歌词照样搬，避免继续散落在仓库根目录。

安全策略
--------
* 配对改名阶段一律以音频文件名为准，只改歌词名，绝不改音频名。
* `--collect` 搬音频时会规范化文件名（`歌 - 手.mp3` → `歌-手.mp3`），不改内容。
* 目标已存在 → 报冲突，跳过，不覆盖。
* 目标名含 Windows 非法字符 → 跳过，不擅自清洗。
* 有动作时写 `配对日志.txt`，记录旧名→新名和配对依据。
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

AUDIO_EXTS = {".mp3"}
LYRIC_EXTS = {".lrc"}
FUZZY_THRESHOLD = 0.85
ILLEGAL_CHARS = set('\\/:*?"<>|')

LAYER_NAMES = {"L1": "同名", "L2": "规范化同名", "L3": "LRC标签", "L4": "模糊相似"}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def read_text_auto(path: Path) -> str:
    """按 utf-8-sig → utf-8 → gb18030 依次尝试。中文歌词站常见 GBK。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize(text: str) -> str:
    """折叠肉眼看不见的差异，用于 L2 比较。"""
    text = unicodedata.normalize("NFKC", text).lower()
    for pair in (("（", "("), ("）", ")"), ("【", "["), ("】", "]"),
                 ("－", "-"), ("—", "-"), ("–", "-"), ("_", "-")):
        text = text.replace(*pair)
    return re.sub(r"[\s\-]+", "", text)


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def split_stem(stem: str):
    """`歌名-歌手` 按第一个 '-' 拆（歌手名可能含 '-'，如 A-Lin）。"""
    i = stem.find("-")
    if i <= 0:
        return stem.strip(), ""
    return stem[:i].strip(), stem[i + 1:].strip()


def parse_lrc_meta(path: Path):
    """从 LRC 里猜出 (歌名, 歌手)。优先 ID 标签，退化到首行 `歌名 - 歌手`。"""
    try:
        text = read_text_auto(path)
    except OSError:
        return None, None

    title = singer = None
    for line in text.splitlines()[:80]:
        tag = re.match(r"^\[(ti|ar)\s*:\s*(.*?)\s*\]$", line.strip(), re.I)
        if tag:
            key, value = tag.group(1).lower(), tag.group(2).strip()
            if value:
                if key == "ti":
                    title = value
                else:
                    singer = value
    if title or singer:
        return title, singer

    for line in text.splitlines()[:40]:
        plain = re.sub(r"\[[^\]]*\]", "", line).strip()
        if not plain:
            continue
        for dash in (" - ", " -", "- ", "－"):
            if dash in plain:
                left, right = plain.split(dash, 1)
                return left.strip(), right.strip()
        return plain, None          # 只有一行字，拿不到歌手
    return None, None


def singers_compatible(a: str, b: str) -> bool:
    """歌手是否算同一个：完全一致，或一方是另一方的前缀/子串。"""
    if not a or not b:
        return True
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return True
    return na.startswith(nb) or nb.startswith(na) or na in nb or nb in na


# --------------------------------------------------------------------------
# 配对
# --------------------------------------------------------------------------

def collect(songs_dir: Path):
    """递归收集音频与歌词，按父目录分桶（只和同目录的歌词配对）。"""
    audios, lyrics = defaultdict(list), defaultdict(list)
    for path in sorted(songs_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(songs_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        suffix = path.suffix.lower()
        if suffix in AUDIO_EXTS:
            audios[path.parent].append(path)
        elif suffix in LYRIC_EXTS:
            lyrics[path.parent].append(path)
    return audios, lyrics


_META_CACHE = {}


def lrc_meta(path: Path):
    """parse_lrc_meta 的缓存版（一次运行内每个文件只读一遍）。"""
    if path not in _META_CACHE:
        _META_CACHE[path] = parse_lrc_meta(path)
    return _META_CACHE[path]


def score_pair(lyric: Path, audio: Path, threshold: float = FUZZY_THRESHOLD):
    """给一组（歌词, 音频）打分。返回 (rank, layer, note)，配不上返回 None。

    rank 越小越可信；L1/L2/L3 可以自动执行，L4 必须人工确认。
    """
    if audio.stem == lyric.stem:
        return 1, "L1", "同名"
    if normalize(audio.stem) == normalize(lyric.stem):
        return 2, "L2", "规范化后同名"

    lyr_title, lyr_singer = lrc_meta(lyric)
    a_title, a_singer = split_stem(audio.stem)
    if lyr_title and normalize(lyr_title) == normalize(a_title) and singers_compatible(lyr_singer, a_singer):
        return 3, "L3", f"{lyr_title}/{lyr_singer or '?'}"

    # L4 兜底：LRC 里没有标签时，用歌词文件名拆出的歌名
    fuzzy_title, fuzzy_singer = lyr_title, lyr_singer
    if not fuzzy_title:
        fuzzy_title, fuzzy_singer = split_stem(lyric.stem)
    if fuzzy_title:
        ratio = difflib.SequenceMatcher(None, normalize(fuzzy_title), normalize(a_title)).ratio()
        if ratio >= threshold and singers_compatible(fuzzy_singer, a_singer):
            return 4, "L4", f"相似度 {ratio:.2f}"
    return None


def match_one(lyric: Path, audios: list, threshold: float = FUZZY_THRESHOLD):
    """给单个歌词文件找最合适的音频。

    返回 (audio, layer, note) 或 (None, 'AMBIGUOUS'|'NONE', 说明)。
    """
    candidates = []          # (layer_rank, layer, audio, note)
    for audio in audios:
        scored = score_pair(lyric, audio, threshold)
        if scored:
            rank, layer, note = scored
            candidates.append((rank, layer, audio, note))

    if not candidates:
        return None, "NONE", "无相似音频"

    candidates.sort(key=lambda item: item[0])
    best_rank = candidates[0][0]
    best = [c for c in candidates if c[0] == best_rank]
    if len(best) > 1:
        names = "、".join(c[2].name for c in best)
        return None, "AMBIGUOUS", f"同时匹配 {names}"
    return best[0][2], best[0][1], best[0][3]


def plan(songs_dir: Path, threshold: float = FUZZY_THRESHOLD):
    """产出完整的配对方案。"""
    audios_by_dir, lyrics_by_dir = collect(songs_dir)

    renames = []        # (lyric, target_stem, layer, note)
    fuzzy = []          # (lyric, audio, note)
    ambiguous = []      # (lyric, note)
    orphans = []        # (lyric, note)
    conflicts = []      # (lyric, target_stem, 原因)
    matched = []        # (lyric, audio, layer)

    for directory, lyrics in lyrics_by_dir.items():
        audios = audios_by_dir.get(directory, [])
        for lyric in lyrics:
            audio, layer, note = match_one(lyric, audios, threshold)
            if layer == "NONE":
                orphans.append((lyric, note))
            elif layer == "AMBIGUOUS":
                ambiguous.append((lyric, note))
            elif layer == "L4":
                fuzzy.append((lyric, audio, note))
            else:
                matched.append((lyric, audio, layer))
                if lyric.stem != audio.stem:
                    # 预览阶段就暴露"目标已被占用"，别拖到执行时才失败
                    target = lyric.with_name(audio.stem + lyric.suffix.lower())
                    if target.exists():
                        conflicts.append((lyric, audio.stem, f"{target.name} 已存在，不覆盖"))
                    else:
                        renames.append((lyric, audio.stem, layer, note))

    # 孤立音频：没有任何歌词指向它
    claimed = {audio for _, audio, _ in matched}
    orphan_audios = [a for audios in audios_by_dir.values() for a in audios if a not in claimed]

    renames.sort(key=lambda item: item[0].name)
    return {
        "matched": matched,
        "renames": renames,
        "fuzzy": fuzzy,
        "ambiguous": ambiguous,
        "orphans": orphans,
        "conflicts": conflicts,
        "orphan_audios": orphan_audios,
        "audio_count": sum(len(v) for v in audios_by_dir.values()),
        "lyric_count": sum(len(v) for v in lyrics_by_dir.values()),
    }


# --------------------------------------------------------------------------
# 归位：把散落在别处的文件收回 songs/
# --------------------------------------------------------------------------

def canonical_stem(stem: str) -> str:
    """规范成 `歌名-歌手`：去掉分隔符两侧的空格。拆不出歌手就原样保留。"""
    title, singer = split_stem(stem)
    if not singer:
        return stem.strip()
    return f"{title}-{singer}"


def standalone_lyric_stem(lyric: Path, inside: list, threshold: float):
    """孤立歌词该叫什么：优先跟随 songs/ 里已存在的音频，否则用自身文件名。

    典型场景：音频已经在 songs/，歌词被丢在仓库根目录，名字还是 `歌名 - 歌手.lrc`。
    这时应该跟着音频叫，而不是自成一套。
    """
    best = None
    for audio in inside:
        result = score_pair(lyric, audio, threshold)
        if result and (best is None or result[0] < best[0]):
            best = (result[0], result[1], result[2], audio)
    if best and best[0] <= 3:                     # L1–L3 才跟随，模糊的不跟
        _, layer, _, audio = best
        return audio.stem, f"{LAYER_NAMES[layer]}，跟随 songs/ 里的 {audio.name}"
    stem = canonical_stem(lyric.stem)
    if best:
        return stem, f"与 {best[3].name} 相似度偏低，按自身名字搬，请复核"
    return stem, "无对应音频"


def plan_collect(source: Path, target_dir: Path, threshold: float = FUZZY_THRESHOLD):
    """规划归位：源目录（含子目录）里的音频和歌词，一律搬进 target_dir。

    网页上传很容易把文件扔在仓库根目录，命名也常常是 `歌名 - 歌手`（带空格）。
    这里逐个文件搬，配对只用来决定歌词该叫什么名字，不用来决定要不要搬。
    """
    target_res = target_dir.resolve()
    audios_by_dir, lyrics_by_dir = defaultdict(list), defaultdict(list)
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == target_res or target_res in resolved.parents:
            continue                                   # 跳过目标目录本身
        rel = path.relative_to(source)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in {"tools", "node_modules"}:
            continue
        suffix = path.suffix.lower()
        if suffix in AUDIO_EXTS:
            audios_by_dir[path.parent].append(path)
        elif suffix in LYRIC_EXTS:
            lyrics_by_dir[path.parent].append(path)

    inside = sorted(p for p in target_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS)

    moves = []        # (src, target_stem, kind, note) —— 逐个文件，音频歌词各自独立
    notes = []        # (src, 说明) 已归位但值得复核
    conflicts = []    # (src, target_stem, 原因)
    planned = {}      # 目标文件名 -> 源文件，防本次运行内撞名

    def add(src: Path, stem: str, kind: str, note: str) -> None:
        name = stem + src.suffix.lower()
        if not stem.strip():
            conflicts.append((src, stem, "规范化后文件名为空，未搬"))
            return
        if name in planned:
            conflicts.append((src, stem, f"与本次归位的 {planned[name].name} 同名，未搬"))
            return
        if (target_dir / name).exists():
            conflicts.append((src, stem, f"{name} 已在 songs/ 中，不覆盖"))
            return
        planned[name] = src
        moves.append((src, stem, kind, note))

    for directory in sorted(set(audios_by_dir) | set(lyrics_by_dir), key=str):
        audios = sorted(audios_by_dir.get(directory, []))
        pool = sorted(lyrics_by_dir.get(directory, []))
        used = set()

        for audio in audios:
            stem = canonical_stem(audio.stem)
            note = "无歌词"
            scored = []
            for lyric in pool:
                if lyric in used:
                    continue
                result = score_pair(lyric, audio, threshold)
                if result:
                    scored.append((result[0], result[1], result[2], lyric))
            scored.sort(key=lambda item: item[0])

            if scored:
                top_rank = scored[0][0]
                top = [item for item in scored if item[0] == top_rank]
                if len(top) > 1:
                    names = "、".join(item[3].name for item in top)
                    notes.append((audio, f"同时匹配多个歌词（{names}），歌词未跟随，按自身名字搬"))
                else:
                    _, layer, layer_note, lyric = top[0]
                    used.add(lyric)
                    add(lyric, stem, "lyric", f"{LAYER_NAMES[layer]}，跟随音频")
                    note = LAYER_NAMES[layer]
                    if layer == "L4":
                        notes.append((lyric, f"与 {audio.name} 仅{layer_note}，请复核"))
            add(audio, stem, "audio", note)

        # 本目录里剩下的歌词：音频没有、或音频在别处
        for lyric in pool:
            if lyric in used:
                continue
            stem, note = standalone_lyric_stem(lyric, inside, threshold)
            add(lyric, stem, "lyric", note)

    moves.sort(key=lambda item: (item[2], item[0].name))
    return {
        "moves": moves,
        "notes": notes,
        "conflicts": conflicts,
        "audio_count": sum(len(v) for v in audios_by_dir.values()),
        "lyric_count": sum(len(v) for v in lyrics_by_dir.values()),
    }


def apply_collect(moves: list, target_dir: Path):
    """执行归位：逐个文件搬，音频与歌词各自独立，一个失败不影响其他。"""
    done, failed = [], []
    target_dir.mkdir(parents=True, exist_ok=True)
    for src, stem, kind, note in moves:
        dst = target_dir / (stem + src.suffix.lower())
        try:
            shutil.move(str(src), str(dst))
            done.append((src, dst, kind, note))
        except OSError as exc:
            failed.append((src, dst, str(exc)))
    return done, failed


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def safe_target(lyric: Path, target_stem: str):
    """校验目标名是否可写。返回 (target_path, 错误说明)。"""
    bad = sorted(set(target_stem) & ILLEGAL_CHARS)
    if bad:
        return None, f"目标名含非法字符 {' '.join(bad)}"
    target = lyric.with_name(target_stem + lyric.suffix.lower())
    if target.exists() and target != lyric:
        return None, "目标文件已存在，不覆盖"
    return target, None


def apply_renames(renames: list):
    """执行改名，返回 (成功, 失败) 列表。"""
    done, failed = [], []
    for lyric, target_stem, layer, note in renames:
        target, error = safe_target(lyric, target_stem)
        if error:
            failed.append((lyric, target_stem, error))
            continue
        try:
            os.replace(lyric, target)
            done.append((lyric, target, layer, note))
        except OSError as exc:
            failed.append((lyric, target_stem, str(exc)))
    return done, failed


def write_log(path: Path, report: dict, done: list, failed: list) -> None:
    lines = [
        "配对日志",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"扫描: 音频 {report['audio_count']} / 歌词 {report['lyric_count']}",
        f"已改名 {len(done)} / 失败 {len(failed)}",
        "",
    ]
    if done:
        lines.append("[已改名]")
        for old, new, layer, note in done:
            lines.append(f"  {old.name}  ->  {new.name}   ({layer} {LAYER_NAMES[layer]}: {note})")
        lines.append("")
    if failed:
        lines.append("[改名失败]")
        for old, target_stem, error in failed:
            lines.append(f"  {old.name}  ->  {target_stem}.lrc   原因: {error}")
        lines.append("")
    if report["fuzzy"]:
        lines.append("[弱配，未改名，请人工确认]")
        for lyric, audio, note in report["fuzzy"]:
            lines.append(f"  {lyric.name}  ≈  {audio.name}   ({note})")
        lines.append("")
    if report["ambiguous"]:
        lines.append("[歧义，未改名]")
        for lyric, note in report["ambiguous"]:
            lines.append(f"  {lyric.name}: {note}")
        lines.append("")
    if report["conflicts"]:
        lines.append("[冲突，未改名（目标文件已存在）]")
        for lyric, stem, reason in report["conflicts"]:
            lines.append(f"  {lyric.name}  ->  {stem}.lrc   {reason}")
        lines.append("")

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def format_collect(report: dict, applied: bool) -> str:
    audio_n, lyric_n = report["audio_count"], report["lyric_count"]
    if not (audio_n or lyric_n):
        return ""

    lines = [f"归位扫描：songs/ 之外 音频 {audio_n} 个，歌词 {lyric_n} 个"]
    title = "已归位" if applied else "待归位（一律搬进 songs/）"
    sections = [
        (title, [
            f"{src.name}  ->  {stem}{src.suffix.lower()}"
            + ("   [音频] " if kind == "audio" else "   [歌词] ")
            + note
            for src, stem, kind, note in report["moves"]
        ]),
        ("冲突（songs/ 已有同名，未搬）", [
            f"{src.name}  ->  {stem}{src.suffix.lower()}   {reason}"
            for src, stem, reason in report["conflicts"]
        ]),
        ("需要复核（已归位，但配对不确定）", [
            f"{src.name}: {note}" for src, note in report["notes"]
        ]),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append("")
        lines.append(f"--- {title} ---")
        lines.extend("  " + item for item in items)
    return "\n".join(lines)


def format_report(report: dict, applied: bool) -> str:
    lines = []
    lines.append(f"扫描：音频 {report['audio_count']} 个，歌词 {report['lyric_count']} 个")

    already = len(report["matched"]) - len(report["renames"]) - len(report["conflicts"])
    lines.append(
        f"配对：同名 {already}  |  需改名 {len(report['renames'])}  |  "
        f"冲突 {len(report['conflicts'])}  |  弱配 {len(report['fuzzy'])}  |  歧义 {len(report['ambiguous'])}"
    )
    lines.append(
        f"孤立：音频 {len(report['orphan_audios'])}  |  歌词 {len(report['orphans'])}"
    )

    sections = [
        ("待改名" if not applied else "已改名", [
            f"{old.name}  ->  {stem}.lrc   ({layer} {LAYER_NAMES[layer]})"
            for old, stem, layer, _ in report["renames"]
        ]),
        ("弱配（未改名，需人工确认）", [
            f"{lyric.name}  ≈  {audio.name}   ({note})"
            for lyric, audio, note in report["fuzzy"]
        ]),
        ("歧义（未改名）", [f"{lyric.name}: {note}" for lyric, note in report["ambiguous"]]),
        ("冲突（目标名已被占用，未改名）", [
            f"{lyric.name}  ->  {stem}.lrc   {reason}"
            for lyric, stem, reason in report["conflicts"]
        ]),
        ("孤立歌词（找不到对应音频）", [f"{lyric.name}: {note}" for lyric, note in report["orphans"]]),
        ("孤立音频（没有歌词）", [audio.name for audio in report["orphan_audios"]]),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append("")
        lines.append(f"--- {title} ---")
        lines.extend("  " + item for item in items)
    return "\n".join(lines)


def ask_confirm(count: int) -> bool:
    if count == 0:
        return False
    if not sys.stdin or not sys.stdin.isatty():
        print("\n非交互环境，未执行改名。确认无误后加 --yes 重跑。")
        return False
    try:
        answer = input(f"\n将重命名 {count} 个歌词文件，是否继续？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    setup_stdout()

    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="自动识别 MP3 与 LRC 的对应关系并对齐文件名",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dir", default=str(repo_root / "songs"), help="歌曲目录（默认仓库内 songs/）")
    parser.add_argument("--collect", action="store_true",
                        help="先把散落在歌曲目录之外（如仓库根目录）的音频/歌词归位进 songs/")
    parser.add_argument("--from", dest="from_dir", default=str(repo_root),
                        help="归位时扫描的来源目录（默认仓库根）")
    parser.add_argument("--dry-run", action="store_true", help="只出报告，不改动任何文件")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过交互确认")
    parser.add_argument("--report", default=None, help="把报告另存为文本文件")
    parser.add_argument("--log", default=str(repo_root / "配对日志.txt"), help="改名日志路径")
    parser.add_argument("--fuzzy", type=float, default=FUZZY_THRESHOLD, help="模糊配对阈值，默认 0.85")
    args = parser.parse_args(argv)

    songs_dir = Path(args.dir).resolve()
    if not songs_dir.is_dir():
        print(f"找不到歌曲目录：{songs_dir}", file=sys.stderr)
        return 2

    # ---- 第一步：归位（可选）----
    collect_report = None
    collect_failed = []
    if args.collect:
        collect_report = plan_collect(Path(args.from_dir).resolve(), songs_dir, args.fuzzy)
        collect_text = format_collect(collect_report, applied=False)
        if collect_text:
            print(collect_text)

        if collect_report["moves"] and not args.dry_run:
            if args.yes or ask_confirm(len(collect_report["moves"])):
                moved, collect_failed = apply_collect(collect_report["moves"], songs_dir)
                if moved:
                    print(f"\n✅ 已归位 {len(moved)} 个文件到 {songs_dir.name}/")
                for src, target, error in collect_failed:
                    print(f"⚠️  跳过 {src.name}: {error}")
            # 归位后重新扫描，后面的配对才能看到新文件
            print()

    # ---- 第二步：歌曲目录内的配对 ----
    report = plan(songs_dir, args.fuzzy)
    text = format_report(report, applied=False)
    print(text)

    executed = False
    done, failed = [], []
    if args.dry_run:
        pass
    elif args.yes or ask_confirm(len(report["renames"])):
        done, failed = apply_renames(report["renames"])
        executed = bool(done) or bool(failed)
        if done:
            print(f"\n✅ 已改名 {len(done)} 个")
        for old, target_stem, error in failed:
            print(f"⚠️  跳过 {old.name}: {error}")
        if executed:
            write_log(Path(args.log), report, done, failed)
            print(f"📝 日志已写入 {args.log}")

    if args.report:
        chunks = []
        if collect_report is not None:
            header = format_collect(collect_report, applied=bool(collect_failed) or not args.dry_run)
            if header:
                chunks.append(header)
        chunks.append(format_report(report, applied=executed) if executed else text)
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n\n".join(chunks) + "\n")
        print(f"📝 报告已写入 {args.report}")

    # 退出码：有没解决的问题就返回 1，方便 CI 报警（用 || true 可忽略）
    unresolved = bool(report["fuzzy"] or report["ambiguous"] or report["orphans"]
                      or report["conflicts"] or failed or collect_failed)
    if collect_report is not None:
        unresolved = unresolved or bool(collect_report["conflicts"] or collect_failed)
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
