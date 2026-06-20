"""JMComic 本子信息查询脚本，供 Bot 调用。"""
import argparse
import io
import logging
import os
import sys

# 屏蔽 JMComic 库的 logging 输出
logging.basicConfig(level=logging.CRITICAL)

from jmcomic import JmOption


def format_album_info(album) -> str:
    """把 JmAlbumDetail 格式化为纯文本。"""
    lines = []

    lines.append(f"[{album.name}]")
    lines.append(f"JM{album.album_id}")
    lines.append("")

    if album.authors:
        lines.append(f"作者: {', '.join(album.authors)}")

    lines.append(f"发布: {album.pub_date}  |  更新: {album.update_date}")
    lines.append(f"总页数: {album.page_count}  观看: {album.views}  点赞: {album.likes}  评论: {album.comment_count}")
    lines.append("")

    if album.tags:
        lines.append(f"标签: {', '.join(album.tags[:10])}")
    if album.actors:
        lines.append(f"人物: {', '.join(album.actors[:10])}")
    if album.works:
        lines.append(f"作品: {', '.join(album.works[:10])}")
    lines.append("")

    lines.append(f"章节 ({len(album.episode_list)}):")
    for pid, pindex, pname in album.episode_list:
        lines.append(f"  {pname}  (id: {pid})")

    if album.description:
        desc = album.description[:200]
        lines.append(f"\n简介: {desc}")

    return "\n".join(lines)


def main():
    # 强制 UTF-8 避免 Windows GBK 编码问题
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description="查询 JMComic 本子信息")
    parser.add_argument("album_id", help="本子 ID")
    args = parser.parse_args()

    album_id = args.album_id

    # 屏蔽 JMComic 库的 print 输出，只保留我们的格式化结果
    null = open(os.devnull, 'w', encoding='utf-8')
    old_stdout = sys.stdout
    sys.stdout = null

    try:
        option = JmOption.default()
        client = option.build_jm_client()
        album = client.get_album_detail(album_id)
    except Exception as e:
        sys.stdout = old_stdout
        null.close()
        print(f"查询失败: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if sys.stdout is null:
            sys.stdout = old_stdout
            null.close()

    # 现在恢复 stdout，打印干净的格式化结果
    print(format_album_info(album))


if __name__ == "__main__":
    main()
