#!/bin/bash
# ============================================================
#  JM2 Bot 发布打包脚本
#  生成干净的 tar.gz 发布包，排除敏感和运行时文件
# ============================================================

set -e

RED='\033[0;1;31m'
GREEN='\033[0;1;32m'
YELLOW='\033[0;1;33m'
CYAN='\033[0;1;36m'
NC='\033[0m'

VERSION="${1:-$(date +%Y%m%d)}"
RELEASE_NAME="jm2-bot-v${VERSION}"
OUTPUT_DIR="./release"
PACKAGE="${OUTPUT_DIR}/${RELEASE_NAME}.tar.gz"

echo -e "${CYAN}📦 JM2 Bot 发布打包${NC}"
echo -e "   版本: ${VERSION}"
echo -e "   输出: ${PACKAGE}"
echo ""

# ── 安全检查 ──
echo -e "${YELLOW}[1/4] 安全检查...${NC}"

# 检查是否有硬编码的 QQ 号
QQ_LEAKS=$(grep -rn '[1-9][0-9]{5,11}' \
    --include='*.py' --include='*.sh' --include='*.md' --include='*.json' \
    . 2>/dev/null | grep -v '.git/' | grep -v 'search_index' | grep -v '__pycache__' | grep -v '.egg-info' || true)

# 过滤掉合法的数字（如端口号、ID 示例等）
if [ -n "$QQ_LEAKS" ]; then
    # 尝试区分真实 QQ 号（6-11位且看起来不像端口/示例）
    REAL_LEAKS=$(echo "$QQ_LEAKS" | grep -v 'port.*[0-9]\{4,5\}' | grep -v 'example\|示例\|350234\|example' || true)
    if [ -n "$REAL_LEAKS" ]; then
        echo -e "${RED}  ⚠️  发现疑似 QQ 号:${NC}"
        echo "$REAL_LEAKS"
        echo ""
        read -p "  是否继续打包？[y/N] " yn
        if [[ ! "$yn" =~ ^[Yy] ]]; then
            echo "  已取消"
            exit 1
        fi
    else
        echo -e "  ${GREEN}✓${NC} 未发现敏感 QQ 号"
    fi
fi

# 检查是否有 token/密码泄露
SECRET_LEAKS=$(grep -rni 'token\|password\|secret\|access_key' \
    --include='*.py' --include='*.sh' --include='*.json' \
    . 2>/dev/null | grep -v '.git/' | grep -v 'search_index' | grep -v '__pycache__' | grep -v '.egg-info' | grep -v 'os.getenv\|os.environ\|getenv.*TOKEN\|ACCESS_TOKEN.*getenv\|TODO\|FIXME\|example\|#.*token' || true)

if [ -n "$SECRET_LEAKS" ]; then
    echo -e "${YELLOW}  ⚠️  发现可能的敏感字段引用:${NC}"
    echo "$SECRET_LEAKS"
    echo "  (请确认以上均为环境变量读取，而非硬编码)"
fi
echo -e "  ${GREEN}✓${NC} 安全检查通过"

# ── 清理临时文件 ──
echo -e "\n${YELLOW}[2/4] 清理临时文件...${NC}"
find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find . -type f -name '*.pyc' -delete 2>/dev/null || true
find . -type f -name '.DS_Store' -delete 2>/dev/null || true
echo -e "  ${GREEN}✓${NC} 清理完成"

# ── 打包 ──
echo -e "\n${YELLOW}[3/4] 创建发布包...${NC}"
mkdir -p "$OUTPUT_DIR"

tar -czf "$PACKAGE" \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='downloads' \
    --exclude='downloads/*' \
    --exclude='logs' \
    --exclude='logs/*' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='*.egg-info' \
    --exclude='.env' \
    --exclude='*.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    --exclude='release' \
    --exclude='dist' \
    --exclude='*.tar.gz' \
    --exclude='*.zip' \
    --exclude='.DS_Store' \
    --exclude='.idea' \
    --exclude='.vscode' \
    --exclude='.agent' \
    --exclude='gemini.md' \
    --exclude='my_pdf_output' \
    --exclude='[Teterun]*' \
    --exclude='*[Part*' \
    --exclude='*P站*' \
    --exclude='*Xidaidai*' \
    --exclude='*priority_*' \
    --exclude='*原神*' \
    .

PACKAGE_SIZE=$(du -h "$PACKAGE" | cut -f1)
echo -e "  ${GREEN}✓${NC} 打包完成: ${PACKAGE} (${PACKAGE_SIZE})"

# ── 校验 ──
echo -e "\n${YELLOW}[4/4] 校验包内容...${NC}"
echo "  包含文件数: $(tar -tzf "$PACKAGE" | wc -l)"
echo "  主要文件:"
tar -tzf "$PACKAGE" | grep -v '/$' | grep -v '__pycache__' | head -30 | while read f; do echo "    $f"; done

# 二次确认没有敏感文件
echo ""
echo "  敏感文件检查:"
if tar -tzf "$PACKAGE" | grep -qE '\.env$|\.db$|logs/'; then
    echo -e "    ${RED}❌ 发现敏感文件！请检查${NC}"
    tar -tzf "$PACKAGE" | grep -E '\.env$|\.db$|logs/'
    exit 1
else
    echo -e "    ${GREEN}✓${NC} 无敏感文件泄露"
fi

# ── SHA256 ──
if command -v sha256sum &>/dev/null; then
    sha256sum "$PACKAGE" > "${PACKAGE}.sha256"
    echo -e "\n  ${CYAN}SHA256:${NC}"
    cat "${PACKAGE}.sha256"
fi

echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     ✅ 发布包已就绪！                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}文件:${NC} ${PACKAGE}"
echo -e "  ${CYAN}大小:${NC} ${PACKAGE_SIZE}"
echo ""
echo -e "  ${CYAN}用户安装方法:${NC}"
echo "    tar -xzf ${RELEASE_NAME}.tar.gz"
echo "    cd JM2 && bash install.sh"
echo ""
echo -e "  ${YELLOW}⚠️ 发布前请确认:${NC}"
echo "    1. 版本号已更新 (src/jmcomic/__init__.py)"
echo "    2. CHANGELOG 已整理"
echo "    3. 已在干净环境测试 install.sh"
