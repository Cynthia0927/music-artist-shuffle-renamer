"""按歌手交叉排列歌曲，并用编号前缀批量重命名。"""

from __future__ import annotations

import csv
import os
import random
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".wma",
    ".ape",
}

# 本工具生成的前缀。再次运行时会先去掉它，避免变成 0001__0234__歌名。
PREFIX_RE = re.compile(r"^\d{4,8}__")
ARTIST_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")
COLLABORATOR_RE = re.compile(
    r"\s*(?:,|，|、|/|＆|&|\+|＋|\bfeat\.?\b|\bft\.?\b)\s*",
    re.IGNORECASE,
)
LOG_PREFIX = "_歌曲重命名记录_"


@dataclass(frozen=True)
class Song:
    path: Path
    clean_name: str
    artist_key: str
    artist_display: str
    artist_detected: bool


def strip_tool_prefix(filename: str) -> str:
    """只移除本工具生成的“数字+双下划线”前缀。"""
    return PREFIX_RE.sub("", filename, count=1)


def detect_artist(filename: str) -> tuple[str, str, bool]:
    """从“歌手 - 歌名”中读取第一位歌手。识别失败时把每首歌单独成组。"""
    clean_name = strip_tool_prefix(filename)
    stem = Path(clean_name).stem.strip()
    parts = ARTIST_SEPARATOR_RE.split(stem, maxsplit=1)

    if len(parts) == 2 and parts[0].strip():
        artist_text = parts[0].strip()
        primary_artist = COLLABORATOR_RE.split(artist_text, maxsplit=1)[0].strip()
        if primary_artist:
            key = " ".join(primary_artist.casefold().split())
            return key, primary_artist, True

    # 没有“歌手 - 歌名”结构时，不把未知歌曲错误归到同一歌手。
    return f"__unknown__{stem.casefold()}", "未识别", False


def scan_songs(folder: Path) -> list[Song]:
    songs: list[Song] = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        clean_name = strip_tool_prefix(path.name)
        artist_key, artist_display, detected = detect_artist(clean_name)
        songs.append(
            Song(
                path=path,
                clean_name=clean_name,
                artist_key=artist_key,
                artist_display=artist_display,
                artist_detected=detected,
            )
        )
    return songs


def _spread_score(sequence: list[Song], counts: dict[str, int]) -> float:
    """分数越小，表示同一歌手在整张歌单中分布得越均匀。"""
    total = len(sequence)
    positions: dict[str, list[int]] = defaultdict(list)
    for position, song in enumerate(sequence):
        positions[song.artist_key].append(position)

    score = 0.0
    for artist_key, artist_positions in positions.items():
        count = counts[artist_key]
        if count <= 1:
            continue

        ideal_gap = total / count
        # 当歌曲数量允许时，希望同一歌手至少隔开理想间距的 70%。
        desired_gap = max(2.0, ideal_gap * 0.70)
        gaps = [
            artist_positions[index] - artist_positions[index - 1]
            for index in range(1, len(artist_positions))
        ]
        # 把结尾回到开头也算进去，避免某位歌手只在开头或结尾扎堆。
        gaps.append(total + artist_positions[0] - artist_positions[-1])

        for gap in gaps:
            if gap == 1:
                score += 1_000_000
            shortfall = max(0.0, desired_gap - gap)
            score += shortfall * shortfall

    return score


def _build_even_candidate(
    groups: dict[str, list[Song]], total: int, rng: random.Random
) -> list[Song]:
    """把每位歌手的歌曲均匀放到一条时间线上，再合并为完整歌单。"""
    timeline: list[tuple[float, float, Song]] = []
    for group in groups.values():
        shuffled = group.copy()
        rng.shuffle(shuffled)
        interval = total / len(shuffled)
        phase = rng.random()

        for index, song in enumerate(shuffled):
            target_position = (index + phase) * interval
            timeline.append((target_position, rng.random(), song))

    timeline.sort(key=lambda item: (item[0], item[1]))
    return [song for _, _, song in timeline]


