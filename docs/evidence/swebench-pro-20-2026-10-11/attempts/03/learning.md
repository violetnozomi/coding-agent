# 第 03 题复盘：封面 URL 白名单与导入副作用

冻结补丁增加 process_cover_url：从 edition 中移除 cover 字段，仅在 URL 使用 HTTP/HTTPS 且主机名精确匹配允许集合时返回封面地址。主机名用 casefold 比较；不采用容易误接收相似域名的子串匹配。导入流程使用验证后的 URL，避免对不支持的来源尝试抓取。

可讲解 URL 解析与字符串前缀判断的区别、白名单匹配、输入字典的就地修改契约，以及“拒绝抓取”和“导入其余数据”的职责拆分。这些检查不是对所有网络安全问题的完整保证。

Agent completed。同一补丁在修正 pytest 环境后官方必需项 6/6 通过；扩展运行该测试文件为 83 passed、1 failed、3 rerun，失败名称 test_covers_are_added_to_edition。这个范围不能宣称全绿，也不能把官方必需项通过等同于全部既有行为正确。修正评分没有重新调用模型。

证据：[补丁](model.patch)、[命令事实](command-evidence.json)、[修正评分](evaluation-corrected-result.json)、[原始日志](evaluation-corrected-stdio.txt)。
