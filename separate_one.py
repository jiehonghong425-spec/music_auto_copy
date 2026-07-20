#!/usr/bin/env python3
"""简化的逐首分离脚本 — 可断点续传，每次运行处理一首"""
import sqlite3, shutil, subprocess, sys, os, json
from pathlib import Path
from datetime import datetime

# FFmpeg PATH
os.environ["PATH"] = (
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links") + os.pathsep +
    os.environ.get("PATH", "")
)

DB = Path("./progress.db")
OUTPUT = Path("./separated")
MODEL = "htdemucs"
JOBS = 4  # 并行数，利用多核 CPU

def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    # 取一首 downloaded 状态的
    row = conn.execute(
        "SELECT * FROM items WHERE status='downloaded' ORDER BY id LIMIT 1"
    ).fetchone()

    if not row:
        print("没有待分离的文件！")
        conn.close()
        return

    item = dict(row)
    item_id = item["id"]
    title = item["title"]
    dl_path = item.get("download_path", "")

    if not dl_path or not Path(dl_path).exists():
        conn.execute("UPDATE items SET status='failed', error_message='文件不存在' WHERE id=?", (item_id,))
        conn.commit()
        conn.close()
        print(f"[SKIP] {title} — 文件不存在")
        return

    # 标记开始
    conn.execute("UPDATE items SET status='separating' WHERE id=?", (item_id,))
    conn.commit()

    print(f"[{item_id}] {title}")
    print(f"  文件: {dl_path}")

    # 创建输出目录
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-")[:60]
    tmp_dir = OUTPUT / "_tmp"
    final_dir = OUTPUT / f"{item_id}_{safe_title}"

    # 清理旧残留
    if tmp_dir.exists():
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 运行 Demucs
        cmd = [
            sys.executable, "-m", "demucs",
            "--two-stems", "vocals",
            "-n", MODEL,
            "-d", "cpu",
            "-j", str(JOBS),
            "-o", str(tmp_dir),
            str(dl_path),
        ]
        print(f"  命令: {' '.join(cmd[:6])} ...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            err = result.stderr.splitlines()[-5:]
            err_msg = "; ".join(err) if err else f"返回码 {result.returncode}"
            print(f"  FAIL: {err_msg}")
            conn.execute("UPDATE items SET status='failed', error_message=? WHERE id=?", (err_msg[:500], item_id))
            conn.commit()
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
            conn.close()
            return

        # 收集输出
        instrumental = None
        vocals = None
        model_dir = tmp_dir / MODEL
        if model_dir.exists():
            for sub_dir in model_dir.iterdir():
                if not sub_dir.is_dir():
                    continue
                for f in sub_dir.iterdir():
                    if not f.is_file() or f.suffix.lower() not in (".wav", ".mp3", ".flac"):
                        continue
                    name = f.name.lower()
                    if "no_vocals" in name:
                        dest = final_dir / ("instrumental" + f.suffix)
                        shutil.copy2(str(f), str(dest))
                        instrumental = dest
                    elif "vocals" in name:
                        dest = final_dir / ("vocals" + f.suffix)
                        shutil.copy2(str(f), str(dest))
                        vocals = dest

        # 清理临时
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

        if instrumental:
            # 写元数据
            (final_dir / "metadata.json").write_text(
                json.dumps({"model": MODEL, "two_stems": True, "device": "cpu"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            conn.execute(
                "UPDATE items SET instrumental_path=?, vocals_path=?, status='separated', separated_at=? WHERE id=?",
                (str(instrumental), str(vocals) if vocals else "", datetime.now().isoformat(), item_id),
            )
            print(f"  OK! 伴奏: {instrumental.stat().st_size//1024}KB, 人声: {vocals.stat().st_size//1024 if vocals else 0}KB")
        else:
            conn.execute("UPDATE items SET status='failed', error_message='未找到输出文件' WHERE id=?", (item_id,))
            print(f"  FAIL: 未找到输出文件")

        conn.commit()

    except subprocess.TimeoutExpired:
        conn.execute("UPDATE items SET status='failed', error_message='超时(>60分钟)' WHERE id=?", (item_id,))
        conn.commit()
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
        print(f"  FAIL: 超时")
    except Exception as e:
        conn.execute("UPDATE items SET status='failed', error_message=? WHERE id=?", (str(e)[:500], item_id))
        conn.commit()
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
        print(f"  FAIL: {e}")

    conn.close()

if __name__ == "__main__":
    main()