def interleave_by_artist(songs: Iterable[Song], rng: random.Random) -> list[Song]:
    """在整张歌单中均匀分散每位歌手，而不是只避免两首相邻。"""
    groups: dict[str, list[Song]] = defaultdict(list)
    for song in songs:
        groups[song.artist_key].append(song)

    total = sum(len(group) for group in groups.values())
    counts = {artist_key: len(group) for artist_key, group in groups.items()}

    # 多生成一些均匀方案，选择同歌手间距最自然的一份。400 首歌曲也能很快完成。
    attempts = min(180, max(60, len(groups)))
    best_sequence: list[Song] | None = None
    best_score = float("inf")
    for _ in range(attempts):
        candidate = _build_even_candidate(groups, total, rng)
        score = _spread_score(candidate, counts)
        if score < best_score:
            best_sequence = candidate
            best_score = score
            if score == 0:
                break

    return best_sequence or []


def build_rename_plan(folder: Path, seed: int | None = None) -> tuple[list[tuple[Song, Path]], dict[str, int]]:
    songs = scan_songs(folder)
    if not songs:
        raise ValueError("所选文件夹中没有找到支持的音频文件。")

    rng = random.Random(seed)
    ordered = interleave_by_artist(songs, rng)
    width = max(4, len(str(len(ordered))))

    plan: list[tuple[Song, Path]] = []
    for index, song in enumerate(ordered, start=1):
        new_name = f"{index:0{width}d}__{song.clean_name}"
        plan.append((song, folder / new_name))

    detected_artists = {song.artist_key for song in songs if song.artist_detected}
    last_position: dict[str, int] = {}
    repeat_within_5 = 0
    for position, song in enumerate(ordered):
        previous_position = last_position.get(song.artist_key)
        if previous_position is not None and position - previous_position <= 5:
            repeat_within_5 += 1
        last_position[song.artist_key] = position

    stats = {
        "songs": len(songs),
        "artists": len(detected_artists),
        "unknown": sum(not song.artist_detected for song in songs),
        "adjacent_same": sum(
            ordered[i - 1].artist_key == ordered[i].artist_key
            for i in range(1, len(ordered))
        ),
        "repeat_within_5": repeat_within_5,
    }
    return plan, stats


def validate_plan(plan: list[tuple[Song, Path]]) -> None:
    old_paths = {song.path for song, _ in plan}
    target_paths: set[Path] = set()

    for _, target in plan:
        if target in target_paths:
            raise FileExistsError(f"生成了重复文件名：{target.name}")
        target_paths.add(target)

        if target.exists() and target not in old_paths:
            raise FileExistsError(f"目标文件名已被其他文件占用：{target.name}")

        if len(target.name) > 240:
            raise ValueError(f"文件名过长，无法安全添加前缀：{target.name}")


def write_log(folder: Path, plan: list[tuple[Song, Path]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = folder / f"{LOG_PREFIX}{stamp}.csv"
    with log_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["原文件名", "新文件名", "识别到的歌手"])
        for song, target in plan:
            writer.writerow([song.path.name, target.name, song.artist_display])
    return log_path


def execute_renames(pairs: list[tuple[Path, Path]]) -> None:
    """分两步重命名，避免旧名称与新名称互相占用。失败时尽力恢复原名。"""
    token = uuid.uuid4().hex
    staged: list[tuple[Path, Path, Path]] = []

    try:
        for index, (old_path, new_path) in enumerate(pairs):
            temp_path = old_path.with_name(f".__歌曲乱序临时__{token}_{index}{old_path.suffix}")
            old_path.rename(temp_path)
            staged.append((old_path, temp_path, new_path))
    except Exception:
        for old_path, temp_path, _ in reversed(staged):
            if temp_path.exists() and not old_path.exists():
                temp_path.rename(old_path)
        raise

    completed: list[tuple[Path, Path, Path]] = []
    try:
        for item in staged:
            old_path, temp_path, new_path = item
            temp_path.rename(new_path)
            completed.append(item)
    except Exception as error:
        rollback_errors: list[str] = []
        for old_path, _, new_path in reversed(completed):
            try:
                if new_path.exists() and not old_path.exists():
                    new_path.rename(old_path)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        for old_path, temp_path, _ in reversed(staged[len(completed) :]):
            try:
                if temp_path.exists() and not old_path.exists():
                    temp_path.rename(old_path)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))

        if rollback_errors:
            raise RuntimeError(
                f"重命名失败，而且有文件未能自动恢复：{error}\n"
                + "\n".join(rollback_errors)
            ) from error
        raise


