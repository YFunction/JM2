from pathlib import Path
import argparse
import os
import shutil
import sys

from jmcomic import download_album, Feature


DEFAULT_ALBUM_ID = 350234
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "downloads"


def cleanup_download_tree(root: Path) -> None:
    """只保留 PDF 文件，删除其它目录和文件。"""
    if not root.exists():
        return

    for item in root.iterdir():
        if item.is_file() and item.suffix.lower() == ".pdf":
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                item.unlink(missing_ok=True)
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="下载 JMComic 本子并自动转成 PDF。"
    )
    parser.add_argument(
        "album_id",
        nargs="?",
        default=str(DEFAULT_ALBUM_ID),
        help=f"本子 ID（默认: {DEFAULT_ALBUM_ID}）"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"输出目录（默认: {DEFAULT_OUTPUT_DIR}）"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    album_id = args.album_id
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"目标本子 ID: {album_id}")
    print(f"输出目录: {output_dir}")

    old_cwd = os.getcwd()
    try:
        os.chdir(output_dir)
        album, _ = download_album(
            album_id,
            extra=Feature.export_pdf,
        )
        cleanup_download_tree(output_dir)
        print(f"下载完成，结果文件将生成在: {output_dir}")
        print(f"本子标题: {album.name if hasattr(album, 'name') else album_id}")
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"运行失败: {e}", file=sys.stderr)
        sys.exit(1)
