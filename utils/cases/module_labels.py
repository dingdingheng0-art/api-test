"""模块名 → 中文标签。

**为什么单独放一个文件**

报告的树是 ``epic（项目）> suite（模块）> subSuite（场景）``。
模块名如果显示成英文 ``admin_attribute``，看报告的人根本不知道是哪个功能；
而中文标签 ``商品属性`` 又藏在下一层，两边对不上。

所以模块的显示名统一走这里，规则是：

1. ``configs/modules.yaml`` 里配的 ``label``（最准，能人工改）
2. 模块目录里的 ``module.yaml`` 里配的 ``label``
3. 本文件内置的常见模块名映射
4. 实在没有就把英文按驼峰拆开（至少好读一点）

第 3 条以前只写在 Excel 解析器里，只有 Excel 用例能拿到中文名 ——
YAML 用例和报告层级都拿不到。现在挪到这里，谁都能用。
"""
from __future__ import annotations

# 内置的英文模块名 → 中文
BUILTIN_LABELS: dict[str, str] = {
    "manager_statistics": "首页统计",
    "manager_permission": "权限菜单",
    "sys_user": "系统用户",
    "admin_user": "会员管理",
    "sys_role": "角色管理",
    "sys_menu": "菜单管理",
    "sys_config": "参数配置",
    "shop_transport": "运费模板",
    "shop_pickAddr": "自提点",
    "shop_notice": "店铺公告",
    "prod_spec": "商品规格",
    "prod_prod": "商品管理",
    "prod_prodTag": "商品标签",
    "prod_prodComm": "商品评论",
    "prod_category": "商品分类",
    "order_order": "订单管理",
    "admin_indexImg": "轮播图",
    "admin_hotSearch": "热门搜索",
    "admin_area": "地区管理",
    "token_refresh": "令牌刷新",
    "logOut": "退出登录",
    "captcha_get": "获取验证码",
    "captcha_check": "校验验证码",
    "admin_file": "文件上传",
    "sys_log": "操作日志",
    "admin_delivery": "配送配置",
}


def has_chinese(text: str) -> bool:
    """字符串里有没有中文。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def split_english(name: str) -> str:
    """``admin_indexImg`` → ``admin indexImg``（拆不出中文时至少好读一点）。"""
    words = [w for w in name.replace("-", "_").split("_") if w]
    return " ".join(words)


def builtin_label(name: str) -> str:
    """内置映射里查模块的中文名；查不到返回空串。"""
    if not name:
        return ""
    return BUILTIN_LABELS.get(name, "")
