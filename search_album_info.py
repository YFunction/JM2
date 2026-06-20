"""JMComic 本子信息查询脚本，供 Bot 调用。"""
import argparse
import io
import logging
import os
import re
import sys

# 屏蔽 JMComic 库的 logging 输出
logging.basicConfig(level=logging.CRITICAL)

from jmcomic import JmOption, JmMagicConstants

# 排序方式映射（中文别名 → API 常量）
SORT_ALIASES = {
    '收藏': JmMagicConstants.ORDER_BY_LIKE,
    '点赞': JmMagicConstants.ORDER_BY_LIKE,
    '喜欢': JmMagicConstants.ORDER_BY_LIKE,
    '最新': JmMagicConstants.ORDER_BY_LATEST,
    '发布时间': JmMagicConstants.ORDER_BY_LATEST,
    '观看': JmMagicConstants.ORDER_BY_VIEW,
    '观看次数': JmMagicConstants.ORDER_BY_VIEW,
    '长度': JmMagicConstants.ORDER_BY_PICTURE,
    '页数': JmMagicConstants.ORDER_BY_PICTURE,
    '图片': JmMagicConstants.ORDER_BY_PICTURE,
}
SORT_LABELS = {
    JmMagicConstants.ORDER_BY_LIKE: '收藏',
    JmMagicConstants.ORDER_BY_LATEST: '发布时间',
    JmMagicConstants.ORDER_BY_VIEW: '观看次数',
    JmMagicConstants.ORDER_BY_PICTURE: '页数',
}
DEFAULT_SORT = JmMagicConstants.ORDER_BY_LIKE  # 默认按收藏排序
DEFAULT_TOP = 20  # 默认显示前 20 条


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


def format_search_results(page, keyword: str, sort_order: str, top_n: int) -> str:
    """把 JmSearchPage 格式化为搜索结果文本。"""
    sort_label = SORT_LABELS.get(sort_order, sort_order)
    total = page.total
    shown = min(len(page), top_n)

    lines = []
    lines.append(f"搜索「{keyword}」共 {total} 个结果，按{sort_label}排序，显示前 {shown} 个：")
    lines.append("")

    for i, (aid, title, tags) in enumerate(page.iter_id_title_tag()):
        if i >= top_n:
            break
        tag_str = ", ".join(tags[:3]) if tags else ""
        lines.append(f"JM{aid}  {title}")
        if tag_str:
            lines.append(f"  标签: {tag_str}")

    if total > shown:
        lines.append(f"\n...共 {total} 条结果，仅显示前 {shown} 条")

    return "\n".join(lines)


def build_search_query(raw_query: str) -> str:
    """
    将用户输入的关键词转为 JM API 搜索查询。
    多词搜索自动添加 + 前缀实现相关性匹配（要求每词都出现，但不要求紧邻）。
    若用户已手动使用 +/- 语法则保持原样。
    """
    query = raw_query.strip()
    # 用户已使用 +/- 语法，保持原样
    if re.search(r'[+-]\S', query):
        return query
    # 单字或没有空格，原样搜索
    words = query.split()
    if len(words) <= 1:
        return query
    # 多词：每词加 + 实现相关性匹配
    return ' '.join(f'+{w}' for w in words)


def main():
    # 强制 UTF-8 避免 Windows GBK 编码问题；Linux 下若 stdout 无 buffer 则跳过
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="查询 JMComic 本子信息")
    parser.add_argument("query", help="本子 ID 或搜索关键词")
    parser.add_argument("--kw", action="store_true", help="强制按关键词搜索（即使输入是纯数字）")
    parser.add_argument("-s", "--sort", type=str, default=None,
                        help=f"排序方式: 收藏/最新/观看/长度 (默认: 收藏)")
    parser.add_argument("-n", "--top", type=int, default=DEFAULT_TOP,
                        help=f"显示前 N 个结果 (默认: {DEFAULT_TOP})")
    args = parser.parse_args()

    query = args.query

    # 解析排序方式
    sort_order = DEFAULT_SORT
    if args.sort:
        sort_key = args.sort.strip()
        sort_order = SORT_ALIASES.get(sort_key)
        if sort_order is None:
            print(f"不支持的排序方式: {sort_key}，可选: {', '.join(SORT_ALIASES.keys())}", file=sys.stderr)
            sys.exit(1)

    top_n = args.top

    # 智能判断：纯数字 → ID 精确查询，否则 → 关键词搜索
    is_numeric = re.fullmatch(r'\d+', query)
    if args.kw:
        is_numeric = False  # --kw 强制按关键词搜索

    # 屏蔽 JMComic 库的 print 输出，只保留我们的格式化结果
    null = open(os.devnull, 'w', encoding='utf-8')
    old_stdout = sys.stdout
    sys.stdout = null

    try:
        option = JmOption.default()
        client = option.build_jm_client()

        if is_numeric:
            # 精确 ID 查询
            album = client.get_album_detail(query)
            result = format_album_info(album)
        else:
            # 关键词搜索（带排序和相关性优化）
            search_query = build_search_query(query)
            page = client.search_site(search_query, page=1, order_by=sort_order)
            # 0 结果时回退：去掉 + 前缀，宽松匹配
            if page.total == 0 and search_query != query:
                page = client.search_site(query, page=1, order_by=sort_order)
            result = format_search_results(page, query, sort_order, top_n)

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
    print(result)


if __name__ == "__main__":
    main()
