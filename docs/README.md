# 文档

这里记录设计与判断，不只有结论。想快速上手先看 [安装](../INSTALL.md)；想理解取舍，按下面顺序读最省力。

建议入口：[设计原则](MEMORY_SYSTEM_DESIGN_PRINCIPLES.md) → [架构决策](ADR.md) → [接入实录](HOST_INTEGRATION.md)。

## 设计与模型

| 文档 | 讲什么 |
| --- | --- |
| [设计原则](MEMORY_SYSTEM_DESIGN_PRINCIPLES.md) | 五条哲学、四条硬约束、十三项明确不做的事 |
| [数据模型](MEMORY_SCHEMA_V1.md) | 24 字段主表、四类角色、主体防坍缩、索引设计 |
| [写入管线](MEMORY_WRITE_PIPELINE.md) | 价值闸门、槽位、九条决策表 |
| [读取管线](MEMORY_READ_PIPELINE.md) | 三通道与预算：什么时候注入什么、超了先砍谁 |
| [检索](MEMORY_RETRIEVAL_V1.md) | 结构化过滤、词法与二元组匹配、三阶段排序 |
| [生命周期](MEMORY_LIFECYCLE.md) | 状态机、合法转移、可解释性 |
| [原理长文](MEMORY_PRINCIPLES_REPORT.md) | 对象模型与读写检索的完整推演，含实测 |

## 宿主与实现

| 文档 | 讲什么 |
| --- | --- |
| [环境取证](CURRENT_ENVIRONMENT_ANALYSIS.md) | DSH 消息生命周期：注入点为什么只能在这里 |
| [接入实录](HOST_INTEGRATION.md) | 真实会话证据、压缩后三层恢复、安装踩坑 |
| [取舍表](ADOPTION_DECISION_TABLE.md) | 36 项机制取舍（采用 / 改造 / 推迟 / 拒绝） |
| [管理与界面](MEMORY_ADMIN.md) | 信任边界、不暴露裸 SQL 的四道闸 |
| [架构决策](ADR.md) | 34 条 ADR，每条含被否决的备选与理由 |

## 质量与历史

| 文档 | 讲什么 |
| --- | --- |
| [评估套件](MEMORY_EVAL_SUITE.md) | 不存垃圾 / 不认错主体 / 状态更新 / 不污染 / 作用域 |
| [缺陷日志](DEFECT_LOG.md) | 只有真跑才暴露的缺陷与修法 |
| [阶段记录](ENGINEERING_LOG.md) | 内部里程碑的变更、schema 演进与验证证据 |
| [发布说明 0.1.0](RELEASE_NOTES_v0.1.0.md) | 首个公开版本的范围、升级与已知限制 |

## 边界

| 文档 | 讲什么 |
| --- | --- |
| [推迟清单](V2_V3_RESERVED.md) | 刻意没做的，以及什么信号才该做 |
| [未知清单](UNKNOWN_LIST.md) | 还不知道的，以及怎么绕行 |
