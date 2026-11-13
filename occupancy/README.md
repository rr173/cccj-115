# 保护区单季物种占据分析（occupancy）流水线

多次访问相机/声学样点，未检出不等于不存在。用 `unmarked::occu` 拟合单季
occupancy 模型，分离潜在占据概率 ψ 与条件检测概率 p；用 `targets` 管理可复现
批处理。所有数据均为完全合成。

## 结构

```
_targets.R          targets 流水线定义
R/sim_data.R        合成数据：站点属性 / 访问记录 / 观察者-设备配置（含注入的数据问题）
R/cleaning.R        隔离表规则 + 检测历史构建（NA = 无检测机会，绝不填 0）
R/versioning.R      协变量版本固定；站点集合/公式/分析的稳定身份哈希；清单
R/model.R           occu 拟合、收敛与可辨识性诊断、发布闸门、预测区间
R/likelihood.R      手写单站点似然，对照 unmarked 输出
R/io.R              每个分析版本的批处理产物落盘
tests/validate.R    验证套件（全部案例）
run_pipeline.R      一键运行：tar_make() + 验证
output/<analysis_id>/   每版本产物：detection_history.csv, site_covariates.csv,
                        convergence.csv, estimates.csv, vcov.csv,
                        predictions.csv, likelihood_crosscheck.csv
output/manifest.csv     分析版本清单（同身份复用，输入修订/公式变化生成新版本）
```

## 数据状态语义

每次访问三种互斥状态，只有第三种算检测机会：

| 状态 | 条件 | 历史编码 |
|---|---|---|
| 访问取消 | `completed = FALSE` | `NA` |
| 设备故障 | `completed = TRUE, device_ok = FALSE` | `NA` |
| 有效访问 | 两者皆真 | 检出 `1` / 未检出 `0` |

缺陷记录进入隔离表（不静默修复）：`duplicate_site_key`（S005）、
`date_inversion`（S010）、`conflicting_detection`（S020，该访问变为缺失而非 0）、
`detection_without_opportunity`（S030）。

## 模型

- 占据子模型：`~ elev + forest`（站点协变量，按 `cov_version` 固定版本）
- 检测子模型：`~ obs_mid + obs_low + dev_aru`（观察者资历 + 设备类型，逐访问）
- 发布闸门：优化收敛（code 0）、SE 有限且不过大、vcov 条件数受控、无边界
  估计；不满足则不发布估计，输出定位问题的诊断（`WITHHELD.txt` +
  `convergence.csv`）。
- 预测区间：logit 尺度 Wald 区间 + 基于参数协方差的参数化 bootstrap 区间。

## 运行

```sh
Rscript run_pipeline.R        # 或先 targets::tar_make() 再 Rscript tests/validate.R
```

## 验证案例（tests/validate.R）

1. 全未检出站点仍有非零占据概率（似然含 `1-ψ` 项）
2. 有效未检出(0) 与设备故障/取消(NA) 区分
3. 观察者检测率差异被恢复（novice < mid < senior）
4. 缺访为 NA 而非 0
5. 重复站点键 / 日期倒置 / 冲突检测记录进入隔离表
6. 单一检测历史 → 不可辨识 → 不发布，诊断可定位
7. 协变量版本变更 → 新 analysis_id；同输入 → 同 id（复用）
8. 固定种子拟合完全可重复
9. 手写单站点似然与 unmarked 负对数似然一致（< 1e-6）
10. 概率范围与预测区间均在 [0,1] 且有序
