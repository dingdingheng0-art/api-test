"""校验层用到的异常。

单独一个文件的原因：``verify/`` 下面好几个模块都要抛这几类错，
放一起方便统一处理，也方便调用方 ``except VerifyError`` 一把接住。
"""
from __future__ import annotations


class VerifyError(RuntimeError):
    """校验层的通用错误。

    意思是"校验这一步没能正常完成"—— 跟"校验了但结果不符"是两回事。
    结果不符走 ``AssertionError``（pytest 会当成用例失败）。
    """


class VerifyConfigError(VerifyError):
    """配置缺失或配错了。

    比如用例里写了 ``redis_get`` 断言，但 ``config.yaml`` 里没配 redis。
    """


class DependencyMissing(VerifyError):
    """缺少第三方依赖。

    比如要用 redis 校验但没装 ``redis`` 包。消息里直接告诉人怎么装。
    """
