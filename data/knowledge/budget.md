<!-- {"doc_id": "budget", "title": "预算规则", "version": "1.0", "effective_date": "2026-09-07"} -->
# 预算规则

## 预算限制 {#budget.budget}
预算使用率 = 消耗 / 可用预算。daily_budget 是互斥投放切片分配预算，跨切片可相加。budget_limited=1 表示该切片受预算限制；结合预算使用率超过 95% 与曝光下降进行诊断。预算提额、修改出价、暂停投放均需审批；MVP 不执行任何写操作。
