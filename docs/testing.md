# 测试与复现

从项目根目录执行下列命令。先按 [README](../README.zh-CN.md#演示与测试离线模式) 安装依赖并初始化合成数据。离线检查不调用真实模型；报告与 Trace 写入 Git 忽略的 `artifacts/` 目录。

## 离线验证

```bash
uv run --no-sync python -m scripts.evaluate --mode demo --output artifacts/eval
uv run --no-sync python -m scripts.smoke_http --mode demo
```

第一条命令运行固定评测集，输出 `report.json`、`report.md` 和 `traces.sqlite3`；第二条检查 HTTP 接口及流式响应。使用其他输出目录时，相应替换下文路径。命令可在 Bash 或 PowerShell 中执行。

## 评测范围与评分

Criteo外部查询组件提供独立的下载、评测与证据核查入口，见[外部数据查询评测](#外部数据查询评测)，与下述合成回归集分开记录。

回归集版本为 **2.1**，含 100 条开发定义的合成任务：20 条口径、25 条 SQL、25 条诊断、20 条安全/信息不足和 10 条鲁棒性。任务、标签和预期条件见 [data/eval/regression.jsonl](../data/eval/regression.jsonl)。固定集用于发现回归，不衡量真实业务泛化能力。

| 类别 | 评分条件 | 解释边界 |
| --- | --- | --- |
| 全部 | 得分为 1、工具链与证据类型匹配、运行完成；鲁棒性预期失败也可通过 | `diagnostic_sql` 不参与工具序列比较；证据类型存在不保证解释正确 |
| RAG | 所需引用全部命中；声明 `required_claims` 的任务还需正文满足文本条件 | 引用覆盖和答案得分分别记录；文本匹配不等于完整语义判断 |
| SQL | 首次 SQL 结果与独立 oracle 比较行数、必需列名和值；按任务要求比较顺序 | 允许额外列，保留重复行；数值容差为相对 `1e-6`、绝对 `1e-8`；字段契约与数值一致性分别记录 |
| 诊断 | 主因首位得 1 分；主因在后或首位为允许次因得 0.5 分；正常对照需无根因且样本充分 | 异常题主要来自固定注入的重叠窗口，不验证真实因果或全部解释 |
| 安全与鲁棒性 | 检查状态、固定结论文字、可选错误码、禁止查询条件与无根因 | 仅覆盖定义的代码路径及故障场景 |

`citation_correct` 统计非 safety 任务的 `evidence_ok AND completed` 均值，预期失败也可能计为 0，不能当作引用准确率。阈值见 [configs/regression_thresholds.json](../configs/regression_thresholds.json)。比较报告时，应保持任务、知识、数据、模式及评分版本一致。

## 阅读逐题证据

将报告和同目录的 `traces.sqlite3` 导出为可浏览的证据页：

```bash
uv run --no-sync python -m scripts.evaluate --review-report artifacts/eval/report.json --output artifacts/eval-review
```

用浏览器打开 `artifacts/eval-review/review.html`，即可查看问题、预期答案、SQL 结果、知识来源和评分对照，无需启动服务器。`--review-report` 可以重复指定，用于比较匹配当前任务与知识版本的报告。输出目录已有记录时，需指定新目录，脚本会拒绝覆盖。

| 文件 | 用途 |
| --- | --- |
| `review.html` | 逐题浏览、筛选和记录导入导出 |
| `review.json` | 结构化任务、证据、评分规则与报告指纹 |
| `review.csv` | 标签、评分意见、来源和备注的交换格式 |
| `review.md` | 编辑器可读的任务与证据索引 |

页面中填写的备注可以导出为 `evaluation-notes.json`。将下载文件放到上述输出目录后，可生成包含备注的页面：

```bash
uv run --no-sync python -m scripts.evaluate --review-packet artifacts/eval-review/review.json --review-notes artifacts/eval-review/evaluation-notes.json --output artifacts/eval-review/filled.html
```

备注必须匹配证据包、任务及报告 ID；`draft` / `recorded` 只表示是否填写。导出保留检查方法与记录来源，AI 辅助内容需注明工具名称。导出命令不调用模型、不加载 `.env`，也不改写原始自动分数。

## 真实模型与缓存

按 [README](../README.zh-CN.md#快速启动真实模型) 配置模型并安装 `youtu` 依赖后，可以执行以下检查。它们会请求已配置的模型并消耗供应商额度。

```bash
uv run --no-sync python -m scripts.model_probe
uv run --no-sync python -m scripts.cache_probe --mode youtu
uv run --no-sync python -m scripts.evaluate --mode youtu --output artifacts/eval-youtu
```

模型探针检查普通响应、流式与连续工具调用；缓存探针记录提供方返回的缓存命中用量。仅使用直接 `openai` 适配器时，模型探针加 `--without-youtu`，其余命令改用 `--mode openai`。

提供方未返回的用量、未配置价格的成本均为 `null`；缓存命中不保证总费用下降。运行结果取决于实际模型与环境，应以自己生成的报告为准。

## 外部数据查询评测

仓库提供Criteo下载、问答生成、标准答案校验和模型评测脚本。数据集与结果在本地生成，不随仓库分发。数据来源版本及许可见 `configs/criteo_source.json`；使用前确认适用的CC-BY-NC-SA-4.0许可。

从项目根目录依次执行：

```bash
uv run --no-sync python -m scripts.prepare_criteo
uv run --no-sync python -m scripts.build_large_eval
uv run --no-sync python -m scripts.verify_large_eval
uv run --no-sync python -m scripts.evaluate_large --split dev --limit 200 --concurrency 4 --output artifacts/criteo-dev
uv run --no-sync python -m scripts.evaluate_large --split test --concurrency 8 --output artifacts/criteo-test
```

前三步下载公开数据、生成问答并独立校验标准答案；后两步使用 `.env` 中的模型配置，会消耗供应商额度。默认生成110,000题，训练/开发/测试分别为88,000/10,000/12,000题，覆盖20类查询模板。训练划分不表示已经训练模型。

完整验收要求标准答案通过校验、测试集至少10,000题且全部完成，正确率严格超过99.5%。部分测试或开发集成绩不能触发验收。报告保存在指定输出目录；重复运行相同配置与目录可继续未完成题，已有失败不会被覆盖。改变模型、代码或输入后需使用新目录。

此评测针对SQL规划与受控执行，不能代表RAG、预算操作、根因诊断或任意自然语言的准确率。模型表现以本次生成的报告为准。
