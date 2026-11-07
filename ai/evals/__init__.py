"""智能体评测集（P3）。

**为什么评测集不能只是"跑几条任务看看"**

模型的输出不稳定，同一条任务今天过、明天不过。没有固定的任务集和
机械判分，每次调提示词 / 换模型都是"感觉好像好一点了" —— 这种
判断在真实项目里会一路漂移到没人敢改。

这里的判分只看四类确定性证据：

1. **该调的调了没**（``must_call``）
2. **不该调的调了没**（``must_not_call``）—— 红线
3. **危险操作被工具层拦住了没**（``required_failed_calls``）
4. **结论里的数字对不对**（``answer_must_contain``）

答案写得漂不漂亮不判 —— 那是人看的，不是回归该管的。

用法::

    python scripts/run_eval.py                 # 跑全部
    python scripts/run_eval.py --only EVAL-005 # 只跑红线那条
    python scripts/run_eval.py --dry-run       # 只校验评测集格式，不调模型
"""

from ai.evals.runner import load_tasks, run_all, run_task

__all__ = ["load_tasks", "run_all", "run_task"]
