# 数据字典与诊断口径

本项目只使用固定种子的合成数据。生成参数：`data/seeds/config.json`；结构：`data/schema.sql`；指标：`configs/metrics.json`；字段机器可读说明：`configs/schema.json`。

| 表 | 主键 | 业务含义 |
| --- | --- | --- |
| accounts | account_id | 广告账户、币种、时区 |
| campaigns | campaign_id | 账户下的投放计划 |
| ad_groups | ad_group_id | 计划下的广告组 |
| creatives | creative_id | 广告组下的创意 |
| channels | channel_id | 搜索/信息流渠道 |
| regions | region_id | 深圳/上海地域 |
| devices | device_id | 移动端/桌面端 |
| daily_metrics | date + campaign_id + creative_id + channel_id + region_id + device_id | 互斥投放切片的每日指标 |

所有 ID 是稳定整数，名称只在维表；事实表保留维度外键以支持直接分组。合成器保证创意→广告组→Campaign→账户一致，质量测试独立检查，运行时关联不得重复事实。原始指标不允许空值、负数；比率在分母为零时返回 null。金额为 CNY 元，日期为 Asia/Shanghai 的广告事件日。

| 字段 | 中文含义 |
| --- | --- |
| date | 广告事件日期，闭区间过滤 |
| impressions / clicks / conversions | 曝光数 / 点击数 / 成熟转化数 |
| spend / conversion_value | 消耗金额 / 转化价值 |
| daily_budget | 当日分配到该互斥切片的预算，可跨切片相加 |
| budget_limited | 该切片是否受预算约束，0/1；聚合求和为受限切片数量 |

固定范围为 2026-06-01 至 2026-08-29，共 90 天、4,320 行。2026-08-23 至 08-29 分别向 Campaign 1–6 注入流量下降、CTR 下滑、CVR 下滑、CPC 上涨、预算受限、单一信息流渠道成本异常。Campaign 6 的次因包括 CPC 上涨和 CVR 下滑。08-09 至 08-15 是正常对照期。真值输出到 `data/generated/anomaly_truth.json`，运行时代码不读取此文件。

默认最近 7 天以数据最大日期为锚点，不使用机器当前日期。上期是紧邻的等长区间；同比需要去年数据，此数据集不支持。归因为点击后 7 天，合成数据视为已成熟；点击 CVR 和曝光转化率有独立知识片段。

CTR=clicks/impressions，CVR=conversions/clicks，CPC=spend/clicks，CPA=spend/conversions，ROAS=conversion_value/spend。必须先汇总分子分母再相除。漏斗贡献按 log(本期/上期) 分解；维度转化贡献为该维度转化变化/总变化，总变化为零时为 null。CPC 成本类 contribution 是该成本信号的规则权重，不应与漏斗贡献相加。

诊断门槛：两期都至少 100 点击、20 转化，各切片日期覆盖完整、两期切片相同。变化阈值为 20%；预算使用率阈值 95%；渠道异常要求存在 CPC 上涨超过 30% 的渠道，同时至少一个对照渠道低于 10%。不足时不输出根因。置信度由样本量、变化强度和其他漏斗指标稳定性加权形成，最高 0.95；这是解释性规则分数，不是统计显著性或真实因果概率。