def rename_songs(folder: Path, seed: int | None = None) -> tuple[Path, dict[str, int], list[str]]:
    plan, stats = build_rename_plan(folder, seed=seed)
    validate_plan(plan)
    log_path = write_log(folder, plan)

    pairs = [(song.path, target) for song, target in plan if song.path != target]
    try:
        execute_renames(pairs)
    except Exception:
        try:
            log_path.rename(log_path.with_name(log_path.stem + "_失败" + log_path.suffix))
        except OSError:
            pass
        raise

    preview = [target.name for _, target in plan[:20]]
    return log_path, stats, preview


def latest_active_log(folder: Path) -> Path | None:
    candidates = [
        path
        for path in folder.glob(f"{LOG_PREFIX}*.csv")
        if "_已撤销" not in path.stem and "_失败" not in path.stem
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def load_undo_pairs(log_path: Path) -> list[tuple[Path, Path]]:
    folder = log_path.parent
    pairs: list[tuple[Path, Path]] = []
    with log_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"原文件名", "新文件名"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("恢复记录格式不正确。")
        for row in reader:
            current = folder / row["新文件名"]
            original = folder / row["原文件名"]
            if not current.exists():
                raise FileNotFoundError(f"找不到待恢复文件：{current.name}")
            pairs.append((current, original))
    return pairs


def undo_latest(folder: Path) -> tuple[Path, int]:
    log_path = latest_active_log(folder)
    if log_path is None:
        raise FileNotFoundError("没有找到可撤销的重命名记录。")

    pairs = load_undo_pairs(log_path)
    current_paths = {old for old, _ in pairs}
    for _, original in pairs:
        if original.exists() and original not in current_paths:
            raise FileExistsError(f"原文件名已被其他文件占用：{original.name}")

    execute_renames(pairs)
    undone_log = log_path.with_name(log_path.stem + "_已撤销" + log_path.suffix)
    log_path.rename(undone_log)
    return undone_log, len(pairs)


class MusicShuffleApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("歌曲按歌手交叉乱序重命名")
        self.root.geometry("760x560")
        self.root.minsize(660, 480)

        script_folder = Path(sys.argv[0]).resolve().parent
        self.folder_var = tk.StringVar(value=str(script_folder))
        self.status_var = tk.StringVar(value="请选择歌曲文件夹，然后点击蓝色按钮。")

        self._build_ui()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(outer, text="歌曲交叉乱序工具", font=("Microsoft YaHei UI", 18, "bold"))
        title.pack(anchor="w")
        ttk.Label(
            outer,
            text="识别“歌手 - 歌名”，把每位歌手均匀分散到整张歌单，再添加 0001__ 形式的播放顺序。",
            wraplength=700,
        ).pack(anchor="w", pady=(6, 18))

        folder_frame = ttk.LabelFrame(outer, text="歌曲文件夹", padding=12)
        folder_frame.pack(fill="x")
        ttk.Entry(folder_frame, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_frame, text="选择文件夹", command=self.choose_folder).pack(side="left", padx=(10, 0))

        button_frame = ttk.Frame(outer)
        button_frame.pack(fill="x", pady=16)
        self.rename_button = tk.Button(
            button_frame,
            text="一键交叉乱序并重命名",
            command=self.run_rename,
            bg="#1769e0",
            fg="white",
            activebackground="#1257bd",
            activeforeground="white",
            font=("Microsoft YaHei UI", 12, "bold"),
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        )
        self.rename_button.pack(side="left")
        ttk.Button(button_frame, text="撤销最近一次重命名", command=self.run_undo).pack(
            side="left", padx=(12, 0), ipady=8
        )

        notes = (
            "支持 MP3、FLAC、WAV、M4A、AAC、OGG、WMA、APE。只处理所选文件夹当前层，不处理子文件夹。\n"
            "重复运行会先识别并替换旧的 0001__ 前缀。每次操作都会在歌曲文件夹保存 CSV 恢复记录。\n"
            "程序会按歌曲占比计算间隔。例如某位歌手有 40 首、总共 400 首，就尽量每隔约 10 首出现一次。"
        )
        ttk.Label(outer, text=notes, wraplength=700, foreground="#555555").pack(anchor="w", pady=(0, 12))

        ttk.Label(outer, textvariable=self.status_var, font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", pady=(3, 8)
        )
        self.output = tk.Text(outer, height=13, wrap="none", font=("Consolas", 10))
        self.output.pack(fill="both", expand=True)
        self.output.insert("end", "重命名后的前 20 首歌曲会显示在这里。\n")
        self.output.configure(state="disabled")

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or None)
        if chosen:
            self.folder_var.set(chosen)

    def get_folder(self) -> Path:
        raw = self.folder_var.get().strip().strip('"')
        folder = Path(raw)
        if not folder.is_dir():
            raise NotADirectoryError("请选择一个真实存在的歌曲文件夹。")
        return folder

    def set_output(self, lines: Iterable[str]) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", "\n".join(lines))
        self.output.configure(state="disabled")

    def run_rename(self) -> None:
        try:
            folder = self.get_folder()
            plan, stats = build_rename_plan(folder)
        except Exception as error:
            messagebox.showerror("无法开始", str(error))
            return

        detail = (
            f"找到 {stats['songs']} 首歌曲，识别到 {stats['artists']} 位主要歌手。\n"
            f"有 {stats['unknown']} 首未识别歌手，将按独立歌曲参与乱序。\n\n"
            "将只修改文件名，不修改音频内容。是否继续？"
        )
        if not messagebox.askyesno("确认批量重命名", detail):
            return

        self.rename_button.configure(state="disabled")
        self.status_var.set("正在重命名，请不要关闭程序……")
        self.root.update_idletasks()
        try:
            # 确认后重新生成一次正式顺序，让每次点击都得到新的乱序结果。
            log_path, final_stats, preview = rename_songs(folder)
        except Exception as error:
            self.status_var.set("重命名失败，程序已尽力恢复原文件名。")
            messagebox.showerror("重命名失败", str(error))
        else:
            adjacent_text = (
                "没有相邻的同歌手歌曲"
                if final_stats["adjacent_same"] == 0
                else f"有 {final_stats['adjacent_same']} 处相邻同歌手歌曲"
            )
            self.status_var.set(
                f"完成：{final_stats['songs']} 首歌曲已均匀分散，{adjacent_text}；"
                f"同歌手在 5 首内再次出现 {final_stats['repeat_within_5']} 次。"
            )
            self.set_output(preview)
            messagebox.showinfo(
                "处理完成",
                f"已重命名 {final_stats['songs']} 首歌曲。\n\n"
                f"恢复记录：{log_path.name}\n"
                "需要恢复时，点击“撤销最近一次重命名”。",
            )
        finally:
            self.rename_button.configure(state="normal")

    def run_undo(self) -> None:
        try:
            folder = self.get_folder()
            log_path = latest_active_log(folder)
            if log_path is None:
                raise FileNotFoundError("所选文件夹中没有可撤销的重命名记录。")
        except Exception as error:
            messagebox.showerror("无法撤销", str(error))
            return

        if not messagebox.askyesno(
            "确认撤销",
            f"将按照以下记录恢复文件名：\n{log_path.name}\n\n是否继续？",
        ):
            return

        try:
            undone_log, count = undo_latest(folder)
        except Exception as error:
            self.status_var.set("撤销失败。")
            messagebox.showerror("撤销失败", str(error))
        else:
            self.status_var.set(f"已恢复 {count} 首歌曲的原文件名。")
            self.set_output([f"已撤销：{undone_log.name}"])
            messagebox.showinfo("撤销完成", f"已恢复 {count} 首歌曲的原文件名。")


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    MusicShuffleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
