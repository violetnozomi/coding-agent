# 第 02 题复盘：表单解析与嵌套默认值

冻结补丁在有请求正文时只解析正文，不混入查询字符串。parse_qs 保留空值、把加号按表单规则解释为空格，并选取同名字段最后一个值。随后 unflatten 还原嵌套结构；默认值不能覆盖已提交的嵌套/indexed 字段祖先，例如 seeds--0--key 存在时不能先注入 seeds=[]。

这一题可以讲解“解析输入→填缺失值→重建结构”的先后关系，以及扁平参数与嵌套对象的冲突。补丁同时改动 upstream/utils.py 与 lists.py，应结合全部 diff 复盘，不能只看入口方法。

Agent completed。初次独立评分受 Agent 依赖安装造成的 pytest 版本混杂阻断；修正评分环境后，同一冻结补丁的官方必需项 6/6 通过，没有重新调用模型或修改补丁。环境混杂事实仍保留，不能当作完全相同运行条件的比较。

证据：[补丁](model.patch)、[工具事实](runtime.jsonl)、[原始评分](evaluation-result.json)、[修正环境评分](evaluation-corrected-result.json)。
