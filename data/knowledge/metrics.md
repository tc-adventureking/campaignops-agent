<!-- {"doc_id": "metrics", "title": "指标字典", "version": "1.1", "effective_date": "2026-09-12"} -->
# 指标字典

## 基础指标 {#metrics.base}
曝光 impressions、点击 clicks、消耗 spend、转化 conversions、转化价值 conversion_value 都是可加指标。总消耗为 SUM(spend)，总曝光为 SUM(impressions)，总点击为 SUM(clicks)，总转化为 SUM(conversions)。金额是人民币 CNY，日期按 Asia/Shanghai 汇总。必须先过滤请求的日期及 Campaign 范围，再聚合。

## 点击率 {#metrics.ctr}
CTR / 点击率 定义为 clicks / impressions。SQL 口径为 SUM(clicks) / NULLIF(SUM(impressions), 0)。点击为零且曝光大于零时，CTR 为 0；曝光为零时，CTR 为 null。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 点击转化率 {#metrics.cvr}
CVR / 转化率 / 点击转化率 定义为 conversions / clicks。SQL 口径为 SUM(conversions) / NULLIF(SUM(clicks), 0)。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 曝光转化率 {#metrics.impression_cvr}
曝光转化率 定义为 conversions / impressions。SQL 口径为 SUM(conversions) / NULLIF(SUM(impressions), 0)。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 单次点击成本 {#metrics.cpc}
CPC / 点击成本 定义为 spend / clicks。SQL 口径为 SUM(spend) / NULLIF(SUM(clicks), 0)。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 单次转化成本 {#metrics.cpa}
CPA / 转化成本 定义为 spend / conversions。SQL 口径为 SUM(spend) / NULLIF(SUM(conversions), 0)。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 广告支出回报 {#metrics.roas}
ROAS / 广告回报 定义为 conversion_value / spend。SQL 口径为 SUM(conversion_value) / NULLIF(SUM(spend), 0)。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 预算使用率 {#metrics.budget_utilization}
预算使用率 定义为 spend / daily_budget。SQL 口径为 SUM(spend) / NULLIF(SUM(daily_budget), 0)。分母为零返回 null，不能填零。所有金额为人民币 CNY。

## 聚合规则 {#metrics.aggregation}
聚合后计算：先求分子与分母之和，再计算 CTR、CVR、CPC、CPA、ROAS。禁止直接平均每日或渠道比率。事实表按日、Campaign、创意、渠道、地域、设备记录；重复关联会使指标翻倍。
