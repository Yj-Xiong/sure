# Harness 示例页面

打开 `harness-demo.html` 即可离线查看，不需要启动服务器或安装依赖。

页面包含五个视图：

- 运行概览
- 任务与音频约束
- 处理流程
- 产物与门控
- 修复示例

页面中的数据是演示数据，不代表真实评测结果。正式运行应由 `/sure_audio_eval` 生成自己的 context、registry、objective、subjective、analysis 和 run report 产物。

相关性示例只展示模型条件点上“主观 MOS 与客观指标”的 Pearson / Spearman 关系；reference 和 unprocessed 不参与，也不展示四类候选范围相关性。

流程顺序中先生成 `objective_metric_plan.json`，明确指标类型、方向、配对 reference 要求和模型条件分析范围，再开始音频校验与评分。
